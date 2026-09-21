"""Bringing on-site visits back in line with the synced calendars.

The calendars are the single source of truth for who is actually checking in
and out, so the cleaning schedule should be exactly what the calendars show.
A reservation that only a payment report brought in (no matching calendar event
on a listing whose calendar is connected and healthy) must not have a cleaning —
it may be the payment for a cancellation or an old stay. Those visits are removed
QUIETLY: no calendar cancellation notices, no messages.

The reservation itself is treated according to what it is worth:
  * paid (a payout on record) or confirmed real by a person: kept as a payment
    record. It is still valid income; it just has no cleaning and isn't on the
    Rentals board.
  * not paid and not confirmed: a stale or duplicate row, removed outright when
    nothing else depends on it (otherwise marked cancelled).
Stays that have already started, finished cleanings and anything in progress are
never touched. Nothing is done for a listing without a working calendar."""
from django.db import transaction
from django.utils import timezone

from .. import google_calendar_push
from ..models import Booking, Visit
from . import coverage
from .bookings import _refresh_next_bookings_for_property

UNTOUCHABLE_VISIT = (Visit.Status.SUBMITTED, Visit.Status.VERIFIED, Visit.Status.CANCELLED, Visit.Status.SKIPPED, Visit.Status.IN_PROGRESS)


def has_payment_evidence(booking):
    return booking.payout_amount is not None or booking.confirmed_real


def _removable_visits(booking):
    return booking.visits.exclude(status__in=UNTOUCHABLE_VISIT).filter(started_at__isnull=True)


def quiet_cancel_visits(booking):
    """Cancels the booking's unfinished, unstarted visits and removes their
    calendar events without notifying anyone. Returns how many."""
    count = 0
    for visit in _removable_visits(booking):
        gone = google_calendar_push.quiet_delete_event(visit)
        # A queryset update on purpose: it bypasses the save signal, whose
        # calendar sync would delete the event again WITH an email.
        Visit.objects.filter(pk=visit.pk).update(status=Visit.Status.CANCELLED, google_sync_pending=not gone)
        count += 1
    return count


def _deletable(booking):
    return (
        booking.source != Booking.Source.MANUAL and not booking.manually_cancelled
        and not booking.guest_requests.exists()
        and not booking.visits.filter(status__in=(Visit.Status.SUBMITTED, Visit.Status.VERIFIED, Visit.Status.IN_PROGRESS)).exists()
        and not booking.visits.filter(payment_batch__isnull=False).exists()
    )


def retire_booking(booking):
    """Quietly takes a reservation the calendar doesn't back out of operations.
    Returns 'kept' (a payment record now), 'removed' or 'cancelled'."""
    property_ = booking.property
    quiet_cancel_visits(booking)
    if booking.on_calendar:
        booking.on_calendar = False
        booking.save(update_fields=['on_calendar'])
    if has_payment_evidence(booking):
        outcome = 'kept'
    elif _deletable(booking):
        booking.delete()
        outcome = 'removed'
    else:
        booking.status = Booking.Status.CANCELLED
        booking.save(update_fields=['status'])
        outcome = 'cancelled'
    _refresh_next_bookings_for_property(property_)
    return outcome


def demote_booking(booking):
    """A reservation the calendar used to show but no longer does, that has money
    on record: keep it as a payment record and cancel its unfinished cleanings the
    normal way (the cleaner is told, as for any real cancellation)."""
    for visit in _removable_visits(booking):
        visit.status = Visit.Status.CANCELLED
        visit.save(update_fields=['status'])
    booking.on_calendar = False
    booking.save(update_fields=['on_calendar'])
    _refresh_next_bookings_for_property(booking.property)


def plan(now=None):
    """What a recalibration would do, from what the last calendar polls found:
    every upcoming platform reservation, on a listing whose calendar is healthy,
    that the calendar does not show."""
    now = now or timezone.now()
    covered = coverage.covered_keys(now)
    candidates = (
        coverage.payment_only(Booking.objects.filter(status=Booking.Status.ACTIVE, check_in__gt=now), covered)
        .select_related('property', 'unit').order_by('property__name', 'check_in')
    )
    keep, remove = [], []
    for booking in candidates:
        entry = {'booking': booking, 'visits': _removable_visits(booking).count()}
        (keep if has_payment_evidence(booking) else remove).append(entry)
    uncovered_count = sum(
        1 for b in Booking.objects.filter(status=Booking.Status.ACTIVE, check_out__gte=now, property__is_general=False)
        .exclude(source=Booking.Source.MANUAL).only('property_id', 'unit_id', 'source')
        if (b.property_id, b.unit_id, b.source) not in covered
    )
    return {
        'keep': keep, 'remove': remove, 'covered_listings': len(covered),
        'visits_to_remove': sum(e['visits'] for e in keep + remove), 'uncovered_reservations': uncovered_count,
    }


@transaction.atomic
def apply(now=None):
    """Carries out plan(): returns {'kept': n, 'removed': n, 'cancelled': n, 'visits': n}."""
    result = {'kept': 0, 'removed': 0, 'cancelled': 0, 'visits': 0}
    current = plan(now)
    result['visits'] = current['visits_to_remove']
    for entry in current['keep'] + current['remove']:
        result[retire_booking(entry['booking'])] += 1
    return result
