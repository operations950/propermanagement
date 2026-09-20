"""In-house ("offline") reservations: stays booked directly, by phone or
word of mouth, that never appear on Airbnb or VRBO. They are ordinary
Booking rows with source 'manual', so everything downstream — the turnover
visit, the cleaner's link and calendar invite, the Today board, occupancy —
treats them exactly like a platform booking.

Only manual reservations can be created, edited or cancelled here; a platform
reservation changes on the platform (and via its calendar or CSV)."""
import uuid
from datetime import datetime, time

from django.db import transaction
from django.utils import timezone

from core.models import Property
from ..models import Booking, Visit, VisitType
from .bookings import (
    DEFAULT_CHECK_IN_TIME, DEFAULT_CHECK_OUT_TIME, TURNOVER_SLUG, _find_next_booking,
    _refresh_next_bookings_for_property,
)
from .checklist import create_visit
from .feeds import eligible_properties

FINISHED_VISIT_STATUSES = (Visit.Status.SUBMITTED, Visit.Status.VERIFIED, Visit.Status.CANCELLED)


class ReservationError(ValueError):
    """A reservation that can't be saved; the message is written for staff."""


def default_times(prop):
    return prop.default_check_in_time or DEFAULT_CHECK_IN_TIME, prop.default_check_out_time or DEFAULT_CHECK_OUT_TIME


def listing_options():
    """[(value, label, property, unit)] for every rentable listing: 'p-<id>'
    for a property with no units, 'u-<id>' for each unit of a building."""
    options = []
    for prop in eligible_properties():
        units = [u for u in prop.units.all() if u.is_active]
        if units:
            options += [(f'u-{u.pk}', f'{prop.name} — {u.label}', prop, u) for u in units]
        else:
            options.append((f'p-{prop.pk}', prop.name, prop, None))
    return options


def resolve_listing(value):
    """(property, unit) for a listing_options value, or None."""
    for option_value, _label, prop, unit in listing_options():
        if option_value == value:
            return prop, unit
    return None


def _aware(day, clock):
    return timezone.make_aware(datetime.combine(day, clock))


def find_conflict(prop, unit, check_in_date, check_out_date, exclude_pk=None):
    """An active reservation at the same place whose nights overlap the
    requested ones. A same-day turnover (one leaves the morning the next
    arrives) is NOT an overlap."""
    others = Booking.objects.filter(property=prop, unit=unit, status=Booking.Status.ACTIVE)
    if exclude_pk:
        others = others.exclude(pk=exclude_pk)
    for other in others:
        their_in = timezone.localtime(other.check_in).date()
        their_out = timezone.localtime(other.check_out).date()
        if their_in < check_out_date and their_out > check_in_date:
            return other
    return None


def _describe(booking):
    a, b = timezone.localtime(booking.check_in), timezone.localtime(booking.check_out)
    return f'{booking.get_source_display()} reservation {a:%b} {a.day}–{b:%b} {b.day}'


def _validate(prop, unit, guest_name, check_in_date, check_out_date, exclude_pk=None):
    if not (guest_name or '').strip():
        raise ReservationError("Enter the guest's name.")
    if check_in_date is None or check_out_date is None:
        raise ReservationError('Enter the check-in and check-out dates.')
    if check_out_date <= check_in_date:
        raise ReservationError('Check-out has to be after check-in.')
    conflict = find_conflict(prop, unit, check_in_date, check_out_date, exclude_pk)
    if conflict:
        raise ReservationError(f'Those dates overlap an existing {_describe(conflict)} at this listing.')


def _amounts(lodging_total, cleaning_fee):
    gross = None
    if lodging_total is not None:
        gross = lodging_total + (cleaning_fee or 0)
    return {'gross_amount': gross, 'cleaning_fee': cleaning_fee}


