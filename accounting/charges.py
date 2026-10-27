"""What owners are charged and what they pay.

A charge is posted to the accrual book (money owed to the association: debit receivables, credit income). A payment is posted to both books: in the accrual
book it clears receivables; in the cash book it is the income itself, in the account of the charge it pays. Money paid before it is owed sits in Prepaid Fees
until a charge comes along to use it.
"""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Q

from core.models import Contact, Unit

from . import ledger, owners
from . import standard_chart as sc
from .models import ZERO, Application, BankAccount, Charge, ChargeRule, JournalEntry, Receipt

FREQ_MONTHS = {'monthly': 1, 'quarterly': 3, 'semiannual': 6, 'annual': 12}
INCOME_FOR_KIND = {Charge.Kind.ASSESSMENT: sc.INCOME_ASSESSMENT, Charge.Kind.SPECIAL: sc.INCOME_SPECIAL, Charge.Kind.LATE_FEE: sc.INCOME_LATE_FEE,
                   Charge.Kind.FINE: sc.INCOME_MISC, Charge.Kind.OTHER: sc.INCOME_MISC, Charge.Kind.OPENING: sc.INCOME_ASSESSMENT}


class ChargeError(Exception):
    pass


def add_months(day, n):
    month_index = day.month - 1 + n
    year, month = day.year + month_index // 12, month_index % 12 + 1
    last = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)).day
    return date(year, month, min(day.day, last))


def default_bank(association, kind='operating'):
    bank = BankAccount.objects.filter(association=association, kind=kind, is_active=True).first() or BankAccount.objects.filter(association=association, is_active=True).first()
    if bank is None:
        raise ChargeError('This association has no bank account set up yet.')
    return bank


# ---------------------------------------------------------------- recurring charges

def rule_due_dates(rule, until):
    dates, i = [], 0
    while True:
        d = rule.first_due if i == 0 else add_months(rule.first_due, FREQ_MONTHS.get(rule.frequency, 1) * i)
        if d > until or (rule.installments and i >= rule.installments) or (rule.end_date and d > rule.end_date):
            break
        dates.append(d)
        i += 1
        if rule.frequency == ChargeRule.Frequency.ONCE:
            break
    return dates


def billable_units(association):
    """[(unit, owner)] for every unit that has an owner to bill."""
    out = []
    for unit in owners.active_units(association):
        person = owners.billed_owner(unit)
        if person is not None:
            out.append((unit, person))
    return out


def rule_description(rule, due):
    return rule.name if rule.frequency == ChargeRule.Frequency.ONCE or rule.installments else f'{rule.name} {due:%b %Y}'


@transaction.atomic
def post_rule(rule, due, user=None):
    """Post one due date of a recurring charge to every unit that has an owner and has not been charged for it yet. Returns how many were charged."""
    association = rule.association
    start = ledger.profile_for(association).books_start
    if start and due <= start:
        return 0
    have = set(Charge.objects.filter(rule=rule, due_date=due).values_list('unit_id', flat=True))
    pairs = [(u, o) for u, o in billable_units(association) if u.pk not in have]
    if not pairs:
        return 0
    if rule.method == ChargeRule.Method.FLAT:
        amounts = {u.pk: ledger.money(rule.amount) for u, o in pairs}
    else:
        rows = owners.shares(association, rule.share_kind)
        split = {u.pk: amt for u, amt in owners.split_by_share(rule.amount, rows)}
        amounts = {u.pk: split.get(u.pk, ZERO) for u, o in pairs}
    pairs = [(u, o) for u, o in pairs if amounts[u.pk] > 0]
    if not pairs:
        return 0
    total = sum((amounts[u.pk] for u, o in pairs), ZERO)
    desc = rule_description(rule, due)
    lines = [{'account': sc.AR, 'debit': amounts[u.pk], 'unit': u, 'owner': o, 'memo': desc} for u, o in pairs]
    lines.append({'account': rule.income_account, 'credit': total, 'memo': desc})
    entry = ledger.post_entry(association, due, lines, memo=desc, basis=JournalEntry.Basis.ACCRUAL, source=JournalEntry.Source.CHARGE,
                              source_ref=f'rule:{rule.pk}:{due}', user=user)
    kind = Charge.Kind.SPECIAL if rule.is_special else Charge.Kind.ASSESSMENT
    Charge.objects.bulk_create([Charge(association=association, unit=u, owner=o, rule=rule, kind=kind, description=desc, due_date=due, amount=amounts[u.pk],
                                       income_account=rule.income_account, entry=entry) for u, o in pairs])
    for u, o in pairs:
        apply_credits(association, u, o, user=user)
    return len(pairs)


