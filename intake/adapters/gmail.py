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
    """Reads a shared mailbox (e.g. admin@proper-realty.com) via the Gmail
    API. Connected once via intake/views.py's gmail_connect flow (admin-
    only — see intake/gmail_auth.py) rather than an API key, since Gmail
    access is per-Google-account OAuth, not a static credential.

    Same whole-thread-classification architecture as Quo (see quo.py and
    intake/classifier.py's _handle_gmail_thread / _reconcile_thread_ticket):
    one RawEvent per email THREAD (not per message), only offered for
    (re)classification when the thread has new activity since we last saw
    it, with history bounded to a recent window rather than the whole
    mailbox history.
    """

    def _service(self):
        from googleapiclient.discovery import build

        from ..gmail_auth import credentials_for
        from ..models import GmailInboxToken

        token = GmailInboxToken.objects.first()
        if not token:
            return None
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

    def _build_event(self, service, thread_id, known_last_message_id):
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

        return RawEvent(
            event_type='gmail_thread',
            source='email',
            external_id=thread_id,
            title=subject,
            body=transcript,
            reporter_email=email_addr,
            reporter_name=name,
            extra={'latest_message_id': latest_message_id},
        )

    def pull(self) -> list[RawEvent]:
        from ..gmail_auth import is_configured
        from ..models import GmailInboxToken, GmailThreadState, PollCursor

        if not is_configured() or not GmailInboxToken.objects.exists():
            return []

        service = self._service()
        if service is None:
            return []

        cursor, _ = PollCursor.objects.get_or_create(key=CURSOR_KEY, defaults={'value': ''})
        if cursor.value:
            after = cursor.value
        else:
            since = timezone.now() - timedelta(days=settings.GMAIL_INITIAL_SYNC_DAYS)
            after = since.strftime('%Y/%m/%d')
            logger.info(
                'Gmail: first sync — limiting to threads updated in the last %d day(s) (since %s)',
                settings.GMAIL_INITIAL_SYNC_DAYS, after,
            )
        poll_started_at = timezone.now()
        query = f'in:inbox after:{after}'

        try:
            threads = self._list_threads(service, query)
        except Exception:
            logger.exception('Gmail: failed to list threads')
            return []
        logger.info('Gmail: found %d thread(s) to check (query=%r)', len(threads), query)

        known_state = {
            s.thread_id: s.last_message_id
            for s in GmailThreadState.objects.filter(thread_id__in=[t['id'] for t in threads])
        }

        events = []
        seen_ids = set()
        for i, th in enumerate(threads, start=1):
            thread_id = th['id']
            seen_ids.add(thread_id)
            try:
                event = self._build_event(service, thread_id, known_state.get(thread_id))
            except Exception:
                logger.exception('Gmail: failed to fetch thread %s', thread_id)
                continue
            if event:
                events.append(event)

        retry_states = list(
            GmailThreadState.objects.filter(last_classified_at__isnull=True).exclude(thread_id__in=seen_ids)
        )
        if retry_states:
            logger.info('Gmail: retrying %d previously-unclassified thread(s)', len(retry_states))
        for state in retry_states:
            try:
                event = self._build_event(service, state.thread_id, None)
            except Exception:
                logger.exception('Gmail: retry failed for thread %s', state.thread_id)
                continue
            if event:
                events.append(event)

        cursor.value = poll_started_at.strftime('%Y/%m/%d')
        cursor.save()
        return events
