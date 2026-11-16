"""Rent from the lease: what a tenant owes for a month, and posting it as charges ahead of the first of the month.

What a month's rent is: the lease's base rent, replaced by the latest rent step that has started by that month, replaced in turn by a seasonal rent if the month is one of its
months. A rent step starts with the month its date falls in when the date is the 1st, otherwise with the next month. A month the lease only partly covers (the first or last month) is
charged by the day when the lease says so. The monthly CAM estimate is a second line, prorated the same way.

Posting: the charge for a month is dated the 1st and is posted `rent_post_days_ahead` days (10 by default) before it, so the tenant can be invoiced in time. It posts once: the journal
entry's source_ref names the lease, the month and the line, and that is what is looked for before posting.
"""
import calendar
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction

from accounting import charges, ledger
from accounting import standard_chart as sc
from accounting.models import Charge, JournalEntry, Ownership

from .models import CommercialSettings, Lease

CENT = Decimal('0.01')
ZERO = Decimal('0.00')
RENT, CAM = 'rent', 'cam'


def first_of(day):
    return day.replace(day=1)


def next_month(day):
    return (first_of(day) + timedelta(days=32)).replace(day=1)


def days_in(month):
    return calendar.monthrange(month.year, month.month)[1]


def settings_for(prop):
    return CommercialSettings.objects.get_or_create(property=prop)[0]


def lead_days(prop):
    return settings_for(prop).rent_post_days_ahead


# ---------------------------------------------------------------- what a month's rent is

def step_month(step):
    """The first month a rent step applies to."""
    d = step.effective_date
    return first_of(d) if d.day == 1 else next_month(d)


def coverage(lease, month):
    """(days the lease covers in the month, days in the month). Zero covered days: no rent that month."""
    month = first_of(month)
    last = month.replace(day=days_in(month))
    start = max(month, lease.start_date) if lease.start_date else month
    end = min(last, lease.end_date) if lease.end_date else last
    return max((end - start).days + 1, 0), days_in(month)


def is_billable(lease, month):
    """Whether the lease bills this month at all: it has started, it has not ended, and it was not marked ended with no end date."""
    if lease.status == Lease.Status.ENDED and not lease.end_date:
        return False
    if not lease.start_date:
        return lease.status == Lease.Status.ACTIVE and coverage(lease, month)[0] > 0
    return coverage(lease, month)[0] > 0


def base_for_month(lease, month):
    """(monthly rent before proration, where it came from: 'base', 'step' or 'season', a short label)."""
    month = first_of(month)
    amount, source, label = lease.base_rent, 'base', 'Base rent'
    steps = [s for s in lease.steps.all() if step_month(s) <= month]
    if steps:
        latest = max(steps, key=lambda s: (step_month(s), s.pk))
        amount, source, label = latest.monthly_rent, 'step', f'Rent step from {latest.effective_date:%b} {latest.effective_date.day}, {latest.effective_date.year}'
    seasons = [s for s in lease.seasons.all() if s.covers(month.month)]
    if seasons:
        s = max(seasons, key=lambda x: x.pk)
        amount, source, label = s.monthly_rent, 'season', s.name or 'Seasonal rent'
    return amount, source, label


def cam_monthly_for(lease, month):
    """The monthly CAM estimate in force for a month: the lease's own figure, replaced by the latest CAM estimate step that has started by then (a CAM reconciliation can set them)."""
    month = first_of(month)
    steps = [s for s in lease.cam_steps.all() if step_month(s) <= month]
    if steps:
        return max(steps, key=lambda s: (step_month(s), s.pk)).monthly_amount
    return lease.cam_estimate or ZERO


