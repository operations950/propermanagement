"""The per-property calendar: a timeline like a host calendar. One row per unit,
one column per day; each reservation is a bar running from its check-in to its
check-out (starting and ending mid-day, so a same-day turnover shows as two
bars meeting), and the blank stretches between reservations are marked with how
many nights they are.

It draws exactly the nights the performance numbers count (see
performance._Unit.add): a stay only a payment report knows about, on a listing
with a connected calendar, is not drawn for future dates — the calendar decides
what is booked ahead. Cancelled reservations are never drawn.

Positions are in "day units" from the window start: a night on day d occupies
[d, d+1), and a stay is drawn from check-in day + 0.5 to check-out day + 0.5."""
from datetime import timedelta

from django.utils import timezone

from ..models import Booking
from .performance import SHORT_GAP_NIGHTS, _collect_units, _local_date

WINDOWS = (30, 60, 90, 180)
DEFAULT_DAYS = 30


def _pct(x, total):
    return round(max(0.0, min(x, total)) / total * 100, 3)


def _lanes(bars):
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
        bar['top'] = 4 + bar['lane'] * 34
    return max(len(ends), 1)


def build_property_calendar(prop, start=None, days=DEFAULT_DAYS, today=None):
    today = today or timezone.localdate()
    days = days if days in WINDOWS else DEFAULT_DAYS
    start = start or today
    end = start + timedelta(days=days)
    units, _ = _collect_units([prop], today)
    ordered = sorted(units.values(), key=lambda u: (u.unit.label if u.unit else ''))

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

    rows, total_booked, total_nights, listing_bookings = [], 0, 0, []
    for u in ordered:
        window = [start + timedelta(days=i) for i in range(days)]
        bars = []
        for booking, nights in u.bars:
            in_window = [n for n in nights if start <= n < end]
            if not in_window:
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
            listing_bookings.append({'unit': u, 'booking': booking, 'bar': bars[-1]})
        lane_count = _lanes(bars)

        booked = u.booked
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
        in_window_nights = sum(1 for n in window if n in booked)
        has_data = u.counted and (u.data_start is None or end > u.data_start)
        upcoming_after = min((b for b, _ in u.bars if _local_date(b.check_in) >= end and b.status == Booking.Status.ACTIVE), key=lambda b: b.check_in, default=None)
        rows.append({
            'label': u.unit.label if u.unit else prop.name, 'has_units': u.unit is not None, 'bars': bars, 'blanks': blanks,
            'lanes': lane_count, 'height': 34 * lane_count + 8,
            'booked': in_window_nights, 'nights': days, 'occupancy': in_window_nights / days * 100 if has_data else None,
            'next_after': upcoming_after, 'counted': u.counted,
        })
        if u.counted:
            total_booked += in_window_nights
            total_nights += days

    listing_bookings.sort(key=lambda e: (e['booking'].check_in, e['unit'].unit.label if e['unit'].unit else ''))
    today_x = (today - start).days
    return {
        'property': prop, 'start': start, 'end': end, 'days': days, 'windows': WINDOWS, 'today': today,
        'header_days': header_days, 'months': months, 'rows': rows,
        'today_left': _pct(today_x + 0.5, days) if 0 <= today_x < days else None,
        'occupancy': total_booked / total_nights * 100 if total_nights else None,
        'booked': total_booked, 'available': total_nights,
        'reservations': listing_bookings,
        'previous': start - timedelta(days=days), 'next': start + timedelta(days=days),
        'min_width': max(days * 30, 300),
    }
