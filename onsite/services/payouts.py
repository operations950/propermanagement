"""Payouts: what the platforms have paid or will pay for each reservation.

Two kinds of report carry them:
  * the payout SUMMARY (already paid, and some scheduled) has stay dates, so it
    creates and enriches reservations — see onsite/services/bookings.py;
  * the "upcoming payouts" report has only the confirmation code, the money and
    an estimated payout date. It cannot create a reservation, so it attaches to
    ones already on record and holds the rest (PendingPayout) until a reservation
    with that code arrives from any other import.

A payout is also the best evidence that an upcoming reservation is real, which
is what onsite/services/review.py builds on: `payouts_through` records how far
ahead the uploaded payout reports actually reach."""
from django.db import transaction
from django.utils import timezone

from ..models import Booking, BookingFeedHealth, PendingPayout

PAID, SCHEDULED = 'paid', 'scheduled'


def status_for(payout_date, reported='', today=None):
    """'paid' once the payout date has passed, 'scheduled' before; the report's
    own word wins when it says one of the two."""
    reported = (reported or '').strip().lower()
    if reported in (PAID, SCHEDULED):
        return reported
    today = today or timezone.localdate()
    if payout_date is None:
        return ''
    return PAID if payout_date <= today else SCHEDULED


def note_coverage(source, payout_dates):
    """Records how far ahead this platform's payout reports reach (only ever
    forward)."""
    dates = [d for d in payout_dates if d]
    if not dates:
        return
    health, _ = BookingFeedHealth.objects.get_or_create(source=source)
    latest = max(dates)
    if health.payouts_through is None or latest > health.payouts_through:
        health.payouts_through = latest
        health.save(update_fields=['payouts_through'])


def _apply_to_booking(booking, amount, payout_date, status):
    """Sets a booking's payout from a payouts-only report. A payout already
    marked paid is never replaced by a later estimate."""
    if booking.payout_status == PAID and booking.payout_amount is not None:
        return False
    booking.payout_amount = amount
    booking.payout_date = payout_date
    booking.payout_status = status
    booking.amount_source = 'payout report'
    booking.save(update_fields=['payout_amount', 'payout_date', 'payout_status', 'amount_source'])
    return True


@transaction.atomic
def apply_payouts(source, rows, today=None):
    """Attach each row's payout to its reservation, or hold it for later.
    Returns {'attached': [(row, booking)], 'held': [row], 'kept': [(row, booking)]}
    where 'kept' are reservations whose already-paid amount was left alone."""
    result = {'attached': [], 'held': [], 'kept': []}
    for row in rows:
        status = status_for(row.payout_date, row.status, today)
        booking = Booking.objects.filter(source=source, external_uid=row.external_uid).first()
        if booking is None:
            PendingPayout.objects.update_or_create(
                source=source, external_uid=row.external_uid,
                defaults={'guest_name': row.guest_name, 'amount': row.amount, 'payout_date': row.payout_date, 'status': status},
            )
            result['held'].append(row)
        elif _apply_to_booking(booking, row.amount, row.payout_date, status):
            result['attached'].append((row, booking))
        else:
            result['kept'].append((row, booking))
    note_coverage(source, [r.payout_date for r in rows])
    return result


def attach_pending(source, uids):
    """Called after an import or calendar poll: any held payout whose code now
    belongs to a reservation is attached to it and forgotten."""
    uids = [u for u in uids if u]
    if not uids:
        return 0
    attached = 0
    for pending in PendingPayout.objects.filter(source=source, external_uid__in=uids):
        booking = Booking.objects.filter(source=source, external_uid=pending.external_uid).first()
        if booking is None:
            continue
        _apply_to_booking(booking, pending.amount, pending.payout_date, pending.status or status_for(pending.payout_date))
        pending.delete()
        attached += 1
    return attached