def rent_for_month(lease, month):
    """What the lease charges for a month: {'rent', 'cam', 'prorated', 'days', 'of', 'source', 'label', 'monthly'}; rent and cam are zero when the lease does not bill that month."""
    month = first_of(month)
    days, of = coverage(lease, month)
    if not is_billable(lease, month) or days <= 0:
        return {'rent': ZERO, 'cam': ZERO, 'prorated': False, 'days': 0, 'of': of, 'source': '', 'label': '', 'monthly': ZERO}
    monthly, source, label = base_for_month(lease, month)
    prorated = bool(lease.prorate and days < of)
    factor = Decimal(days) / Decimal(of) if prorated else Decimal(1)
    rent = (Decimal(monthly) * factor).quantize(CENT, rounding=ROUND_HALF_UP)
    cam = (Decimal(cam_monthly_for(lease, month) or 0) * factor).quantize(CENT, rounding=ROUND_HALF_UP)
    return {'rent': rent, 'cam': cam, 'prorated': prorated, 'days': days, 'of': of, 'source': source, 'label': label, 'monthly': Decimal(monthly)}


def schedule(lease, start, months=12):
    """The next `months` months of the lease: [{'month', **rent_for_month}], so the tenant's page can show increases and seasons coming."""
    out, m = [], first_of(start)
    for _ in range(months):
        out.append({'month': m, **rent_for_month(lease, m)})
        m = next_month(m)
    return out


def generate_steps(lease, first_effective, years, percent=None, amount=None, note=''):
    """Make yearly rent steps: the first on `first_effective`, then each anniversary, each `percent` percent above the one before or `amount` dollars above it.
    Starts from the rent in force the month before the first step. Returns the steps made (an existing step on the same date is left alone)."""
    from .models import RentStep
    if (percent is None) == (amount is None):
        raise ValueError('Give a percent or a dollar amount, not both and not neither.')
    current, _, _ = base_for_month(lease, first_of(first_effective) - timedelta(days=1))
    made = []
    for i in range(int(years)):
        eff = date(first_effective.year + i, first_effective.month, min(first_effective.day, calendar.monthrange(first_effective.year + i, first_effective.month)[1]))
        current = (Decimal(current) * (Decimal(1) + Decimal(percent) / Decimal(100))).quantize(CENT, rounding=ROUND_HALF_UP) if percent is not None else (Decimal(current) + Decimal(amount)).quantize(CENT)
        step, new = RentStep.objects.get_or_create(lease=lease, effective_date=eff, defaults={'monthly_rent': current, 'note': note or (f'{percent}% increase' if percent is not None else f'${amount} increase')})
        if new:
            made.append(step)
        else:
            current = step.monthly_rent
    return made


# ---------------------------------------------------------------- the tenant row the books bill

def ensure_ownership(lease):
    """Keep the accounting.Ownership row for this lease: the tenant, on the unit, billed. Ended leases end their row. Returns the row."""
    from core.models import Contact
    own = lease.ownership or Ownership.objects.filter(unit=lease.unit, person=lease.tenant, end_date__isnull=True).first()
    ended = lease.status == Lease.Status.ENDED
    end = (lease.end_date or date.today()) if ended else None
    if own is None:
        own = Ownership.objects.create(unit=lease.unit, person=lease.tenant, start_date=lease.start_date, end_date=end, is_billed=not ended, notes='Tenant')
    else:
        own.unit, own.person, own.start_date, own.end_date = lease.unit, lease.tenant, lease.start_date, end
        own.is_billed = not ended
        own.save()
    if lease.ownership_id != own.pk:
        lease.ownership = own
        lease.save(update_fields=['ownership'])
    tenant = lease.tenant
    if tenant.contact_type != Contact.ContactType.TENANT and Contact.ContactType.TENANT not in (tenant.secondary_types or []):
        tenant.secondary_types = list(tenant.secondary_types or []) + [Contact.ContactType.TENANT]
        tenant.save(update_fields=['secondary_types'])
    tenant.properties.add(lease.property)
    return own


# ---------------------------------------------------------------- posting

def _ref(lease, month, line):
    return f'lease:{lease.pk}:{first_of(month):%Y-%m}:{line}'


def is_posted(lease, month, line=RENT):
    return Charge.objects.filter(unit=lease.unit, is_void=False, entry__source_ref=_ref(lease, month, line)).exists()


def _income(association, line):
    ledger.ensure_chart(association)
    return ledger.account(association, sc.COMMERCIAL_CODES['rent'] if line == RENT else sc.COMMERCIAL_CODES['cam'])


