import logging
import time

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

QUO_API_BASE = 'https://api.quo.com'

# Quo's own limit is 10 req/sec per key, but several independent processes
# share this one key (the scheduler's sync_quo_contacts/link_quo_contact_threads
# jobs, plus any admin-triggered command like analyze_recent_quo_contacts,
# which alone can fire 25+ requests in under a second just paginating
# conversations) — a fixed pacing delay after every call plus a retry-with-
# backoff on 429 is what actually keeps a real, multi-hundred-page crawl
# from dying partway through instead of a single unhandled QuoAPIError (see
# the analyze_recent_quo_contacts production failure this was written to
# fix: it 429'd on _list_contacts() right after a clean 26-request
# conversation crawl, and the whole run was lost).
RATE_LIMIT_PACING_SECONDS = 0.12
MAX_429_RETRIES = 5


class QuoAPIError(Exception):
    pass


class QuoAdapter:
    """Read-only access to the shared Quo phone line's conversations,
    messages, calls, phone numbers, and contacts.

    Docs: https://www.quo.com/docs/mdx/api-reference/introduction
    Auth: raw API key in the `Authorization` header — NOT `Bearer <key>`.
    Rate limit: 10 requests/second per key (see rate-limits.md).

    Message capture itself is real-time via the Quo webhook
    (intake/views.py::quo_webhook), not via any method here. These methods
    back the scheduler's contact-sync/thread-linking jobs and the live
    Contractor Communication ticket UI (messaging/services.py's
    fetch_quo_conversation), which reads a thread's messages on demand.
    """

    def _headers(self):
        return {'Authorization': settings.QUO_API_KEY, 'Content-Type': 'application/json'}

    def _get(self, path, params=None):
        for attempt in range(1, MAX_429_RETRIES + 1):
            resp = requests.get(f'{QUO_API_BASE}{path}', headers=self._headers(), params=params or {}, timeout=15)
            if resp.status_code == 429:
                if attempt == MAX_429_RETRIES:
                    raise QuoAPIError('Rate limited by Quo API (429) — exhausted retries.')
                retry_after = resp.headers.get('Retry-After')
                try:
                    wait = float(retry_after) if retry_after else 0.5 * (2 ** (attempt - 1))
                except ValueError:
                    wait = 0.5 * (2 ** (attempt - 1))
                logger.warning(
                    'Quo: rate limited (429) on %s, retrying in %.1fs (attempt %d/%d)',
                    path, wait, attempt, MAX_429_RETRIES,
                )
                time.sleep(wait)
                continue
            resp.raise_for_status()
            time.sleep(RATE_LIMIT_PACING_SECONDS)
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

    def _list_phone_numbers(self):
        """Every phone line this Quo account owns — this business runs THREE
        (Primary/Backup/Evolve), not the one shared line the rest of this
        adapter's docstring assumes, which matters for anything that needs
        to check every line a contact might have called/texted (see
        analyze_recent_quo_contacts.py). No pagination in practice (a
        handful of numbers), but handle nextPageToken defensively anyway."""
        numbers = []
        page_token = None
        while True:
            params = {}
            if page_token:
                params['pageToken'] = page_token
            data = self._get('/v1/phone-numbers', params)
            numbers.extend(data.get('data', []))
            page_token = data.get('nextPageToken')
            if not page_token:
                break
        return numbers

    def _list_calls(self, phone_number_id, participant):
        """Call history for one (phoneNumberId, participant) pair — same
        shape/pagination as _list_messages, but /v1/calls requires BOTH
        params (a bare list with neither returns 400), so unlike
        _list_conversations there's no cheap global crawl for calls."""
        calls = []
        page_token = None
        while True:
            params = {'phoneNumberId': phone_number_id, 'participants': [participant], 'maxResults': 50}
            if page_token:
                params['pageToken'] = page_token
            data = self._get('/v1/calls', params)
            calls.extend(data.get('data', []))
            page_token = data.get('nextPageToken')
            if not page_token:
                break
        return calls

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
