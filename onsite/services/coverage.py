"""Which bookings are OPERATIONAL — real for cleanings, occupancy and the Rentals board.

The synced platform calendars are the single source of truth for who is
actually checking in and out. The payment reports (CSV imports) are the single
source of truth for money, and only for money. A reservation that only a payment
report knows about is a valid financial record, but it is never a stay: no
cleaning, no board row, no occupancy from today on, whether or not its listing
has a calendar. A listing with no calendar link is a problem to fix (it is
flagged everywhere), not a reason to let files stand in for the calendar.
In-house (manual) reservations are the one exception: they are entered by hand,
have no platform calendar, and are always operational.

"Covered" is still worked out per listing and platform (an active, connected,
recently-polled calendar for that (property, unit, source)); it says whether a
calendar is vouching for a listing, not whether files may count."""
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
    get = booking.get if isinstance(booking, dict) else (lambda k: getattr(booking, k))
    return bool(get('source') == Booking.Source.MANUAL or get('on_calendar'))


def operational(qs, covered=None):
    """The bookings in `qs` that are operational: on a calendar, or entered by hand."""
    return qs.filter(Q(on_calendar=True) | Q(source=Booking.Source.MANUAL))


def payment_only(qs, covered=None):
    """The bookings in `qs` that are financial records only: known from a
    payment report, not on a calendar."""
    return qs.filter(on_calendar=False).exclude(source=Booking.Source.MANUAL)