@transaction.atomic
def post_lease_month(lease, month, user=None):
    """Post one month's rent (and CAM estimate) for a lease, once. Returns the charges made (none when the lease does not bill that month, it is already posted, or it is before
    the books begin). A closed month raises ledger.LedgerError."""
    association, month = lease.property, first_of(month)
    start = ledger.profile_for(association).books_start
    if start and month <= start:
        return []
    due = rent_for_month(lease, month)
    if due['rent'] <= 0 and due['cam'] <= 0:
        return []
    ensure_ownership(lease)
    made = []
    suffix = ' (prorated)' if due['prorated'] else ''
    for line, amount, text in ((RENT, due['rent'], f'Rent {month:%b %Y}{suffix}'), (CAM, due['cam'], f'CAM estimate {month:%b %Y}{suffix}')):
        if amount <= 0 or is_posted(lease, month, line):
            continue
        income = _income(association, line)
        entry = ledger.post_entry(association, month, [{'account': sc.AR, 'debit': amount, 'unit': lease.unit, 'owner': lease.tenant, 'memo': text},
                                                       {'account': income, 'credit': amount, 'memo': text}],
                                  memo=text, basis=JournalEntry.Basis.ACCRUAL, source=JournalEntry.Source.CHARGE, source_ref=_ref(lease, month, line), user=user)
        made.append(Charge.objects.create(association=association, unit=lease.unit, owner=lease.tenant, kind=Charge.Kind.ASSESSMENT, description=text, due_date=month, amount=amount,
                                          income_account=income, entry=entry))
    if made:
        charges.apply_credits(association, lease.unit, lease.tenant, user=user)
    return made


def leases_for(association, month):
    return [l for l in Lease.objects.filter(property=association).select_related('unit', 'tenant').prefetch_related('steps', 'seasons', 'cam_steps') if is_billable(l, month)]


def month_status(association, month):
    """For the close sheet: how many leases bill the month and how many already have their rent posted ('missing' leases are the ones the books still lack)."""
    month = first_of(month)
    start = ledger.profile_for(association).books_start
    if start and month <= start:
        return {'leases': 0, 'posted': 0, 'missing': 0}
    leases = [l for l in leases_for(association, month) if rent_for_month(l, month)['rent'] > 0 or rent_for_month(l, month)['cam'] > 0]
    posted = sum(1 for l in leases if is_posted(l, month, RENT) or (rent_for_month(l, month)['rent'] <= 0 and is_posted(l, month, CAM)))
    return {'leases': len(leases), 'posted': posted, 'missing': len(leases) - posted}


def months_to_post(association, today):
    """The months whose rent is due to be posted today: this month (in case it was missed) up to the month whose first day is within the property's lead days. An earlier month that was
    missed shows on the close sheet and is posted from the invoices screen."""
    ahead = today + timedelta(days=lead_days(association))
    months, m = [], first_of(today)
    while m <= ahead:
        months.append(m)
        m = next_month(m)
    return months


def post_rent_due(association, today, user=None):
    """Post the rent that has come within the lead time, for every lease, and make each tenant's invoice (sending it too when the property says to). Safe to run again.
    Returns {'posted': charges made, 'invoices': invoices made, 'sent': invoices sent}."""
    from . import invoices
    result = {'posted': 0, 'invoices': 0, 'sent': 0}
    cfg = settings_for(association)
    for month in months_to_post(association, today):
        for lease in leases_for(association, month):
            try:
                made = post_lease_month(lease, month, user=user)
            except ledger.LedgerError:
                continue                      # a closed month: what was due then is in the books or needs a person
            result['posted'] += len(made)
            if made or is_posted(lease, month, RENT) or is_posted(lease, month, CAM):
                inv, new = invoices.ensure_invoice(lease, month)
                if new:
                    result['invoices'] += 1
                    if cfg.auto_send_invoices and inv.status == inv.Status.READY and month >= first_of(today):      # never by itself for a month already past
                        try:
                            invoices.send_invoice(inv, user=user)
                            result['sent'] += 1
                        except invoices.InvoiceError:
                            pass
    return result
