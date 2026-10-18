"""Checkouts that have no cleaning scheduled. A checkout is covered by ANY scheduled turnover cleaning at that property (and that unit, for a building) on the
checkout day, whether the system made it from the calendar or a person set it up by hand: only a visit attached to the booking itself used to count,
so a hand-made one left a false "no visit scheduled" warning."""
from django.utils import timezone

from ..models import Visit
from .bookings import TURNOVER_SLUG

_NOT_LIVE = (Visit.Status.CANCELLED, Visit.Status.SKIPPED)


def without_visit(bookings):
    """The given bookings (each with its visits prefetched) that nothing covers: no live visit made for the booking, and no live visit at the same property
    and unit (or with no unit set) on its checkout day."""
    bookings = list(bookings)
    if not bookings:
        return []
    days = {timezone.localtime(b.check_out).date() for b in bookings}
    by_day = {}
    for property_id, unit_id, day in (Visit.objects.filter(scheduled_date__in=days, property_id__in={b.property_id for b in bookings}, visit_type__slug=TURNOVER_SLUG)
                                      .exclude(status__in=_NOT_LIVE).values_list('property_id', 'unit_id', 'scheduled_date')):
        by_day.setdefault((property_id, day), set()).add(unit_id)
    missing = []
    for b in bookings:
        if any(v.status not in _NOT_LIVE for v in b.visits.all()):
            continue
        units = by_day.get((b.property_id, timezone.localtime(b.check_out).date()), set())
        if units and (b.unit_id in units or None in units):
            continue
        missing.append(b)
    return missing
