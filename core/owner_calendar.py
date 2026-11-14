"""The owner portal's calendar: which nights of a short-term rental are taken, month by month, for the current month and the eleven after it.

It reads the same nights the On-Site calendars and the performance numbers count (onsite.services.performance._collect_units): a stay a platform calendar shows,
an in-house stay, and a payment-report-only stay only for nights already past. On top of that it adds the nights a platform calendar shows as unavailable that
no stay explains. Each night is one of three things for the owner: "Booked" (a platform reservation), "Owner stay" (an in-house reservation: how an owner's own stay is entered) or
"Blocked" (unavailable on a calendar, nothing else said). Nothing about the guest ever leaves this module: no name, no phone, no money."""
import calendar as calendar_module
from datetime import date, timedelta

from django.utils import timezone

BOOKED, OWNER, BLOCKED = 'booked', 'owner', 'blocked'
LABELS = {BOOKED: 'Booked', OWNER: 'Owner stay', BLOCKED: 'Blocked'}
MONTHS_AHEAD = 11                      # the current month and this many after it
WEEKDAYS = ('Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat')


def first_of(day):
    return day.replace(day=1)


def add_months(first, n):
    index = first.year * 12 + first.month - 1 + n
    return date(index // 12, index % 12 + 1, 1)


def month_range(today=None):
    """(first month, last month) an owner may look at."""
    first = first_of(today or timezone.localdate())
    return first, add_months(first, MONTHS_AHEAD)


def clamp_month(raw, today=None):
    """The month asked for ('YYYY-MM'), pulled into the range an owner may see; the current month when it is missing or not a month."""
    first, last = month_range(today)
    try:
        asked = date(int(raw[:4]), int(raw[5:7]), 1) if raw and len(raw) >= 7 and raw[4] == '-' else first
    except ValueError:
        asked = first
    return min(max(asked, first), last)


def _night_status(prop, today):
    """({unit id (or None): {night: status}}, {unit id (or None): Unit or None}) for the whole property."""
    from onsite.models import Booking, CalendarBlock
    from onsite.services import performance

    units, _unattributed = performance._collect_units([prop], today)
    out = {}
    for (_pid, uid), u in units.items():
        nights = {}
        for booking, booked_nights in u.bars:
            status = OWNER if booking.source == Booking.Source.MANUAL else BOOKED
            for n in booked_nights:
                if nights.get(n) != OWNER:
                    nights[n] = status
        out[uid] = nights
    blocks = list(CalendarBlock.objects.filter(property=prop, end__gt=today - timedelta(days=45)))
    for blk in blocks:
        if blk.unit_id is not None and blk.unit_id not in out:
            continue                                                         # a unit no longer in use
        targets = [blk.unit_id] if blk.unit_id in out else list(out)         # a block on the whole listing covers every unit under it
        day = blk.start
        while day < blk.end:
            for uid in targets:
                out[uid].setdefault(day, BLOCKED)
            day += timedelta(days=1)
    return out, {uid: u.unit for (_p, uid), u in units.items()}


def build_month(prop, month, today=None):
    """One month of a rental as calendars, a grid per unit. Each cell: its date, whether it is in the month, today, past, and the night's status (or '')."""
    today = today or timezone.localdate()
    month = first_of(month)
    statuses, unit_objs = _night_status(prop, today)
    days_in = calendar_module.monthrange(month.year, month.month)[1]
    grid_start = month - timedelta(days=(month.weekday() + 1) % 7)                     # Sunday first
    grid_end = month + timedelta(days=days_in)
    grids = []
    for uid in sorted(statuses, key=lambda k: (unit_objs[k].label if unit_objs.get(k) else '')):
        unit = unit_objs.get(uid)
        weeks, counts = [], {BOOKED: 0, OWNER: 0, BLOCKED: 0}
        day = grid_start
        while day < grid_end:
            cells = []
            for _ in range(7):
                in_month = month <= day < month + timedelta(days=days_in)
                status = statuses[uid].get(day, '') if in_month else ''
                if status:
                    counts[status] += 1
                cells.append({'date': day, 'num': day.day, 'in_month': in_month, 'today': day == today, 'past': day < today, 'status': status, 'label': LABELS.get(status, '')})
                day += timedelta(days=1)
            weeks.append(cells)
        grids.append({'label': unit.label if unit else '', 'weeks': weeks, 'counts': counts, 'open': days_in - sum(counts.values())})
    first, last = month_range(today)
    return {
        'property': prop, 'month': month, 'grids': grids, 'weekdays': WEEKDAYS, 'labels': LABELS, 'multi': len(grids) > 1,
        'previous': add_months(month, -1) if month > first else None, 'next': add_months(month, 1) if month < last else None,
        'months': [add_months(first, i) for i in range(MONTHS_AHEAD + 1)], 'today': today,
    }
