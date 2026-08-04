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
work; if no one has connected a calendar at all, this degrades to a no-op
exactly like every other integration in this app."""
import logging
from datetime import datetime, time

from django.conf import settings
from django.utils import timezone

from core.google_calendar import GoogleCalendarWriteError, create_event, delete_event, update_event
from core.models import GoogleCalendarToken

logger = logging.getLogger(__name__)

DEFAULT_EVENT_TIME = time(9, 0)


def is_configured():
    return bool(settings.GOOGLE_ONSITE_CALENDAR_ID)


def _pushing_token():
    admin_token = GoogleCalendarToken.objects.filter(staff__is_company_admin=True).first()
    return admin_token or GoogleCalendarToken.objects.first()


def _event_start(visit):
    if visit.ready_by:
        return visit.ready_by
    if not visit.scheduled_date:
        return None
    naive = datetime.combine(visit.scheduled_date, visit.scheduled_start or DEFAULT_EVENT_TIME)
    return timezone.make_aware(naive) if timezone.is_naive(naive) else naive


def push_visit(visit):
    """Creates or updates the calendar event for a visit. Never raises —
    logs and sets google_sync_pending=True on any failure, matching the
    house 'degrade, don't break' pattern; a scheduler job retries pending
    visits later."""
    if not is_configured():
        return
    token = _pushing_token()
    if not token:
        return
    start = _event_start(visit)
    if start is None:
        return

    calendar_id = settings.GOOGLE_ONSITE_CALENDAR_ID
    summary = f'{visit.visit_type} — {visit.property.name}'
    try:
        if visit.google_event_id:
            update_event(token, calendar_id, visit.google_event_id, summary, start, start)
        else:
            event = create_event(token, calendar_id, summary, start, start)
            visit.google_event_id = event.get('id', '')
        visit.google_sync_pending = False
        visit.save(update_fields=['google_event_id', 'google_sync_pending'])
    except GoogleCalendarWriteError:
        logger.warning('Onsite calendar push failed for visit %s — will retry.', visit.pk)
        visit.google_sync_pending = True
        visit.save(update_fields=['google_sync_pending'])
    except Exception:
        logger.exception('Unexpected error pushing visit %s to calendar', visit.pk)
        visit.google_sync_pending = True
        visit.save(update_fields=['google_sync_pending'])


def delete_visit_event(visit):
    """Best-effort delete on cancellation — never raises."""
    if not is_configured() or not visit.google_event_id:
        return
    token = _pushing_token()
    if not token:
        return
    try:
        delete_event(token, settings.GOOGLE_ONSITE_CALENDAR_ID, visit.google_event_id)
        visit.google_event_id = ''
        visit.google_sync_pending = False
        visit.save(update_fields=['google_event_id', 'google_sync_pending'])
    except GoogleCalendarWriteError:
        logger.warning('Onsite calendar delete failed for visit %s.', visit.pk)
    except Exception:
        logger.exception('Unexpected error deleting calendar event for visit %s', visit.pk)


def retry_pending():
    """Called on a timer — re-pushes any visit whose last push attempt
    failed."""
    from .models import Visit

    pending = Visit.objects.filter(google_sync_pending=True).exclude(
        status__in=[Visit.Status.CANCELLED, Visit.Status.SKIPPED],
    )
    for visit in pending:
        push_visit(visit)
