"""Calendars of the rentals, drawn from the same nights the performance numbers
count (see performance._Unit.add): a stay that only a payment report knows about,
on a listing with a connected calendar, is not drawn for future dates (the
calendar decides what is booked ahead), and cancelled reservations never are.

Two layouts share one way of drawing a reservation — a bar from check-in to
check-out, starting and ending mid-day so a same-day turnover shows as two bars
meeting:

  * build_timeline(props, ...)    one row per unit across MANY properties, a
                                  column per day: the multi-calendar for scanning
                                  the whole portfolio, with the blank stretches
                                  between stays counted in nights;
  * build_month_grids(prop, ...)  one property (a grid per unit) as ordinary month
                                  calendars: seven columns, a row per week, the
                                  reservations running across them.

Positions are in "day units" from the drawn window's start: a night on day d
occupies [d, d+1), and a stay is drawn from check-in day + 0.5 to check-out day
+ 0.5, clipped to the window."""
from datetime import date, timedelta

from django.utils import timezone

from ..models import Booking
from .performance import SHORT_GAP_NIGHTS, _collect_units, _local_date

WINDOWS = (30, 60, 90, 180)
DEFAULT_DAYS = 30
MONTH_COUNTS = (1, 2, 3, 6)
WEEKDAYS = ('Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat')


def _pct(x, total):
    return round(max(0.0, min(x, total)) / total * 100, 3)


def _lanes(bars, top_base=4, step=34):
    """Rows for bars that overlap in time (bad data, or a calendar and an
    in-house stay); everything else sits in lane 0."""
    ends = []
    for bar in sorted(bars, key=lambda b: b['x0']):
        for i, end in enumerate(ends):
            if bar['x0'] >= end - 1e-9:
                bar['lane'] = i
                ends[i] = bar['x1']
                break
        else:
            bar['lane'] = len(ends)
            ends.append(bar['x1'])
        bar['top'] = top_base + bar['lane'] * step
    return max(len(ends), 1)


def _bars(unit, start, days, top_base=4, step=34):
    """The bars of one unit's reservations that touch [start, start+days)."""
    end = start + timedelta(days=days)
    bars = []
    for booking, nights in unit.bars:
        if not any(start <= n < end for n in nights):
            continue
        first, last = min(nights), max(nights)
        x0 = 0.0 if first < start else (first - start).days + 0.5
        x1 = float(days) if last + timedelta(days=1) > end else (last + timedelta(days=1) - start).days + 0.5
        x1 = min(x1, float(days))
        bars.append({
            'booking': booking, 'x0': x0, 'x1': x1, 'left': _pct(x0, days), 'width': _pct(x1 - x0, days),
            'nights': booking.nights(), 'source': booking.source, 'source_label': booking.get_source_display(),
            'starts_before': first < start, 'ends_after': last + timedelta(days=1) > end,
            'guest': booking.guest_name or 'Reservation',
        })
    return bars, _lanes(bars, top_base, step)


def _blanks(unit, start, days):
    """Runs of unbooked nights in the window, marked as a gap when they sit
    between two stays."""
    booked = unit.booked
    window = [start + timedelta(days=i) for i in range(days)]
    end = start + timedelta(days=days)
    blanks, run = [], []
    for n in window + [None]:
        if n is not None and n not in booked:
            run.append(n)
            continue
        if run:
            a, b = run[0], run[-1] + timedelta(days=1)
            x0 = 0.0 if (a == start and (a - timedelta(days=1)) not in booked) else (a - start).days + 0.5
            x1 = float(days) if (b >= end and b not in booked) else (b - start).days + 0.5
            x1 = min(x1, float(days))
            length = (b - a).days
            between = bool(booked) and min(booked) < a and max(booked) >= b
            blanks.append({
                'left': _pct(x0, days), 'width': _pct(x1 - x0, days), 'nights': length, 'between': between,
                'short': between and length <= SHORT_GAP_NIGHTS, 'roomy': (x1 - x0) >= 2.4,
            })
            run = []
    return blanks