def _visit_for(booking):
    """Make sure a live reservation has its turnover visit (never for a
    general placeholder), scheduled on the checkout day."""
    if booking.status != Booking.Status.ACTIVE or booking.property.is_general:
        return
    if booking.visits.exclude(status=Visit.Status.CANCELLED).exists():
        return
    turnover = VisitType.objects.filter(slug=TURNOVER_SLUG, is_active=True).first()
    if turnover is None:
        return
    next_booking = _find_next_booking(booking.property, booking.check_out, exclude_pk=booking.pk, unit=booking.unit)
    create_visit(
        booking.property, turnover, unit=booking.unit, booking=booking, next_booking=next_booking,
        scheduled_date=timezone.localtime(booking.check_out).date(),
        ready_by=next_booking.check_in if next_booking else None,
    )


@transaction.atomic
def create_reservation(prop, unit, guest_name, check_in_date, check_out_date, check_in_time=None, check_out_time=None,
                       phone='', email='', notes='', lodging_total=None, cleaning_fee=None, user=None):
    _validate(prop, unit, guest_name, check_in_date, check_out_date)
    default_in, default_out = default_times(prop)
    booking = Booking.objects.create(
        property=prop, unit=unit, source=Booking.Source.MANUAL, external_uid=f'manual-{uuid.uuid4().hex[:12]}',
        guest_name=guest_name.strip(), guest_phone=(phone or '').strip(), guest_email=(email or '').strip(),
        notes=(notes or '').strip(),
        check_in=_aware(check_in_date, check_in_time or default_in), check_out=_aware(check_out_date, check_out_time or default_out),
        created_by=user, amount_source='entered by hand' if lodging_total is not None else '',
        **_amounts(lodging_total, cleaning_fee),
    )
    _visit_for(booking)
    _refresh_next_bookings_for_property(prop)   # the stay before this one now has a next guest
    return booking


@transaction.atomic
def update_reservation(booking, guest_name, check_in_date, check_out_date, check_in_time=None, check_out_time=None,
                       phone='', email='', notes='', lodging_total=None, cleaning_fee=None):
    if booking.source != Booking.Source.MANUAL:
        raise ReservationError('Only in-house reservations can be edited here — change a platform reservation on the platform.')
    if booking.status != Booking.Status.ACTIVE:
        raise ReservationError('That reservation is cancelled.')
    _validate(booking.property, booking.unit, guest_name, check_in_date, check_out_date, exclude_pk=booking.pk)
    default_in, default_out = default_times(booking.property)
    booking.guest_name, booking.guest_phone, booking.guest_email = guest_name.strip(), (phone or '').strip(), (email or '').strip()
    booking.notes = (notes or '').strip()
    booking.check_in = _aware(check_in_date, check_in_time or default_in)
    booking.check_out = _aware(check_out_date, check_out_time or default_out)
    booking.gross_amount, booking.cleaning_fee = _amounts(lodging_total, cleaning_fee).values()
    booking.amount_source = 'entered by hand' if lodging_total is not None else ''
    booking.save()

    # Move its turnover visit(s) to the new checkout day; the calendar event follows.
    for visit in booking.visits.exclude(status__in=FINISHED_VISIT_STATUSES):
        visit.scheduled_date = check_out_date
        visit.save(update_fields=['scheduled_date'])
    _visit_for(booking)
    _refresh_next_bookings_for_property(booking.property)
    return booking


@transaction.atomic
def cancel_reservation(booking):
    if booking.source != Booking.Source.MANUAL:
        raise ReservationError('Only in-house reservations can be cancelled here — cancel a platform reservation on the platform.')
    if booking.status != Booking.Status.ACTIVE:
        return booking
    booking.status = Booking.Status.CANCELLED
    booking.save(update_fields=['status'])
    for visit in booking.visits.exclude(status__in=FINISHED_VISIT_STATUSES):
        visit.status = Visit.Status.CANCELLED
        visit.save(update_fields=['status'])      # the signal deletes its calendar event
    _refresh_next_bookings_for_property(booking.property)
    return booking
