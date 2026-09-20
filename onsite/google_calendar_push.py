"""One-directional push of Visit scheduling onto a single shared Google
Calendar (GOOGLE_ONSITE_CALENDAR_ID) — separate from core/google_calendar.py's
per-staff personal-calendar reads, though it reuses that module's
create_event/update_event/delete_event plumbing rather than duplicating the
OAuth/HTTP-error handling.

There's no service-account concept in this app, so the calendar is pushed
to using whichever staff member's own connected Google account is
available — preferring a Company Admin's, since that's the closest existing
notion of "the company's" connection (see core/quickbooks.py for the same
one-shared-thing framing). That staff member's Google account must have
been given access to the GOOGLE_ONSITE_CALENDAR_ID calendar for this to
work.

How the calendar stays in step with the system: every Visit has at most one
event, kept as an ALL-DAY event on the visit's scheduled_date, titled
"<visit type> — <property>" with the assigned cleaner invited to it.
sync_visit() computes what that event SHOULD say from the visit as it is
right now, compares a fingerprint of it with what was last pushed
(Visit.google_synced_state), and creates / updates / deletes only when they
differ:
  - a visit that is cancelled or skipped, or has no date, should have NO
    event → any existing one is deleted;
  - a changed date, property, visit type or ready-by moves/retitles it;
  - a changed assignee swaps the invitee: the old cleaner is removed from
    the event (and told), the new one added (and invited).
sync_visit runs from a post_save signal (onsite/signals.py) so it doesn't
matter which screen or import changed the visit, and reconcile() re-checks
everything on a timer as a backstop (queryset .update() calls bypass
signals; a push that failed; calendar connected after visits existed)."""
import hashlib
import json
import logging
import threading
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import dateformat, timezone

from core.google_calendar import GoogleCalendarWriteError, create_event, delete_event, update_event
from core.models import GoogleCalendarToken

from .services import times

logger = logging.getLogger(__name__)

# The app runs as a single process (see the Procfile), but request threads
# and the scheduler thread share it: without this, a save and a reconcile
# pass over the same visit at the same moment could each see "no event yet"
# and both create one.
_lock = threading.RLock()

RECONCILE_LOOKBACK_DAYS = 2
GONE_STATUS_CODES = (404, 410)
HEALTH_REFRESH_SECONDS = 600


def is_configured():
    return bool(settings.GOOGLE_ONSITE_CALENDAR_ID)


def _pushing_token():
    admin_token = GoogleCalendarToken.objects.filter(staff__is_company_admin=True).first()
    return admin_token or GoogleCalendarToken.objects.first()


# --- what the event should say ------------------------------------------------

def _dead_statuses():
    from .models import Visit
    return (Visit.Status.CANCELLED, Visit.Status.SKIPPED)


def _assignee_email(visit):
    if visit.assigned_staff_id:
        email = visit.assigned_staff.user.email
    elif visit.assigned_contact_id:
        email = visit.assigned_contact.email
    else:
        email = ''
    return (email or '').strip().lower()


def desired_event(visit):
    """What this visit's calendar event should look like right now, or None
    if it shouldn't have one (cancelled/skipped, or nothing to put a date
    on). Always all-day. `attendees` is the assigned cleaner's email when
    they have one — a cleaner with no email on file just isn't invited (the
    assignee still shows in the description)."""
    if visit.status in _dead_statuses() or not visit.scheduled_date:
        return None
    summary = f'{visit.visit_type} — {visit.property.name}'
    if visit.unit_id:
        summary += f' ({visit.unit.label})'
    # The agreed guest times (checkout, next check-in — with any approved
    # early/late change) are what a cleaner plans the day around; the link is
    # the backup for the text message that carries it, which isn't always seen.
    lines = list(times.visit_time_lines(visit))
    if visit.ready_by and not visit.next_booking_id:
        lines.append('Ready by ' + dateformat.format(timezone.localtime(visit.ready_by), 'D M j, g:i A'))
    lines.append(f'Assigned to: {visit.assignee_label()}')
    if visit.is_deep_clean:
        lines.append('Deep clean')
    lines.append(f'Cleaner link: {times.visit_link(visit)}')
    email = _assignee_email(visit)
    return {
        'summary': summary,
        'date': visit.scheduled_date,
        'description': '\n'.join(lines),
        'attendees': [email] if email else [],
    }


