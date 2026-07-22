import logging
from datetime import datetime, timedelta

import requests
from django.conf import settings
from django.utils import timezone

from .base import IntakeAdapter, RawEvent

logger = logging.getLogger(__name__)

QUO_API_BASE = 'https://api.quo.com'
CURSOR_KEY = 'quo_conversations_updated_after'

# How much thread history to actually feed Claude / store as raw_context.
# Some Quo threads span months or years with the same recurring vendor —
# dragging in the entire history every time is expensive and, more
# importantly, makes "original source text" mean "years of relationship
# history" instead of "the context for this issue." MIN guarantees a sparse
# thread still gets enough context even if it's all older than the window;
# MAX bounds the worst case for a very chatty recent thread.
RECENT_WINDOW_DAYS = 45
MIN_MESSAGES = 10
MAX_MESSAGES = 150


class QuoAPIError(Exception):
    pass


class QuoAdapter(IntakeAdapter):
    """Reads conversation threads (SMS) from the shared Quo phone line.

    Docs: https://www.quo.com/docs/mdx/api-reference/introduction
    Auth: raw API key in the `Authorization` header — NOT `Bearer <key>`.
    Rate limit: 10 requests/second per key (see rate-limits.md).

    Unlike the other adapters, this returns one RawEvent per *conversation*
    (not per message), carrying a bounded chunk of the chronological message
    transcript for that thread (see RECENT_WINDOW_DAYS/MIN/MAX_MESSAGES
    above) — see intake/classifier.py's `_handle_quo_thread` and
    intake/thread_classifier.py, which read the thread with Claude before
    deciding whether it's actionable. A thread only appears in the results
    if its latest message has changed since the last poll (tracked via
    intake.models.QuoThreadState), so unchanged conversations aren't
    re-fetched or re-classified every run. Conversations that were fetched
    but never successfully classified (e.g. a billing/API error) stay in a
    retry bucket and get offered again on every subsequent poll, independent
    of the updatedAfter cursor — otherwise the cursor moving past them would
    mean they're silently never retried.

    Also looks up each participant against Quo's own Contacts API (saved
    name/company) once per poll, so the classifier — and the ticket's
    linked Contact record — know whether the message came from someone
    staff have actually saved, not just a bare phone number. This matters
    because the shared line has no access control: anyone who texts it can
    trigger a classification, so knowing "this is a known vendor" vs
    "unrecognized number" is real signal, not decoration.

    This business runs one shared Quo number for every property (no
    per-property number mapping), so `RawEvent.property_name` is left blank
    here — the classifier asks Claude to guess the property from the
    conversation's content instead, and leaves it blank for staff to assign
    if it can't tell.
    """

    def _headers(self):
        return {'Authorization': settings.QUO_API_KEY, 'Content-Type': 'application/json'}

    def _get(self, path, params=None):
        resp = requests.get(f'{QUO_API_BASE}{path}', headers=self._headers(), params=params or {}, timeout=15)
        if resp.status_code == 429:
            raise QuoAPIError('Rate limited by Quo API (429) — will retry next poll.')
        resp.raise_for_status()
        return resp.json()

    def _list_conversations(self, updated_after=None):
        conversations = []
        page_token = None
        while True:
            params = {'maxResults': 100}
            if updated_after:
                params['updatedAfter'] = updated_after
            if page_token:
                params['pageToken'] = page_token
            data = self._get('/v1/conversations', params)
            conversations.extend(data.get('data', []))
            page_token = data.get('nextPageToken')
            if not page_token:
                break
        return conversations

    def _list_messages(self, phone_number_id, participant):
        messages = []
        page_token = None
        while True:
            params = {'phoneNumberId': phone_number_id, 'participants': [participant], 'maxResults': 100}
            if page_token:
                params['pageToken'] = page_token
            data = self._get('/v1/messages', params)
            messages.extend(data.get('data', []))
            page_token = data.get('nextPageToken')
            if not page_token:
                break
        messages.sort(key=lambda m: m.get('createdAt', ''))
        return messages

    def _list_contacts(self):
        contacts = []
        page_token = None
        pages = 0
        while True:
            params = {'maxResults': 50}
            if page_token:
                params['pageToken'] = page_token
            data = self._get('/v1/contacts', params)
            contacts.extend(data.get('data', []))
            page_token = data.get('nextPageToken')
            pages += 1
            if not page_token or pages >= 40:  # ~2000 contacts safety cap
                break
        return contacts

    def _build_contact_lookup(self):
        """phone (E.164) -> {'name': ..., 'company': ...}. Quo's /v1/contacts
        has no phone-number filter, so this fetches the whole list once per
        poll rather than once per conversation."""
        lookup = {}
        try:
            contacts = self._list_contacts()
        except (requests.RequestException, QuoAPIError):
            logger.exception('Quo: failed to list contacts — proceeding without caller identity')
            return lookup
        for c in contacts:
            fields = c.get('defaultFields') or {}
            name = ' '.join(filter(None, [fields.get('firstName'), fields.get('lastName')])).strip()
            company = fields.get('company') or ''
            for p in fields.get('phoneNumbers') or []:
                number = p.get('value')
                if number:
                    lookup[number] = {'name': name, 'company': company}
        logger.info('Quo: loaded %d contact(s) for caller-identity lookup', len(lookup))
        return lookup

    def _recent_messages(self, messages):
        """Bound how much history gets used — see RECENT_WINDOW_DAYS etc.
        Returns (messages_to_use, was_truncated)."""
        if len(messages) <= MIN_MESSAGES:
            return messages, False

        cutoff = timezone.now() - timedelta(days=RECENT_WINDOW_DAYS)

        def _parsed(m):
            try:
                return datetime.fromisoformat(m.get('createdAt', '').replace('Z', '+00:00'))
            except ValueError:
                return None

        recent = [m for m in messages if (_parsed(m) or timezone.now()) >= cutoff]
        if len(recent) < MIN_MESSAGES:
            recent = messages[-MIN_MESSAGES:]
        recent = recent[-MAX_MESSAGES:]
        return recent, len(recent) < len(messages)

    def _format_transcript(self, messages, contact, participant, total_count):
        lines = []
        if total_count > len(messages):
            lines.append(
                f'[Showing the most recent {len(messages)} of {total_count} total messages in this '
                f'conversation — older history omitted.]'
            )
        if contact and (contact.get('name') or contact.get('company')):
            label = contact.get('name') or ''
            if contact.get('company'):
                label = f"{label} ({contact['company']})" if label else contact['company']
            lines.append(f'[Caller: {label} — saved Quo contact, {participant}]')
        else:
            lines.append(f'[Caller: {participant} — NOT a saved contact in Quo. Treat with extra scrutiny.]')
        lines.append('')
        for m in messages:
            speaker = 'Staff (Quo line)' if m.get('direction') == 'outgoing' else m.get('from', 'Caller')
            lines.append(f"[{m.get('createdAt', '')}] {speaker}: {m.get('text', '')}")
        return '\n'.join(lines)

    def _build_event(self, conversation_id, phone_number_id, participant, known_last_message_id, contact_lookup):
        """Fetch a conversation's messages and build a RawEvent if it has
        new activity since `known_last_message_id`. Returns None if there's
        nothing new, or if the fetch itself fails (caller decides how to
        handle that — see the two call sites in pull())."""
        messages = self._list_messages(phone_number_id, participant)
        if not messages:
            return None
        latest_message_id = messages[-1].get('id', '')
        if known_last_message_id and known_last_message_id == latest_message_id:
            return None

        recent, _truncated = self._recent_messages(messages)
        contact = contact_lookup.get(participant)
        return RawEvent(
            event_type='quo_thread',
            source='quo',
            external_id=conversation_id,
            body=self._format_transcript(recent, contact, participant, total_count=len(messages)),
            reporter_phone=participant,
            reporter_name=(contact.get('name') or '') if contact else '',
            extra={
                'phone_number_id': phone_number_id or '',
                'latest_message_id': latest_message_id,
                'is_known_contact': bool(contact),
                'contact_company': (contact.get('company') or '') if contact else '',
            },
        )

    def pull(self) -> list[RawEvent]:
        if not settings.QUO_API_KEY:
            return []

        from intake.models import PollCursor, QuoThreadState

        cursor, _ = PollCursor.objects.get_or_create(key=CURSOR_KEY, defaults={'value': ''})
        if cursor.value:
            updated_after = cursor.value
        else:
            # First-ever sync: bound to a recent window rather than the
            # entire account history (could be years, thousands of threads).
            since = timezone.now() - timedelta(days=settings.QUO_INITIAL_SYNC_DAYS)
            updated_after = since.isoformat()
            logger.info(
                'Quo: first sync — limiting to conversations updated in the last %d day(s) (since %s)',
                settings.QUO_INITIAL_SYNC_DAYS, updated_after,
            )
        poll_started_at = timezone.now().isoformat()

        try:
            conversations = self._list_conversations(updated_after=updated_after)
        except (requests.RequestException, QuoAPIError):
            logger.exception('Quo: failed to list conversations')
            return []
        logger.info('Quo: found %d conversation(s) to check (updated_after=%s)', len(conversations), updated_after)

        contact_lookup = self._build_contact_lookup()

        known_state = {
            s.conversation_id: s.last_message_id
            for s in QuoThreadState.objects.filter(conversation_id__in=[c['id'] for c in conversations])
        }

        events = []
        seen_ids = set()
        for i, convo in enumerate(conversations, start=1):
            conversation_id = convo['id']
            seen_ids.add(conversation_id)
            logger.info('Quo: checking conversation %d/%d (%s)', i, len(conversations), conversation_id)
            phone_number_id = convo.get('phoneNumberId')
            participants = convo.get('participants') or []
            if not participants:
                continue

            try:
                event = self._build_event(
                    conversation_id, phone_number_id, participants[0], known_state.get(conversation_id),
                    contact_lookup,
                )
            except (requests.RequestException, QuoAPIError):
                # Failure here doesn't lose the thread — it stays in the
                # retry bucket below (any QuoThreadState row never
                # successfully classified) regardless of what the cursor
                # does, since get_or_create in classifier.py's
                # _handle_quo_thread already recorded it without a
                # last_message_id.
                logger.exception('Quo: failed to list messages for conversation %s', conversation_id)
                continue
            if event:
                events.append(event)

        # Retry bucket: conversations we've seen before but never
        # successfully classified (API/billing errors, etc). These need
        # retrying regardless of the cursor — once it advances past a
        # conversation's updatedAt, `_list_conversations` above will never
        # offer it again unless it gets *further* new activity.
        retry_states = list(
            QuoThreadState.objects.filter(last_classified_at__isnull=True).exclude(conversation_id__in=seen_ids)
        )
        if retry_states:
            logger.info('Quo: retrying %d previously-unclassified conversation(s)', len(retry_states))
        for state in retry_states:
            try:
                event = self._build_event(
                    state.conversation_id, state.phone_number_id, state.participant, None, contact_lookup,
                )
            except (requests.RequestException, QuoAPIError):
                logger.exception('Quo: retry failed for conversation %s', state.conversation_id)
                continue
            if event:
                events.append(event)

        cursor.value = poll_started_at
        cursor.save()
        return events