def post_due(association, today, user=None):
    """Post every recurring charge that has come due. Returns [(rule, due, count)] for what was posted."""
    done = []
    for rule in ChargeRule.objects.filter(association=association, is_active=True).select_related('income_account'):
        for due in rule_due_dates(rule, today):
            try:
                n = post_rule(rule, due, user=user)
            except ledger.LedgerError:
                continue   # a closed month; what was due then is in the books already or needs a person
            if n:
                done.append((rule, due, n))
    return done


def rule_status(rule, today):
    """For the charges list: the most recent due date, how many units it reached, and when the next is."""
    dates = rule_due_dates(rule, today)
    pairs = billable_units(rule.association)
    last = dates[-1] if dates else None
    posted = Charge.objects.filter(rule=rule, due_date=last, is_void=False).count() if last else 0
    nxt = None
    probe = rule_due_dates(rule, add_months(today, 14))
    later = [d for d in probe if d > today]
    if later:
        nxt = later[0]
    start = ledger.profile_for(rule.association).books_start
    pending = bool(last) and posted < len(pairs) and not (start and last <= start)
    return {'last_due': last, 'posted': posted, 'units': len(pairs), 'next_due': nxt, 'pending': pending}


# ---------------------------------------------------------------- one charge to one unit

@transaction.atomic
def post_charge(association, unit, owner, kind, description, due, amount, income_account=None, user=None, parent=None):
    amount = ledger.money(amount)
    if amount <= 0:
        raise ChargeError('A charge needs an amount above zero.')
    income = income_account or ledger.account(association, INCOME_FOR_KIND[kind])
    entry = ledger.post_entry(association, due, [{'account': sc.AR, 'debit': amount, 'unit': unit, 'owner': owner, 'memo': description},
                                                 {'account': income, 'credit': amount, 'memo': description}],
                              memo=description, basis=JournalEntry.Basis.ACCRUAL, source=JournalEntry.Source.CHARGE, source_ref='one-off', user=user)
    charge = Charge.objects.create(association=association, unit=unit, owner=owner, kind=kind, description=description[:200], due_date=due, amount=amount,
                                   income_account=income, entry=entry, parent=parent)
    apply_credits(association, unit, owner, user=user)
    return charge


@transaction.atomic
def void_charge(charge, user=None, day=None):
    """Take a charge back: it must not have been paid. Posts the opposite entry and marks the charge void."""
    if charge.is_void:
        return
    if charge.applications.exists():
        raise ChargeError('That charge has a payment on it. Reverse the payment first.')
    day = day or date.today()
    ledger.post_entry(charge.association, day, [{'account': charge.income_account or sc.INCOME_ASSESSMENT, 'debit': charge.amount, 'memo': f'Void: {charge.description}'},
                                               {'account': sc.AR, 'credit': charge.amount, 'unit': charge.unit, 'owner': charge.owner, 'memo': f'Void: {charge.description}'}],
                      memo=f'Void: {charge.description}', basis=JournalEntry.Basis.ACCRUAL, source=JournalEntry.Source.REVERSAL, source_ref=f'charge:{charge.pk}', user=user)
    charge.is_void = True
    charge.save(update_fields=['is_void'])


# ---------------------------------------------------------------- what owners pay

def open_charges(unit, owner):
    return [c for c in Charge.objects.filter(unit=unit, owner=owner, is_void=False).order_by('due_date', 'id').prefetch_related('applications') if c.balance > 0]


def _income_lines(applied):
    """Group (charge, amount) by income account into credit lines."""
    by_account = defaultdict(lambda: ZERO)
    for charge, amt in applied:
        by_account[charge.income_account_id or None] += amt
    return by_account