def _fingerprint(desired, calendar_id):
    payload = json.dumps({**desired, 'date': desired['date'].isoformat(), 'calendar': calendar_id}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _notify_fingerprint(desired, calendar_id):
    """Just what changes the invitation itself: title, day, invitees."""
    payload = json.dumps({
        'summary': desired['summary'], 'date': desired['date'].isoformat(),
        'attendees': desired['attendees'], 'calendar': calendar_id,
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


# --- health (what the "not being pushed" warning reads) ----------------------

def _health():
    from .models import OnsiteCalendarHealth
    return OnsiteCalendarHealth.objects.first() or OnsiteCalendarHealth.objects.create()


def _record_success():
    health = _health()
    now = timezone.now()
    if health.last_error or health.needs_reconnect or not health.last_success_at \
            or (now - health.last_success_at).total_seconds() > HEALTH_REFRESH_SECONDS:
        health.last_success_at, health.last_error, health.needs_reconnect = now, '', False
        health.save(update_fields=['last_success_at', 'last_error', 'needs_reconnect'])


def _record_failure(message, needs_reconnect=False):
    health = _health()
    health.last_error, health.last_error_at, health.needs_reconnect = message[:255], timezone.now(), needs_reconnect
    health.save(update_fields=['last_error', 'last_error_at', 'needs_reconnect'])


def status():
    """{'ok': bool, 'code': str, 'message': str} — whether visits are
    actually reaching the calendar, in words an admin can act on. Shown as a
    banner on the On-Site dashboard and in Admin Tools."""
    from .models import Visit

    if not is_configured():
        return {
            'ok': False, 'code': 'not_configured',
            'message': "On-site visits are NOT being added to Google Calendar: no calendar has been chosen. "
                       'Add the calendar\'s ID in Admin Tools → API Keys & Secrets → "Google Calendar ID for on-site visit push".',
        }
    if not GoogleCalendarToken.objects.exists():
        return {
            'ok': False, 'code': 'no_account',
            'message': "On-site visits are NOT being added to Google Calendar: no staff member has connected a Google "
                       "account, so there's nothing to publish with. Connect Google Calendar (Owner Dashboard) using an "
                       'account that can edit the on-site calendar.',
        }
    health = _health()
    if health.needs_reconnect or (
        health.last_error and (not health.last_success_at or (health.last_error_at and health.last_error_at > health.last_success_at))
    ):
        return {
            'ok': False, 'code': 'error',
            'message': f'Google Calendar is not accepting on-site visit updates: {health.last_error}'
                       + (' Disconnect and reconnect the Google account that publishes them.' if health.needs_reconnect else ' It will keep retrying every 30 minutes.'),
        }
    pending = Visit.objects.filter(google_sync_pending=True).exclude(status__in=_dead_statuses()).count()
    if pending:
        return {
            'ok': False, 'code': 'pending',
            'message': f"{pending} on-site visit{'s' if pending != 1 else ''} haven't reached Google Calendar yet — retrying every 30 minutes.",
        }
    return {'ok': True, 'code': 'ok', 'message': 'On-site visits are being kept in sync with Google Calendar.'}


# --- the sync itself ----------------------------------------------------------

def sync_visit(visit):
    """Brings this visit's calendar event in line with the visit (create,
    update or delete as needed). Accepts a Visit or a pk; always works from
    a fresh copy out of the database so a stale in-memory object can never
    push old data or create a second event. Never raises — a failure is
    logged, recorded for the dashboard warning, and flagged
    google_sync_pending so the timer retries it."""
    pk = getattr(visit, 'pk', visit)
    with _lock:
        try:
            _sync(pk)
        except Exception:
            logger.exception('Unexpected error syncing visit %s to Google Calendar', pk)
            _mark_pending(pk)


# Kept under the names the rest of the codebase already calls.
push_visit = sync_visit
delete_visit_event = sync_visit


def _mark_pending(pk):
    from .models import Visit
    Visit.objects.filter(pk=pk).update(google_sync_pending=True)


def _sync(pk):
    from .models import Visit

    if not is_configured():
        return
    token = _pushing_token()
    if not token:
        return
    visit = (
        Visit.objects.select_related('property', 'unit', 'visit_type', 'assigned_staff__user', 'assigned_contact', 'booking', 'next_booking')
        .prefetch_related('booking__guest_requests', 'next_booking__guest_requests')
        .filter(pk=pk).first()
    )
    if visit is None:
        return
    calendar_id = settings.GOOGLE_ONSITE_CALENDAR_ID
    desired = desired_event(visit)

    if desired is None:
        if visit.google_event_id:
            _delete_event_for(visit, token, calendar_id)
        return

    fingerprint = _fingerprint(desired, calendar_id)
    if visit.google_event_id and visit.google_synced_state == fingerprint and not visit.google_sync_pending:
        return

    start = desired['date']
    end = start + timedelta(days=1)  # Google's all-day end date is exclusive
    notify_fp = _notify_fingerprint(desired, calendar_id)
    fields = dict(all_day=True, description=desired['description'], attendees=desired['attendees'], send_updates='all')
    # Emailing the invitees is for changes to the invitation (day, title,
    # who is invited). A description-only change — the agreed times, the
    # link — is written quietly. An event with no recorded state yet (made
    # before this was tracked) is treated as unchanged the first time, so
    # deploying it doesn't email every cleaner about every upcoming visit.
    quiet = bool(visit.google_event_id) and (not visit.google_notify_state or visit.google_notify_state == notify_fp)
    try:
        event = None
        if visit.google_event_id:
            try:
                event = update_event(
                    token, calendar_id, visit.google_event_id, desired['summary'], start, end,
                    **{**fields, 'send_updates': 'none' if quiet else 'all'},
                )
            except GoogleCalendarWriteError as e:
                if e.status_code not in GONE_STATUS_CODES:
                    raise
            # Deleted by hand in Google (or moved to another calendar): make a fresh one.
            if event is not None and event.get('status') == 'cancelled':
                event = None
        if event is None:
            event = create_event(token, calendar_id, desired['summary'], start, end, **fields)
        event_id = event.get('id') or ''
        if not event_id:
            raise GoogleCalendarWriteError("Google Calendar didn't return an ID for the event it created.")
        visit.google_event_id = event_id
        visit.google_synced_state = fingerprint
        visit.google_notify_state = notify_fp
        visit.google_sync_pending = False
        visit.save(update_fields=['google_event_id', 'google_synced_state', 'google_notify_state', 'google_sync_pending'])
        _record_success()
    except GoogleCalendarWriteError as e:
        logger.warning('Onsite calendar push failed for visit %s (%s) — will retry.', pk, e)
        visit.google_sync_pending = True
        visit.save(update_fields=['google_sync_pending'])
        _record_failure(str(e), needs_reconnect=e.needs_reconnect)


def _delete_event_for(visit, token, calendar_id):
    try:
        delete_event(token, calendar_id, visit.google_event_id, send_updates='all')
    except GoogleCalendarWriteError as e:
        if e.status_code not in GONE_STATUS_CODES:
            logger.warning('Onsite calendar delete failed for visit %s (%s) — will retry.', visit.pk, e)
            visit.google_sync_pending = True
            visit.save(update_fields=['google_sync_pending'])
            _record_failure(str(e), needs_reconnect=e.needs_reconnect)
            return
    visit.google_event_id = ''
    visit.google_synced_state = ''
    visit.google_notify_state = ''
    visit.google_sync_pending = False
    visit.save(update_fields=['google_event_id', 'google_synced_state', 'google_notify_state', 'google_sync_pending'])
    _record_success()


def delete_orphaned_event(event_id):
    """For a Visit row that was deleted outright (there's no row left to
    update afterward) — removes its calendar event. Best-effort, never raises."""
    if not event_id or not is_configured():
        return
    token = _pushing_token()
    if not token:
        return
    with _lock:
        try:
            delete_event(token, settings.GOOGLE_ONSITE_CALENDAR_ID, event_id, send_updates='all')
        except GoogleCalendarWriteError as e:
            if e.status_code not in GONE_STATUS_CODES:
                logger.warning('Could not delete the calendar event of a deleted visit (%s).', e)
                _record_failure(str(e), needs_reconnect=e.needs_reconnect)
        except Exception:
            logger.exception('Unexpected error deleting the calendar event of a deleted visit')


def reconcile():
    """Backstop, run on a timer: re-syncs every visit that could be out of
    step — anything whose last push failed, anything scheduled from a couple
    of days ago onward (covers a calendar connected after visits already
    existed, and changes made by code that bypasses signals), and any
    cancelled/skipped visit still holding an event. Visits already in step
    cost one database read and no Google call."""
    from .models import Visit

    if not is_configured() or not _pushing_token():
        return
    cutoff = timezone.localdate() - timedelta(days=RECONCILE_LOOKBACK_DAYS)
    pks = list(
        Visit.objects.filter(
            Q(google_sync_pending=True)
            | Q(scheduled_date__gte=cutoff)
            | (Q(status__in=_dead_statuses()) & ~Q(google_event_id=''))
        ).values_list('pk', flat=True)
    )
    for pk in pks:
        sync_visit(pk)


retry_pending = reconcile
