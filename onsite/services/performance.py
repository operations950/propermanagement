"""How the short-term rentals are performing, computed from the reservations
themselves (nothing is stored): occupancy, vacancy gaps, forward occupancy,
length of stay, cancellations, and — where the uploaded reports carried
amounts — average nightly rate and revenue per available night.

Definitions (also shown on the page):
  night          the date a guest sleeps: check-in date up to, not including,
                 check-out date. Times of day are ignored.
  available      every night of every counted unit in the window. A unit is
                 counted once it has any reservation on record or a connected
                 calendar; a unit with neither has no data and is listed as
                 not counted rather than dragging every number to zero.
  trailing       windows that end yesterday: a night before a unit's first
                 recorded reservation is not counted as available (the app
                 simply wasn't watching it yet).
  vacancy gap    unbooked nights sitting BETWEEN two reservations at the same
                 unit — the hardest to sell. "Short" = 1-2 nights. Unbooked
                 nights with no later reservation are "open", not a gap. A gap
                 never starts before today (one already under way is counted
                 from today) and is worked out from the nights actually
                 booked, so overlapping reservations can't invent one.
  ADR            revenue / nights sold, over reservations whose report gave
                 an amount. Revenue is the PAYOUT — what the platform pays for
                 the stay, the top line (see Booking.lodging_revenue).
  RevPAR         ADR x occupancy — revenue per available night.
Cancelled reservations never count as booked nights."""
from datetime import date, timedelta

from django.utils import timezone

from ..models import Booking, BookingFeed
from . import coverage
from .feeds import eligible_properties

WINDOWS = (30, 60, 90)
SHORT_GAP_NIGHTS = 2


def _local_date(dt):
    return timezone.localtime(dt).date()


def _nights(booking):
    start, end = _local_date(booking.check_in), _local_date(booking.check_out)
    return [start + timedelta(days=i) for i in range((end - start).days)]


def _pct(booked, available):
    return None if not available else booked / available * 100


class _Unit:
    def __init__(self, prop, unit):
        self.prop, self.unit = prop, unit
        self.label = f'{prop.name} — {unit.label}' if unit else prop.name
        self.active, self.all = [], []
        self.active_ops, self.calendar = [], []
        self.bars = []      # (booking, the nights of it that count) — what the calendar draws
        self.booked = set()
        self.data_start = None
        self.connected = False

    def add(self, booking, operational=True, today=None):
        """`operational` is False for a stay only a payment report knows about, on a
        listing whose calendar is connected: it is real for the money and for the
        past, but it says nothing about the future (the calendar does), so its
        nights from `today` on are not counted as booked."""
        self.all.append(booking)
        start = _local_date(booking.check_in)
        if self.data_start is None or start < self.data_start:
            self.data_start = start
        if booking.status == Booking.Status.ACTIVE:
            self.active.append(booking)
            nights = _nights(booking)
            if operational:
                self.active_ops.append(booking)
            elif today is not None:
                nights = [n for n in nights if n < today]
            if booking.on_calendar or booking.source == Booking.Source.MANUAL:
                self.calendar.append(booking)
            if nights:
                self.bars.append((booking, nights))
            self.booked.update(nights)

    @property
    def counted(self):
        return bool(self.all) or self.connected


def _collect_units(props, today=None):
    """{(property_id, unit_id): _Unit} for the given properties, loaded with
    every booking on record, plus how many bookings pointed at no known unit."""
    units = {}
    for prop in props:
        active_units = [u for u in prop.units.all() if u.is_active]
        for unit in (active_units or [None]):
            units[(prop.pk, unit.pk if unit else None)] = _Unit(prop, unit)

    for feed in BookingFeed.objects.filter(is_active=True, not_listed=False, property_id__in=[p.pk for p in props]):
        holder = units.get((feed.property_id, feed.unit_id))
        if holder:
            holder.connected = True
    unattributed = 0
    covered = coverage.covered_keys()
    for booking in Booking.objects.filter(property_id__in=[p.pk for p in props]).select_related('property', 'unit'):
        holder = units.get((booking.property_id, booking.unit_id))
        if holder is None:
            unattributed += 1
            continue
        holder.add(booking, coverage.is_operational(booking, covered), today)
    return units, unattributed