@transaction.atomic
def receive_payment(association, unit, owner, day, amount, method=Receipt.Method.CHECK, reference='', bank_account=None, memo='', user=None):
    """Record money an owner paid. It pays their oldest charges first; anything left over is held as a credit. Posts to both books."""
    amount = ledger.money(amount)
    if amount <= 0:
        raise ChargeError('A payment needs an amount above zero.')
    bank = bank_account or default_bank(association)
    receipt = Receipt.objects.create(association=association, owner=owner, unit=unit, date=day, amount=amount, method=method, reference=reference[:80],
                                     memo=memo[:200], bank_account=bank, created_by=user)
    remaining, applied = amount, []
    for charge in open_charges(unit, owner):
        if remaining <= 0:
            break
        take = min(charge.balance, remaining)
        applied.append((charge, take))
        remaining -= take
    who = {'unit': unit, 'owner': owner}
    desc = f'Payment from {owner.name}, {unit.label}'
    accrual = [{'account': bank.account, 'debit': amount, 'memo': desc, **who}]
    cash = [{'account': bank.account, 'debit': amount, 'memo': desc, **who}]
    if applied:
        accrual.append({'account': sc.AR, 'credit': sum((a for c, a in applied), ZERO), 'memo': desc, **who})
        for account_id, amt in _income_lines(applied).items():
            cash.append({'account': ledger.Account.objects.get(pk=account_id) if account_id else sc.INCOME_ASSESSMENT, 'credit': amt, 'memo': desc, 'unit': unit, 'owner': owner})
    if remaining > 0:
        accrual.append({'account': sc.PREPAID_FEES, 'credit': remaining, 'memo': desc + ' (paid ahead)', **who})
        cash.append({'account': sc.PREPAID_FEES, 'credit': remaining, 'memo': desc + ' (paid ahead)', **who})
    receipt.entry = ledger.post_entry(association, day, accrual, memo=desc, basis=JournalEntry.Basis.ACCRUAL, source=JournalEntry.Source.RECEIPT, source_ref=f'receipt:{receipt.pk}', user=user)
    receipt.cash_entry = ledger.post_entry(association, day, cash, memo=desc, basis=JournalEntry.Basis.CASH, source=JournalEntry.Source.RECEIPT, source_ref=f'receipt:{receipt.pk}', user=user)
    receipt.save(update_fields=['entry', 'cash_entry'])
    Application.objects.bulk_create([Application(receipt=receipt, charge=c, amount=a, date=day) for c, a in applied])
    return receipt


@transaction.atomic
def apply_credits(association, unit, owner, user=None):
    """Use money an owner paid ahead on charges that are now owed."""
    receipts = [r for r in Receipt.objects.filter(unit=unit, owner=owner, is_void=False).order_by('date', 'id').prefetch_related('applications') if r.unapplied > 0]
    if not receipts:
        return 0
    used = 0
    for charge in open_charges(unit, owner):
        for r in receipts:
            room = r.unapplied
            if room <= 0 or charge.balance <= 0:
                continue
            take = min(room, charge.balance)
            day = max(r.date, charge.due_date)
            who = {'unit': unit, 'owner': owner}
            desc = f'Paid-ahead credit used on {charge.description}'
            ledger.post_entry(association, day, [{'account': sc.PREPAID_FEES, 'debit': take, 'memo': desc, **who}, {'account': sc.AR, 'credit': take, 'memo': desc, **who}],
                              memo=desc, basis=JournalEntry.Basis.ACCRUAL, source=JournalEntry.Source.RECEIPT, source_ref=f'receipt:{r.pk}', user=user)
            ledger.post_entry(association, day, [{'account': sc.PREPAID_FEES, 'debit': take, 'memo': desc, **who},
                                                 {'account': charge.income_account or sc.INCOME_ASSESSMENT, 'credit': take, 'memo': desc, 'unit': unit, 'owner': owner}],
                              memo=desc, basis=JournalEntry.Basis.CASH, source=JournalEntry.Source.RECEIPT, source_ref=f'receipt:{r.pk}', user=user)
            Application.objects.create(receipt=r, charge=charge, amount=take, date=day)
            # prefetch caches are stale now
            charge._prefetched_objects_cache = {}
            r._prefetched_objects_cache = {}
            used += 1
    return used


@transaction.atomic
def void_receipt(receipt, user=None, day=None):
    if receipt.is_void:
        return
    day = day or date.today()
    # the payment's own entries and any later entries that used it as a credit all carry its reference; reverse them all
    for entry in JournalEntry.objects.filter(association=receipt.association, source=JournalEntry.Source.RECEIPT, source_ref=f'receipt:{receipt.pk}', reverses__isnull=True):
        if not entry.reversed_by.exists():
            ledger.reverse_entry(entry, day, user=user, memo=f'Void: payment from {receipt.owner.name}')
    receipt.applications.all().delete()
    receipt.is_void = True
    receipt.save(update_fields=['is_void'])


# ---------------------------------------------------------------- late fees

def late_fee_amount(profile, charge):
    kind = profile.late_fee_kind
    flat = profile.late_fee_flat or ZERO
    pct = (profile.late_fee_percent or ZERO) / Decimal('100') * charge.amount
    if kind == profile.LateFeeKind.FLAT:
        return ledger.money(flat)
    if kind == profile.LateFeeKind.PERCENT:
        return ledger.money(pct)
    if kind == profile.LateFeeKind.GREATER:
        return ledger.money(max(flat, pct))
    return ZERO