def _header(start, days, today):
    header_days = []
    for i in range(days):
        d = start + timedelta(days=i)
        header_days.append({
            'date': d, 'num': d.day, 'dow': f'{d:%a}'[0], 'weekend': d.weekday() >= 5, 'today': d == today,
            'left': _pct(i, days), 'width': _pct(1, days),
        })
    months, i = [], 0
    while i < days:
        d = start + timedelta(days=i)
        span = 1
        while i + span < days and (start + timedelta(days=i + span)).month == d.month:
            span += 1
        months.append({'label': f'{d:%B %Y}' if span > 6 else f'{d:%b}', 'left': _pct(i, days), 'width': _pct(span, days)})
        i += span
    return header_days, months


def _next_after(unit, end):
    return min((b for b, _ in unit.bars if _local_date(b.check_in) >= end and b.status == Booking.Status.ACTIVE),
               key=lambda b: b.check_in, default=None)


# --- the multi-property timeline ------------------------------------------------------

def build_timeline(props, start=None, days=DEFAULT_DAYS, today=None):
    """One row per unit (a single row for a property without units) across all of
    `props`, in property then unit order."""
    today = today or timezone.localdate()
    days = days if days in WINDOWS else DEFAULT_DAYS
    start = start or today
    end = start + timedelta(days=days)
    props = sorted(props, key=lambda p: p.name.lower())
    units, _ = _collect_units(props, today)
    header_days, months = _header(start, days, today)
    window = [start + timedelta(days=i) for i in range(days)]

    rows, reservations, total_booked, total_nights = [], [], 0, 0
    for prop in props:
        mine = sorted((u for (pid, _uid), u in units.items() if pid == prop.pk), key=lambda u: (u.unit.label if u.unit else ''))
        for index, u in enumerate(mine):
            bars, lane_count = _bars(u, start, days)
            booked_here = sum(1 for n in window if n in u.booked)
            has_data = u.counted and (u.data_start is None or end > u.data_start)
            rows.append({
                'property': prop, 'group': prop.name, 'first_in_group': index == 0, 'group_size': len(mine),
                'label': u.unit.label if u.unit else prop.name, 'has_units': u.unit is not None,
                'bars': bars, 'blanks': _blanks(u, start, days), 'lanes': lane_count, 'height': 34 * lane_count + 8,
                'booked': booked_here, 'nights': days, 'occupancy': booked_here / days * 100 if has_data else None,
                'next_after': _next_after(u, end), 'counted': u.counted,
            })
            for bar in bars:
                reservations.append({'unit': u, 'property': prop, 'booking': bar['booking'], 'bar': bar})
            if u.counted:
                total_booked += booked_here
                total_nights += days
    reservations.sort(key=lambda e: (e['booking'].check_in, e['property'].name, e['unit'].unit.label if e['unit'].unit else ''))
    today_x = (today - start).days
    return {
        'start': start, 'end': end, 'days': days, 'windows': WINDOWS, 'today': today,
        'header_days': header_days, 'months': months, 'rows': rows,
        'today_left': _pct(today_x + 0.5, days) if 0 <= today_x < days else None,
        'occupancy': total_booked / total_nights * 100 if total_nights else None,
        'booked': total_booked, 'available': total_nights, 'reservations': reservations,
        'previous': start - timedelta(days=days), 'next': start + timedelta(days=days),
        'min_width': max(days * 30, 300),
    }


def build_property_calendar(prop, start=None, days=DEFAULT_DAYS, today=None):
    """The timeline for a single property."""
    cal = build_timeline([prop], start=start, days=days, today=today)
    cal['property'] = prop
    return cal


# --- one property as month calendars ----------------------------------------------------

def _next_month(day):
    return date(day.year + (day.month == 12), day.month % 12 + 1, 1)