def _overlaps(u):
    """Pairs of reservations that share a night at one unit, counting only what
    the synced calendars (and in-house bookings) show. A unit can't host two
    guests at once, so a genuine overlap there is a real problem (say, two
    listings feeding one unit). Overlaps between stays known only from payment
    reports are not reported: those files legitimately contain cancelled,
    replaced and old reservations."""
    found, latest = [], None
    for b in sorted(u.calendar, key=lambda b: _local_date(b.check_in)):
        if latest is not None and _local_date(b.check_in) < _local_date(latest.check_out):
            found.append({'label': u.label, 'first': latest, 'second': b, 'pair': [latest, b]})
        if latest is None or _local_date(b.check_out) > _local_date(latest.check_out):
            latest = b
    return found


def build_performance(today=None, property_id=None):
    today = today or timezone.localdate()
    props = list(eligible_properties())
    if property_id:
        props = [p for p in props if p.pk == property_id]
    units, unattributed = _collect_units(props, today)

    counted = [u for u in units.values() if u.counted]
    excluded = sorted(u.label for u in units.values() if not u.counted)

    def occupancy(start, end, trailing):
        booked = available = 0
        for u in counted:
            first = max(start, u.data_start) if (trailing and u.data_start) else start
            if first >= end:
                continue
            days = (end - first).days
            available += days
            booked += sum(1 for i in range(days) if (first + timedelta(days=i)) in u.booked)
        return booked, available

    # --- looking ahead: occupancy and vacancy gaps for the next 30/60/90 nights
    def gaps_for(u):
        """Runs of unbooked nights between two booked ones, from the booked
        nights themselves (so overlapping or nested reservations can't invent
        a gap), and never starting before today: a gap that is already
        under way is counted from today."""
        nights = sorted(u.booked)
        found = []
        for before, after in zip(nights, nights[1:]):
            start, end = max(before + timedelta(days=1), today), after
            if end > start:
                found.append({'label': u.label, 'start': start, 'end': end, 'length': (end - start).days})
        return found

    all_gaps = [g for u in counted for g in gaps_for(u)]
    ahead = []
    for n in WINDOWS:
        end = today + timedelta(days=n)
        booked, available = occupancy(today, end, trailing=False)
        in_window = [g for g in all_gaps if g['end'] > today and g['start'] < end]
        gap_nights = sum((min(g['end'], end) - max(g['start'], today)).days for g in in_window)
        ahead.append({
            'days': n, 'booked': booked, 'available': available, 'occupancy': _pct(booked, available),
            'open_nights': available - booked - gap_nights, 'gaps': len(in_window), 'gap_nights': gap_nights,
            'short_gaps': sum(1 for g in in_window if g['length'] <= SHORT_GAP_NIGHTS),
        })
    upcoming_gaps = sorted((g for g in all_gaps if g['end'] > today and g['start'] < today + timedelta(days=WINDOWS[-1])),
                           key=lambda g: (g['start'], g['label']))
    for g in upcoming_gaps:
        g['short'] = g['length'] <= SHORT_GAP_NIGHTS

    # --- looking back: last 30/60/90 nights
    all_bookings = [b for u in counted for b in u.all]
    back = []
    for n in WINDOWS:
        start = today - timedelta(days=n)
        booked, available = occupancy(start, today, trailing=True)
        ended = [b for u in counted for b in u.active if start <= _local_date(b.check_out) < today]
        arrived = [b for b in all_bookings if start <= _local_date(b.check_in) < today]
        cancelled = sum(1 for b in arrived if b.status == Booking.Status.CANCELLED)
        with_revenue = [b for b in ended if b.lodging_revenue() is not None and b.nights()]
        revenue = sum((b.lodging_revenue() for b in with_revenue), 0)
        nights_sold = sum(b.nights() for b in with_revenue)
        occ = _pct(booked, available)
        adr = (float(revenue) / nights_sold) if nights_sold else None
        back.append({
            'days': n, 'booked': booked, 'available': available, 'occupancy': occ,
            'stays': len(ended), 'alos': (sum(b.nights() for b in ended) / len(ended)) if ended else None,
            'arrivals': len(arrived), 'cancelled': cancelled, 'cancel_rate': _pct(cancelled, len(arrived)),
            'adr': adr, 'revpar': (adr * (occ / 100)) if (adr is not None and occ is not None) else None,
            'revenue': revenue if with_revenue else None,
            'revenue_coverage': _pct(len(with_revenue), len(ended)), 'stays_with_revenue': len(with_revenue),
        })

    # --- per property
    by_property = []
    for prop in props:
        mine = [u for u in counted if u.prop.pk == prop.pk]
        if not mine:
            continue
        row = {'property': prop, 'units': len(mine)}
        for n in WINDOWS:
            end = today + timedelta(days=n)
            booked = available = 0
            for u in mine:
                days = (end - today).days
                available += days
                booked += sum(1 for i in range(days) if (today + timedelta(days=i)) in u.booked)
            row[f'occ{n}'] = _pct(booked, available)
        gaps = [g for u in mine for g in gaps_for(u) if g['end'] > today and g['start'] < today + timedelta(days=90)]
        row['gaps90'] = len(gaps)
        row['short_gaps90'] = sum(1 for g in gaps if g['length'] <= SHORT_GAP_NIGHTS)
        by_property.append(row)
    by_property.sort(key=lambda r: (r['occ30'] if r['occ30'] is not None else 101, r['property'].name))

    data_from = min((u.data_start for u in counted if u.data_start), default=None)
    return {
        'as_of': today, 'ahead': ahead, 'back': back, 'gaps': upcoming_gaps, 'by_property': by_property,
        'overlaps': [o for u in counted for o in _overlaps(u)],
        'scope': {'units': len(counted), 'properties': len({u.prop.pk for u in counted}), 'excluded': excluded,
                  'unattributed': unattributed, 'data_from': data_from},
    }


