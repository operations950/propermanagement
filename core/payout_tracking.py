"""Platform payouts as things to track: each Airbnb/VRBO payout (one bank transfer, with the lines that make it up)
is expected, then received once a bank deposit of the same amount turns up close to its date, and is flagged when it
has been waiting too long. The matching is deliberately simple - the deposit's date and amount - because that is all a
bank line reliably carries. The month-end income reconciliation does the fuller work; this is the running list of what
the platforms have said they paid and whether it has reached the bank."""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from .models import LedgerLine, QBRecode

EARLY = timedelta(days=3)         # a deposit may be booked a little before the payout's own date
LATE = timedelta(days=10)         # ... and normally lands within a few days after
LINGERING_AFTER = timedelta(days=45)


def _deposits(properties, start, end):
    qs = LedgerLine.objects.filter(role=LedgerLine.Role.TRUST, category=LedgerLine.Category.DEPOSIT, flow__gt=0, txn_date__gte=start - EARLY, txn_date__lte=end + LATE)
    if properties is not None:
        qs = qs.filter(property_id__in=properties)
    return list(qs.order_by('txn_date', 'pk'))


def payout_rows(source=None, since=None, today=None):
    """Every payout with its lines, where it belongs, and whether it has been received, newest first. Each bank deposit
    is used for at most one payout (the nearest by date wins)."""
    from onsite.models import PayoutBatch
    today = today or timezone.localdate()
    qs = PayoutBatch.objects.prefetch_related('items__booking__property', 'items__booking__unit')
    if source:
        qs = qs.filter(source=source)
    if since:
        qs = qs.filter(date__gte=since)
    batches = list(qs.order_by('date', 'pk'))
    if not batches:
        return []
    rows = []
    for b in batches:
        props = {}
        for item in b.items.all():
            if item.booking_id:
                props[item.booking.property_id] = item.booking.property
        units = sorted({item.booking.unit.label for item in b.items.all() if item.booking_id and item.booking.unit_id})
        codes = {i.external_uid for i in b.items.all() if i.external_uid}
        guests = sorted({i.guest_name or (i.booking.guest_name if i.booking_id else '') for i in b.items.all() if i.external_uid} - {''})
        rows.append({'batch': b, 'properties': list(props.values()), 'units': units, 'reservations': len(codes), 'guests': guests, 'items': list(b.items.all()),
                     'received': None, 'deposit': None})
    props_all = {p.pk for r in rows for p in r['properties']}
    scoped = None if any(not r['properties'] for r in rows) else props_all
    pool = _deposits(scoped, min(r['batch'].date for r in rows), max(r['batch'].date for r in rows))
    used = set()
    for r in sorted(rows, key=lambda r: (r['batch'].date, r['batch'].pk)):
        b = r['batch']
        allowed = {p.pk for p in r['properties']}
        candidates = [d for d in pool if d.pk not in used and d.flow == b.amount and (not allowed or d.property_id in allowed)
                      and b.date - EARLY <= d.txn_date <= b.date + LATE]
        if candidates:
            best = min(candidates, key=lambda d: (abs((d.txn_date - b.date).days), d.txn_date, d.pk))
            used.add(best.pk)
            r['received'], r['deposit'] = True, best
    coded = {c.payout_id: c for c in QBRecode.objects.filter(payout_id__in=[r['batch'].pk for r in rows])}
    for r in rows:
        r['coded'] = coded.get(r['batch'].pk)
        if r['coded'] and not r['received']:
            r['received'] = True          # QuickBooks itself holds a deposit of this amount, now coded to it
        age = today - r['batch'].date
        r['status'] = 'received' if r['received'] else ('lingering' if age >= LINGERING_AFTER else ('waiting' if r['batch'].date <= today else 'scheduled'))
        r['age_days'] = age.days
    return sorted(rows, key=lambda r: (r['batch'].date, r['batch'].pk), reverse=True)


def lingering_count(source=None, today=None):
    """How many payouts have gone 45 days without a matching deposit - what decides whether the payouts screen is worth
    a look."""
    return sum(1 for r in payout_rows(source=source, today=today) if r['status'] == 'lingering')
