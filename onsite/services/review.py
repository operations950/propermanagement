"""Reservations a person should look at, because they might not be real.

A platform's calendar and reports can leave stale or duplicate reservations
behind (an old upload, a guest who cancelled and rebooked, a listing mapped to
the wrong unit). The most reliable proof that an upcoming reservation is genuine
is that the platform has scheduled a payout for it. So two things are flagged:

  * upcoming reservations with NO payout on record — but only where the payout
    reports uploaded so far reach far enough ahead to have shown one (a
    reservation checking in on Oct 30 can't be judged from a report that ends
    Sep 22). "How far" is BookingFeedHealth.payouts_through per platform;
  * reservations that overlap at one unit, with a hint when exactly one of the
    pair has a payout.

Nothing is cancelled automatically: each is shown so a person decides (cancel it,
or confirm it is real). See reservation_service.cancel_booking."""
from datetime import datetime, time, timedelta

from django.utils import timezone

from django.db.models import Q

from ..models import Booking, BookingFeedHealth
from . import coverage as coverage_service

# VRBO pays out about a day after check-in.
PAYOUT_LAG_DAYS = 1


def _local_date(dt):
    return timezone.localtime(dt).date()


def _start_of(day):
    return timezone.make_aware(datetime.combine(day, time.min))


def coverage():
    """{source: date the payout reports reach through} for platforms that have
    had a payout report uploaded."""
    return {h.source: h.payouts_through for h in BookingFeedHealth.objects.exclude(payouts_through__isnull=True)}


def unpaid_upcoming(today=None):
    """Upcoming (or current) platform reservations with no payout on record whose
    payout date falls inside what the uploaded payout reports cover, oldest
    check-in first. Reservations already confirmed real are left out."""
    today = today or timezone.localdate()
    reach = coverage()
    if not reach:
        return []
    rows = (coverage_service.operational(Booking.objects.filter(
        status=Booking.Status.ACTIVE, source__in=list(reach), payout_amount__isnull=True, confirmed_real=False,
        property__is_general=False, check_out__gte=_start_of(today),
    )).select_related('property', 'unit').order_by('check_in'))
    return [b for b in rows if _local_date(b.check_in) + timedelta(days=PAYOUT_LAG_DAYS) <= reach[b.source]]


def conflicts(today=None):
    """Pairs of current or upcoming reservations that share a night at one unit,
    each with a hint about which is likelier real (the one with a payout) when
    that is unambiguous."""
    today = today or timezone.localdate()
    bookings = (Booking.objects.filter(
        Q(on_calendar=True) | Q(source=Booking.Source.MANUAL),
        status=Booking.Status.ACTIVE, property__is_general=False, check_out__gte=_start_of(today),
    ).select_related('property', 'unit').order_by('check_in'))
    by_unit = {}
    for b in bookings:
        by_unit.setdefault((b.property_id, b.unit_id), []).append(b)
    found = []
    for stays in by_unit.values():
        latest = None
        for b in stays:
            if latest is not None and _local_date(b.check_in) < _local_date(latest.check_out):
                pair = [latest, b]
                paid = [x for x in pair if x.payout_amount is not None]
                found.append({
                    'label': f'{b.property.name} — {b.unit.label}' if b.unit_id else b.property.name,
                    'pair': pair,
                    'likely_real': paid[0] if len(paid) == 1 else None,
                })
            if latest is None or _local_date(b.check_out) > _local_date(latest.check_out):
                latest = b
    found.sort(key=lambda c: _local_date(c['pair'][1].check_in))
    return found


def paid_not_on_calendar(today=None):
    """Upcoming reservations we are being paid for that a connected calendar
    does not show: valid income, but no cleaning is scheduled for them. Worth a
    look because the calendar may be missing a real stay."""
    today = today or timezone.localdate()
    rows = coverage_service.payment_only(Booking.objects.filter(
        status=Booking.Status.ACTIVE, property__is_general=False, check_out__gte=_start_of(today),
    )).select_related('property', 'unit').order_by('check_in')
    return list(rows)


def counts(today=None):
    """(overlapping pairs, upcoming reservations with no payout)."""
    return len(conflicts(today)), len(unpaid_upcoming(today))