# --- one property, month by month --------------------------------------------------------

def _month_starts(today, months):
    """The first day of each of the last `months` calendar months, oldest
    first, ending with the month `today` falls in."""
    year, month = today.year, today.month
    starts = []
    for _ in range(months):
        starts.append(date(year, month, 1))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return list(reversed(starts))


def _next_month(day):
    return date(day.year + (day.month == 12), day.month % 12 + 1, 1)


def _series(units, month_starts, today):
    """Per-month figures for the given units (a whole property, or one unit),
    plus a trailing total. Only nights BEFORE today are measured, so the
    current month is month-to-date and never mixes in future bookings (those
    are the forward view's job).

    Revenue is spread across the nights of each stay (lodging revenue / nights,
    for stays whose report carried an amount) so a stay that straddles two
    months is split fairly. Payouts are cash: counted in the month of the
    payout date, whatever stay they belong to."""
    rows = []
    for start in month_starts:
        end = min(_next_month(start), today)          # nights up to, not including, this day
        row = {
            'start': start, 'label': f'{start:%b}', 'full': f'{start:%B %Y}', 'partial': _next_month(start) > today,
            'available': 0, 'booked': 0, 'by_source': {s: 0 for s in Booking.Source.values},
            'rev': 0.0, 'rev_nights': 0, 'arrivals': 0, 'stays': [], 'cancelled': 0, 'payouts': 0.0, 'payout_count': 0,
        }
        rows.append(row)
        for u in units:
            first = max(start, u.data_start) if u.data_start else None
            if first is not None and first < end:
                row['available'] += (end - first).days
            claimed = set()      # a night can be sold once; overlapping reservations must not count it twice
            for b in sorted(u.all, key=lambda b: b.check_in):
                cin = _local_date(b.check_in)
                if start <= cin < end:
                    if b.status == Booking.Status.CANCELLED:
                        row['cancelled'] += 1
                    else:
                        row['arrivals'] += 1
                        row['stays'].append(b.nights())
                if b.payout_date and b.payout_amount is not None and start <= b.payout_date < _next_month(start):
                    row['payouts'] += float(b.payout_amount)
                    row['payout_count'] += 1
                if b.status != Booking.Status.ACTIVE:
                    continue
                revenue, nights = b.lodging_revenue(), _nights(b)
                rate = float(revenue) / len(nights) if (revenue is not None and nights) else None
                for night in nights:
                    if start <= night < end and night not in claimed:
                        claimed.add(night)
                        row['booked'] += 1
                        row['by_source'][b.source] = row['by_source'].get(b.source, 0) + 1
                        if rate is not None:
                            row['rev'] += rate
                            row['rev_nights'] += 1
    for row in rows:
        occ = _pct(row['booked'], row['available'])
        row['occupancy'] = occ
        row['adr'] = row['rev'] / row['rev_nights'] if row['rev_nights'] else None
        row['revpar'] = row['adr'] * occ / 100 if (row['adr'] is not None and occ is not None) else None
        row['alos'] = sum(row['stays']) / len(row['stays']) if row['stays'] else None
        seen = row['arrivals'] + row['cancelled']
        row['cancel_rate'] = _pct(row['cancelled'], seen)
        row['revenue'] = row['rev'] if row['rev_nights'] else None
        row['coverage'] = _pct(row['rev_nights'], row['booked'])
    total = {
        'available': sum(r['available'] for r in rows), 'booked': sum(r['booked'] for r in rows),
        'rev': sum(r['rev'] for r in rows), 'rev_nights': sum(r['rev_nights'] for r in rows),
        'arrivals': sum(r['arrivals'] for r in rows), 'cancelled': sum(r['cancelled'] for r in rows),
        'payouts': sum(r['payouts'] for r in rows), 'payout_count': sum(r['payout_count'] for r in rows),
    }
    stays = [n for r in rows for n in r['stays']]
    occ = _pct(total['booked'], total['available'])
    adr = total['rev'] / total['rev_nights'] if total['rev_nights'] else None
    total.update({
        'occupancy': occ, 'adr': adr, 'revpar': adr * occ / 100 if (adr is not None and occ is not None) else None,
        'alos': sum(stays) / len(stays) if stays else None,
        'cancel_rate': _pct(total['cancelled'], total['arrivals'] + total['cancelled']),
        'revenue': total['rev'] if total['rev_nights'] else None,
        'coverage': _pct(total['rev_nights'], total['booked']),
    })
    return rows, total


