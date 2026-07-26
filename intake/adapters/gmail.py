import base64
import logging
import re
from datetime import datetime, timedelta
from email.utils import parseaddr

from django.conf import settings
from django.utils import timezone

from .base import IntakeAdapter, RawEvent

logger = logging.getLogger(__name__)

CURSOR_KEY = 'gmail_threads_after'

# Same bounding rationale as Quo (see quo.py) — an email thread can span
# years with the same vendor; only a recent, size-capped window goes to
# Claude / gets stored as raw_context.
RECENT_WINDOW_DAYS = 45
MIN_MESSAGES = 5
MAX_MESSAGES = 40
MAX_MESSAGE_CHARS = 3000  # per-message cap so one bloated email (long quoted chain, signature) can't dominate


class GmailAdapter(IntakeAdapter):
    """Reads one or more shared mailboxes (e.g. operations@proper-realty.com,
    admin@proper-realty.com) via the Gmail API — each connected mailbox
    (see intake.models.GmailInboxToken, connected via intake/views.py's
    gmail_connect flow, admin-only) is polled independently in `pull()`.
    OAuth per mailbox, not an API key, since Gmail access is per-Google-
    account.

    Same whole-thread-classification architecture as Quo (see quo.py and
    intake/classifier.py's _handle_gmail_thread / _reconcile_thread_ticket):
    one RawEvent per email THREAD (not per message), only offered for
    (re)classification when the thread has new activity since we last saw
    it, with history bounded to a recent window rather than the whole
    mailbox history. The one exception is a thread whose latest message
    comes from an @airbnb.com address — those go out as event_type
    'airbnb_email_booking' instead of 'gmail_thread' (see _build_event),
    which intake/classifier.py routes to structured booking extraction
    (a cleaning ticket) rather than the generic thread classifier.
    """

    def _service(self, token):
        from googleapiclient.discovery import build

        from ..gmail_auth import credentials_for

        creds = credentials_for(token)
        return build('gmail', 'v1', credentials=creds, cache_discovery=False)

    def _list_threads(self, service, query):
        threads = []
        page_token = None
        while True:
            params = {'userId': 'me', 'q': query, 'maxResults': 50}
            if page_token:
                params['pageToken'] = page_token
            resp = service.users().threads().list(**params).execute()
            # TEMPORARY diagnostic — remove once root-caused, see quo.py's
            # matching diagnostic for why.
            logger.info(
                'Gmail: DIAG threads.list params=%r resultSizeEstimate=%r returned=%d',
                params, resp.get('resultSizeEstimate'), len(resp.get('threads', [])),
            )
            threads.extend(resp.get('threads', []))
            page_token = resp.get('nextPageToken')
            if not page_token:
                break
        return threads

    def _header(self, message, name):
        if not message:
            return ''
        for h in message.get('payload', {}).get('headers', []) or []:
            if h.get('name', '').lower() == name.lower():
                return h.get('value', '')
        return ''

    def _decode(self, data):
        try:
            return base64.urlsafe_b64decode(data + '=' * (-len(data) % 4)).decode('utf-8', errors='replace')
        except Exception:
            return ''

    def _extract_part(self, payload, mime_type):
        if payload.get('mimeType') == mime_type and payload.get('body', {}).get('data'):
            return self._decode(payload['body']['data'])
        for part in payload.get('parts', []) or []:
            found = self._extract_part(part, mime_type)
            if found:
                return found
        return None

    def _body_text(self, message):
        payload = message.get('payload', {})
        text = self._extract_part(payload, 'text/plain')
        if text:
            return text
        html = self._extract_part(payload, 'text/html')
        if html:
            return re.sub('<[^<]+?>', ' ', html)
        return message.get('snippet', '')

    def _recent_messages(self, messages):
        if len(messages) <= MIN_MESSAGES:
            return messages, False

        cutoff = timezone.now() - timedelta(days=RECENT_WINDOW_DAYS)

        def _msg_time(m):
            try:
                return datetime.fromtimestamp(int(m.get('internalDate', 0)) / 1000, tz=timezone.utc)
            except (ValueError, TypeError):
                return None

        recent = [m for m in messages if (_msg_time(m) or timezone.now()) >= cutoff]
        if len(recent) < MIN_MESSAGES:
            recent = messages[-MIN_MESSAGES:]
        recent = recent[-MAX_MESSAGES:]
        return recent, len(recent) < len(messages)

    def _format_transcript(self, messages, total_count):
        lines = []
        if total_count > len(messages):
            lines.append(
                f'[Showing the most recent {len(messages)} of {total_count} total messages in this '
                f'email thread — older history omitted.]'
            )
        lines.append('')
        for m in messages:
            frm = self._header(m, 'From')
            date = self._header(m, 'Date')
            body = self._body_text(m).strip()[:MAX_MESSAGE_CHARS]
            lines.append(f'[{date}] From: {frm}\n{body}\n')
        return '\n'.join(lines)

    def _build_event(self, service, thread_id, known_last_message_id, mailbox_email):
        thread = service.users().threads().get(userId='me', id=thread_id, format='full').execute()
        messages = thread.get('messages', [])
        if not messages:
            return None
        latest_message_id = messages[-1].get('id', '')
        if known_last_message_id and known_last_message_id == latest_message_id:
            return None

        recent, _truncated = self._recent_messages(messages)
        transcript = self._format_transcript(recent, total_count=len(messages))

        from_header = self._header(recent[0], 'From') if recent else ''
        name, email_addr = parseaddr(from_header)
        subject = self._header(messages[-1], 'Subject') or '(no subject)'

        # Airbnb's own automated notifications (booking confirmations,
        # cancellations, payout receipts, review requests, ...) all come
        # from an @airbnb.com address — a cheap, high-recall pre-filter.
        # The precise "is this actually a NEW booking confirmation" call is
        # Claude's (see intake/booking_classifier.py::extract_airbnb_booking,
        # invoked from intake/classifier.py::_handle_airbnb_email_booking),
        # which also rejects Airbnb's other automated mail types this same
        # domain check lets through.
        latest_from = self._header(messages[-1], 'From')
        is_airbnb = 'airbnb.com' in latest_from.lower()

        return RawEvent(
            event_type='airbnb_email_booking' if is_airbnb else 'gmail_thread',
            source='airbnb' if is_airbnb else 'email',
            external_id=thread_id,
            title=subject,
            body=transcript,
            reporter_email=email_addr,
            reporter_name=name,
            extra={'latest_message_id': latest_message_id, 'mailbox_email': mailbox_email},
        )

    def pull(self) -> list[RawEvent]:
        from ..gmail_auth import is_configured
        from ..models import GmailInboxToken

        if not is_configured():
            return []

        tokens = list(GmailInboxToken.objects.all())
        if not tokens:
            return []

        events = []
        for token in tokens:
            events.extend(self._pull_mailbox(token))
        return events

    def _pull_mailbox(self, token) -> list[RawEvent]:
        from ..models import GmailThreadState, PollCursor

        mailbox_email = token.mailbox_email
        try:
            service = self._service(token)
        except Exception:
            logger.exception('Gmail (%s): could not build an authenticated client', mailbox_email)
            return []

        # Each mailbox gets its own cursor — the same day-granularity
        # `after:` search-bound approach as before, just keyed per inbox
        # instead of one shared global row.
        cursor_key = f'{CURSOR_KEY}:{mailbox_email}'
        cursor, _ = PollCursor.objects.get_or_create(key=cursor_key, defaults={'value': ''})
        if cursor.value:
            after = cursor.value
        else:
            since = timezone.now() - timedelta(days=settings.GMAIL_INITIAL_SYNC_DAYS)
            after = since.strftime('%Y/%m/%d')
            logger.info(
                'Gmail (%s): first sync — limiting to threads updated in the last %d day(s) (since %s)',
                mailbox_email, settings.GMAIL_INITIAL_SYNC_DAYS, after,
            )
        poll_started_at = timezone.now()
        query = f'in:inbox after:{after}'

        try:
            threads = self._list_threads(service, query)
        except Exception:
            logger.exception('Gmail (%s): failed to list threads', mailbox_email)
            return []
        logger.info('Gmail (%s): found %d thread(s) to check (query=%r)', mailbox_email, len(threads), query)

        known_state = {
            s.thread_id: s.last_message_id
            for s in GmailThreadState.objects.filter(
                mailbox_email=mailbox_email, thread_id__in=[t['id'] for t in threads],
            )
        }

        events = []
        seen_ids = set()
        for th in threads:
            thread_id = th['id']
            seen_ids.add(thread_id)
            try:
                event = self._build_event(service, thread_id, known_state.get(thread_id), mailbox_email)
            except Exception:
                logger.exception('Gmail (%s): failed to fetch thread %s', mailbox_email, thread_id)
                continue
            if event:
                events.append(event)

        retry_states = list(
            GmailThreadState.objects.filter(mailbox_email=mailbox_email, last_classified_at__isnull=True)
            .exclude(thread_id__in=seen_ids)
        )
        if retry_states:
            logger.info('Gmail (%s): retrying %d previously-unclassified thread(s)', mailbox_email, len(retry_states))
        for state in retry_states:
            try:
                event = self._build_event(service, state.thread_id, None, mailbox_email)
            except Exception:
                logger.exception('Gmail (%s): retry failed for thread %s', mailbox_email, state.thread_id)
                continue
            if event:
                events.append(event)

        cursor.value = poll_started_at.strftime('%Y/%m/%d')
        cursor.save()
        return events