def assess_late_fees(association, today, user=None):
    """Charge a late fee on every assessment that is past its grace period and still unpaid, once each. Returns the late-fee charges made."""
    profile = ledger.profile_for(association)
    if not profile.late_fee_is_set():
        return []
    grace = profile.late_fee_grace_days or 0
    made = []
    for charge in Charge.objects.filter(association=association, is_void=False, kind__in=[Charge.Kind.ASSESSMENT, Charge.Kind.SPECIAL],
                                        due_date__lt=today - timedelta(days=grace)).select_related('unit', 'owner').prefetch_related('applications'):
        if charge.balance <= 0 or Charge.objects.filter(parent=charge, kind=Charge.Kind.LATE_FEE).exists():
            continue
        fee = late_fee_amount(profile, charge)
        if fee <= 0:
            continue
        due = charge.due_date + timedelta(days=grace + 1)
        try:
            made.append(post_charge(association, charge.unit, charge.owner, Charge.Kind.LATE_FEE, f'Late fee on {charge.description}', max(due, today if due > today else due), fee,
                                    user=user, parent=charge))
        except ledger.LedgerError:
            continue
    return made


# ---------------------------------------------------------------- reading it back

def accounts(association):
    """One row per (unit, owner) that has ever been charged or has paid: what was charged, what came in, and the balance (owed is plus, paid ahead is minus)."""
    rows = {}
    for c in Charge.objects.filter(association=association, is_void=False).select_related('unit', 'owner'):
        r = rows.setdefault((c.unit_id, c.owner_id), {'unit': c.unit, 'owner': c.owner, 'charged': ZERO, 'received': ZERO})
        r['charged'] += c.amount
    for p in Receipt.objects.filter(association=association, is_void=False).select_related('unit', 'owner'):
        r = rows.setdefault((p.unit_id, p.owner_id), {'unit': p.unit, 'owner': p.owner, 'charged': ZERO, 'received': ZERO})
        r['received'] += p.amount
    for r in rows.values():
        r['balance'] = r['charged'] - r['received']
        r['current'] = owners.billed_owner(r['unit']) is not None and r['owner'].pk in [o.person_id for o in owners.current_ownerships(r['unit'])]
    return sorted(rows.values(), key=lambda r: (r['unit'].label, r['owner'].name))


def totals(rows):
    owed = sum((r['balance'] for r in rows if r['balance'] > 0), ZERO)
    ahead = sum((-r['balance'] for r in rows if r['balance'] < 0), ZERO)
    return {'owed': owed, 'ahead': ahead, 'net': owed - ahead}


def unit_balance(unit):
    rows = accounts(unit.property)
    return sum((r['balance'] for r in rows if r['unit'].pk == unit.pk), ZERO)


def ledger_rows(unit, owner=None):
    """Chronological charges and payments for a unit (or one owner on it) with a running balance."""
    items = []
    cq = Charge.objects.filter(unit=unit, is_void=False)
    rq = Receipt.objects.filter(unit=unit, is_void=False)
    if owner is not None:
        cq, rq = cq.filter(owner=owner), rq.filter(owner=owner)
    for c in cq.select_related('owner'):
        items.append({'date': c.due_date, 'order': 0, 'what': c.description, 'owner': c.owner, 'charge': c.amount, 'payment': None, 'obj': c})
    for p in rq.select_related('owner'):
        how = p.get_method_display() + (f' {p.reference}' if p.reference else '')
        items.append({'date': p.date, 'order': 1, 'what': f'Payment, {how}', 'owner': p.owner, 'charge': None, 'payment': p.amount, 'obj': p})
    items.sort(key=lambda r: (r['date'], r['order']))
    running = ZERO
    for r in items:
        running += (r['charge'] or ZERO) - (r['payment'] or ZERO)
        r['balance'] = running
    return items


BUCKETS = (('Current', -10 ** 6, 30), ('31 to 60 days', 31, 60), ('61 to 90 days', 61, 90), ('Over 90 days', 91, 10 ** 6))


def aging(association, as_of):
    """Open charges by how long they have been owed, per owner and unit."""
    out = {}
    for c in Charge.objects.filter(association=association, is_void=False).select_related('unit', 'owner').prefetch_related('applications'):
        bal = c.balance
        if bal <= 0 or c.due_date > as_of:
            continue
        age = (as_of - c.due_date).days
        row = out.setdefault((c.unit_id, c.owner_id), {'unit': c.unit, 'owner': c.owner, 'buckets': [ZERO] * len(BUCKETS), 'total': ZERO})
        for i, (label, lo, hi) in enumerate(BUCKETS):
            if lo <= age <= hi:
                row['buckets'][i] += bal
                break
        row['total'] += bal
    rows = sorted(out.values(), key=lambda r: (r['unit'].label, r['owner'].name))
    return {'rows': rows, 'buckets': [b[0] for b in BUCKETS], 'totals': [sum((r['buckets'][i] for r in rows), ZERO) for i in range(len(BUCKETS))],
            'total': sum((r['total'] for r in rows), ZERO)}