def build_property_performance(prop, unit_id=None, today=None, months=12):
    """Everything the single-property performance screen shows: a trailing
    `months`-month series (occupancy, average nightly rate, revenue, length of
    stay, cancellations, payouts, nights by source), a per-unit breakdown for a
    multi-unit building, and the forward view (next 30/60/90 days, vacancy
    gaps from today, what is booked next). Money is always computed here; the
    view decides who may see it."""
    today = today or timezone.localdate()
    units, _ = _collect_units([prop], today)
    counted = [u for u in units.values() if u.counted]
    if unit_id:
        counted = [u for u in counted if (u.unit.pk if u.unit else None) == unit_id]
    starts = _month_starts(today, months)
    rows, total = _series(counted, starts, today)
    per_unit = []
    if len(units) > 1:
        for u in units.values():
            if not u.counted:
                continue
            _r, t = _series([u], starts, today)
            per_unit.append({'unit': u.unit, 'label': u.unit.label if u.unit else u.label, **t})
        per_unit.sort(key=lambda r: r['label'])

    forward = build_performance(today=today, property_id=prop.pk)
    upcoming = sorted(
        (b for u in counted for b in u.active_ops if _local_date(b.check_out) >= today),
        key=lambda b: b.check_in,
    )[:12]
    return {
        'property': prop, 'as_of': today, 'months': rows, 'total': total, 'per_unit': per_unit,
        'units': [{'pk': u.unit.pk if u.unit else None, 'label': u.unit.label if u.unit else u.label} for u in units.values()] if len(units) > 1 else [],
        'unit_id': unit_id, 'forward': forward, 'upcoming': upcoming,
        'data_from': min((u.data_start for u in counted if u.data_start), default=None),
        'has_data': bool(counted),
        'sources': [{'value': v, 'label': l} for v, l in Booking.Source.choices],
    }
