"""The month-end lease check: every lease (commercial and long-term) that ends, or whose rent rate runs out, within 120 days of the month being closed, each with a checkbox for
"I looked at this and it is okay". Finished when every one in the list is confirmed. The tenant notices and owner check-ins come later; this only puts them in front of a person.
A confirmation is for a date: if the lease's end date (or rate date) changes, it is asked about again.
"""
from datetime import timedelta

from django.utils import timezone

from accounting import ledger

from .models import Lease, LongTermLease

WINDOW_DAYS = 120


def rows(close_month):
    """One row per lease date to look at, soonest first: {'kind', 'pk', 'what' ('end' or 'rate'), 'date', 'days' from the month's end, 'ok', 'tenant', 'where', 'rent', 'past', 'label', 'link'}."""
    ref = ledger.month_end(close_month)
    limit = ref + timedelta(days=WINDOW_DAYS)
    out = []
    for kind, qs in (('commercial', Lease.objects.select_related('property', 'unit', 'tenant')), ('ltr', LongTermLease.objects.select_related('property', 'tenant'))):
        for lease in qs.exclude(status='ended').filter(property__is_active=True):
            where = lease.property.name + (f' - {lease.unit.label}' if kind == 'commercial' else '')
            rent = lease.base_rent if kind == 'commercial' else lease.monthly_rent
            for what, day, ok_for in (('end', lease.end_date, lease.renewal_ok_for), ('rate', lease.rate_expires_on, lease.rate_ok_for)):
                if day is None:
                    continue
                if what == 'end' and day > limit:
                    continue
                if what == 'rate' and not (ref <= day <= limit):
                    continue
                link = f'/commercial/{lease.property_id}/leases/{lease.pk}/' if kind == 'commercial' else f'/commercial/{lease.property_id}/long-term/{lease.pk}/'
                out.append({'kind': kind, 'pk': lease.pk, 'what': what, 'date': day, 'days': (day - ref).days, 'ok': ok_for == day, 'tenant': lease.tenant.name, 'where': where,
                            'rent': rent, 'past': day < ref, 'link': link,
                            'label': ('Lease ends' if what == 'end' else 'Rent rate expires') if day >= ref else ('Past its end date' if what == 'end' else 'Rate date passed')})
    out.sort(key=lambda r: (r['date'], r['where']))
    return out


def summary(close_month):
    """(status, detail) for the checklist step."""
    items = rows(close_month)
    if not items:
        return 'done', f'No lease ends or changes its rent rate in the next {WINDOW_DAYS} days.'
    ok = sum(1 for r in items if r['ok'])
    if ok == len(items):
        return 'done', f'All {len(items)} lease{"s" if len(items) != 1 else ""} confirmed.'
    if ok == 0:
        return 'not_started', f'{len(items)} lease{"s" if len(items) != 1 else ""} to look at.'
    return None, f'{ok} of {len(items)} confirmed.'


def set_confirmed(kind, pk, what, ok, user=None):
    """Tick (or untick) one lease's end date or rate date as looked at and okay."""
    model = Lease if kind == 'commercial' else LongTermLease
    lease = model.objects.get(pk=pk)
    day = lease.end_date if what == 'end' else lease.rate_expires_on
    if day is None:
        return
    field = 'renewal_ok_for' if what == 'end' else 'rate_ok_for'
    setattr(lease, field, day if ok else None)
    lease.renewal_ok_by, lease.renewal_ok_at = (user if getattr(user, 'pk', None) else None), timezone.now()
    lease.save(update_fields=[field, 'renewal_ok_by', 'renewal_ok_at'])
