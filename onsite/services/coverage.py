"""Which bookings are OPERATIONAL — real for cleanings and the Rentals board.

The synced platform calendars are the single source of truth for who is
actually checking in and out. The payment reports (CSV imports) are the single
source of truth for money. A reservation that only a payment report knows about
is a valid financial record, but where a listing's calendar is connected and
healthy it never gets a cleaning and doesn't appear on the board: it could be a
payment for a cancellation or an old reservation.

"Covered" is per listing and platform: an active, connected, recently-polled
calendar for that (property, unit, source). Where a listing has no working
calendar yet, its imported reservations stay operational (the way they always
were) until the calendar is connected — otherwise those rentals would lose their
cleanings the moment this rule arrived. In-house (manual) reservations have no
platform calendar and are always operational."""
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from ..models import Booking, BookingFeed

# A calendar that hasn't been read successfully for this long no longer
# vouches for the listing.
FRESH_FOR = timedelta(days=2)


def covered_keys(now=None):
    """{(property_id, unit_id, source)} for every listing whose calendar is
    connected and has been read successfully recently."""
    cutoff = (now or timezone.now()) - FRESH_FOR
    return {
        (f.property_id, f.unit_id, f.source)
        for f in BookingFeed.objects.filter(is_active=True, not_listed=False, last_success_at__gte=cutoff)
    }


def is_operational(booking, covered=None):
    """True if this booking counts for cleanings and the board. Accepts a
    Booking or a dict with the same keys (property_id, unit_id, source,
    on_calendar)."""
    covered = covered_keys() if covered is None else covered
    get = booking.get if isinstance(booking, dict) else (lambda k: getattr(booking, k))
    if get('source') == Booking.Source.MANUAL or get('on_calendar'):
        return True
    return (get('property_id'), get('unit_id'), get('source')) not in covered


def operational(qs, covered=None):
    """The bookings in `qs` that are operational."""
    covered = covered_keys() if covered is None else covered
    hidden = Q()
    for property_id, unit_id, source in covered:
        hidden |= Q(property_id=property_id, unit_id=unit_id, source=source)
    if not covered:
        return qs
    return qs.exclude(hidden & Q(on_calendar=False) & ~Q(source=Booking.Source.MANUAL))


def payment_only(qs, covered=None):
    """The bookings in `qs` that are financial records only: known from a
    payment report, not on a connected calendar."""
    covered = covered_keys() if covered is None else covered
    hidden = Q()
    for property_id, unit_id, source in covered:
        hidden |= Q(property_id=property_id, unit_id=unit_id, source=source)
    if not covered:
        return qs.none()
    return qs.filter(hidden & Q(on_calendar=False) & ~Q(source=Booking.Source.MANUAL))
