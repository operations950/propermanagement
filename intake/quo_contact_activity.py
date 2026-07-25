import logging
import time

from django.utils import timezone
from django.utils.dateparse import parse_datetime

logger = logging.getLogger(__name__)

# Quo's 10 req/sec cap — has_recent_call makes up to len(our_number_ids) requests
# per contact checked, so this keeps a full sweep of the backlog comfortably under it.
CALL_CHECK_SLEEP_SECONDS = 0.12


def _parse_iso(ts):
    if not ts:
        return None
    dt = parse_datetime(ts)
    if dt and timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt


def list_our_phone_numbers(adapter):
    """This Quo account's own lines — (ids, E.164 numbers), both needed:
    ids to query /v1/calls per line, numbers to exclude ourselves from a
    conversation's participants when figuring out who the OTHER party is."""
    numbers = adapter._list_phone_numbers()
    ids = [n['id'] for n in numbers if n.get('id')]
    phones = {n['number'] for n in numbers if n.get('number')}
    return ids, phones


def build_text_activity_map(adapter, our_number_phones):
    """One global crawl of every conversation across every Quo phone line
    (no per-contact filtering needed or possible for /v1/conversations),
    returning {participant_e164: {'last_activity': datetime,
    'conversation_id':, 'phone_number_id':}} — the most recent conversation
    per participant. This is the authoritative source for "have we texted
    this person" instead of depending on our own QuoMessage mirror table,
    which only has what backfill_quo_messages/the live webhook happened to
    capture and can't be trusted to be complete."""
    activity = {}
    for convo in adapter._list_conversations():
        last_activity = _parse_iso(convo.get('lastActivityAt') or convo.get('updatedAt'))
        if not last_activity:
            continue
        phone_number_id = convo.get('phoneNumberId', '')
        conversation_id = convo.get('id', '')
        for participant in convo.get('participants') or []:
            if not participant or participant in our_number_phones:
                continue
            existing = activity.get(participant)
            if existing is None or last_activity > existing['last_activity']:
                activity[participant] = {
                    'last_activity': last_activity,
                    'conversation_id': conversation_id,
                    'phone_number_id': phone_number_id,
                }
    return activity


def has_recent_call(adapter, our_number_ids, participant_e164, cutoff):
    """Checks /v1/calls on every one of our lines for this participant (no
    global call feed exists — see QuoAdapter._list_calls's docstring), and
    returns (bool_has_call_since_cutoff, latest_call_info_or_None) where
    latest_call_info has 'created_at'/'direction'/'duration' regardless of
    whether it's within the cutoff, so callers can still report "we called
    them, just not recently" if useful."""
    latest = None
    for phone_number_id in our_number_ids:
        try:
            calls = adapter._list_calls(phone_number_id, participant_e164)
        except Exception:
            logger.exception('Quo: failed to list calls for %s on line %s', participant_e164, phone_number_id)
            calls = []
        time.sleep(CALL_CHECK_SLEEP_SECONDS)
        for call in calls:
            created_at = _parse_iso(call.get('createdAt'))
            if not created_at:
                continue
            if latest is None or created_at > latest['created_at']:
                latest = {
                    'created_at': created_at,
                    'direction': call.get('direction', ''),
                    'duration': call.get('duration') or 0,
                }
    has_recent = bool(latest and latest['created_at'] >= cutoff)
    return has_recent, latest
