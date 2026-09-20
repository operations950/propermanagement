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
                 nights with no later reservation are "open", not a gap.
  ADR            lodging revenue / nights sold, over reservations whose
                 report gave an amount. Lodging revenue = gross minus cleaning
                 and other fees (see Booking.lodging_revenue).
  RevPAR         ADR x occupancy — revenue per available night.
Cancelled reservations never count as booked nights."""
from datetime import timedelta

from django.utils import timezone

from ..models import Booking, BookingFeed
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
        self.booked = set()
        self.data_start = None
        self.connected = False

    def add(self, booking):
        self.all.append(booking)
        start = _local_date(booking.check_in)
        if self.data_start is None or start < self.data_start:
            self.data_start = start
        if booking.status == Booking.Status.ACTIVE:
            self.active.append(booking)
            self.booked.update(_nights(booking))

    @property
    def counted(self):
        return bool(self.all) or self.connected


def build_performance(today=None, property_id=None):
    today = today or timezone.localdate()
    props = list(eligible_properties())
    if property_id:
        props = [p for p in props if p.pk == property_id]
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
    for booking in Booking.objects.filter(property_id__in=[p.pk for p in props]).select_related('property', 'unit'):
        holder = units.get((booking.property_id, booking.unit_id))
        if holder is None:
            unattributed += 1
            continue
        holder.add(booking)

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
        stays = sorted(u.active, key=lambda b: _local_date(b.check_in))
        found = []
        for before, after in zip(stays, stays[1:]):
            gap_start, gap_end = _local_date(before.check_out), _local_date(after.check_in)
            if gap_end > gap_start:
                found.append({'label': u.label, 'start': gap_start, 'end': gap_end, 'length': (gap_end - gap_start).days})
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
        'scope': {'units': len(counted), 'properties': len({u.prop.pk for u in counted}), 'excluded': excluded,
                  'unattributed': unattributed, 'data_from': data_from},
    }