def _month_firsts(first, count):
    out, d = [], first
    for _ in range(count):
        out.append(d)
        d = _next_month(d)
    return out


def months_covering(start, days):
    """How many consecutive months, from the month of `start`, it takes to cover
    `days` days from `start`."""
    last = start + timedelta(days=days - 1)
    return (last.year - start.year) * 12 + last.month - start.month + 1


def build_month_grids(prop, first_month=None, months=1, today=None, window=None, unit_id=None):
    """`months` ordinary month calendars per unit, starting at the month of
    `first_month`: seven columns (Sunday first), a row per week, each reservation
    a bar running across the days it covers. `window` = (first_day, days) dims the
    days outside it and is what the headline occupancy is measured over."""
    today = today or timezone.localdate()
    first = (first_month or today).replace(day=1)
    months = months if months in MONTH_COUNTS else 1
    units, _ = _collect_units([prop], today)
    ordered = sorted(units.values(), key=lambda u: (u.unit.label if u.unit else ''))
    if unit_id:
        ordered = [u for u in ordered if (u.unit.pk if u.unit else None) == unit_id]
    win_start = win_end = None
    if window:
        win_start = window[0]
        win_end = win_start + timedelta(days=window[1])
    firsts = _month_firsts(first, months)
    range_end = _next_month(firsts[-1])

    grids, reservations, seen = [], [], set()
    for u in ordered:
        month_blocks = []
        for m in firsts:
            nxt = _next_month(m)
            grid_start = m - timedelta(days=(m.weekday() + 1) % 7)
            weeks = []
            for w in range(-(-(nxt - grid_start).days // 7)):
                ws = grid_start + timedelta(days=7 * w)
                bars, lanes = _bars(u, ws, 7, top_base=26, step=25)
                cells = []
                for i in range(7):
                    d = ws + timedelta(days=i)
                    cells.append({
                        'date': d, 'num': d.day, 'in_month': m <= d < nxt, 'today': d == today,
                        'dim': bool(window) and not (win_start <= d < win_end), 'booked': d in u.booked,
                        'weekend': d.weekday() >= 5, 'left': _pct(i, 7), 'width': _pct(1, 7),
                    })
                weeks.append({'cells': cells, 'bars': bars, 'height': max(86, 26 + lanes * 25 + 6)})
            in_month = [m + timedelta(days=i) for i in range((nxt - m).days)]
            booked = sum(1 for d in in_month if d in u.booked)
            has_data = u.counted and (u.data_start is None or nxt > u.data_start)
            month_blocks.append({
                'first': m, 'label': f'{m:%B %Y}', 'weeks': weeks, 'booked': booked, 'days': len(in_month),
                'occupancy': booked / len(in_month) * 100 if has_data else None,
            })
        for booking, nights in u.bars:
            if booking.pk not in seen and any(first <= n < range_end for n in nights):
                seen.add(booking.pk)
                reservations.append({'unit': u, 'booking': booking})
        grids.append({'unit': u.unit, 'label': u.unit.label if u.unit else prop.name, 'months': month_blocks, 'counted': u.counted})
    reservations.sort(key=lambda e: (e['booking'].check_in, e['unit'].unit.label if e['unit'].unit else ''))

    headline = None
    if window:
        timeline = build_timeline([prop], start=win_start, days=window[1], today=today)
        headline = {'occupancy': timeline['occupancy'], 'booked': timeline['booked'], 'available': timeline['available']}
    all_units = sorted((u for u in units.values() if u.unit), key=lambda u: u.unit.label)
    return {
        'property': prop, 'first_month': first, 'months': months, 'grids': grids, 'weekdays': WEEKDAYS,
        'window': window, 'window_end': win_end, 'headline': headline, 'reservations': reservations,
        'previous_month': (first - timedelta(days=1)).replace(day=1), 'next_month': _next_month(first), 'today': today,
        'units': [{'pk': u.unit.pk, 'label': u.unit.label} for u in all_units], 'unit_id': unit_id,
    }
