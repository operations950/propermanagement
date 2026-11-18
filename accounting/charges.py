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

from core import memo as request_memo
from core.models import Contact, Unit

from . import ledger, owners
from . import standard_chart as sc
from .models import ZERO, Application, BankAccount, Charge, ChargeRule, JournalEntry, Receipt

FREQ_MONTHS = {'monthly': 1, 'quarterly': 3, 'semiannual': 6, 'annual': 12}
INCOME_FOR_KIND = {Charge.Kind.ASSESSMENT: sc.INCOME_ASSESSMENT, Charge.Kind.SPECIAL: sc.INCOME_SPECIAL, Charge.Kind.LATE_FEE: sc.INCOME_LATE_FEE,
                   Charge.Kind.FINE: sc.INCOME_MISC, Charge.Kind.OTHER: sc.INCOME_MISC, Charge.Kind.OPENING: sc.INCOME_ASSESSMENT, Charge.Kind.INTEREST: sc.INTEREST_INCOME,
                   Charge.Kind.LOAN: sc.INCOME_SPECIAL}
# A payment goes first to interest, then to late fees, then to costs and fines, and last to the assessment itself (the declaration and Florida law say so).
# What late fees and interest are charged on: dues, and the balance forward carried from the old system (its own interest only from the day the books start, since the old system charged its own before)
DUES_KINDS = (Charge.Kind.ASSESSMENT, Charge.Kind.SPECIAL, Charge.Kind.OPENING)
PAY_ORDER = {Charge.Kind.INTEREST: 0, Charge.Kind.LATE_FEE: 1, Charge.Kind.FINE: 2, Charge.Kind.OTHER: 2}


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
        for due in rule_due_dates(rule, today + timedelta(days=rule.post_days_ahead or 0)):
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
    if kind == Charge.Kind.LOAN:
        raise ChargeError('An owner loan is set up on the Owner loans screen, not as a charge.')
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
    if charge.kind == Charge.Kind.LOAN:
        raise ChargeError('An owner loan is not voided like a charge. Change it on the Owner loans screen.')
    if charge.applications.exists():
        raise ChargeError('That charge has a payment on it. Reverse the payment first.')
    day = day or date.today()
    # An opening charge (a balance carried from the old system) never went through income: its receivable came in with the opening balances, against the fund balance.
    # Taking it back takes it out of the fund balance, not out of this period's income.
    back_to = sc.FUND_BALANCE if charge.kind == Charge.Kind.OPENING else (charge.income_account or sc.INCOME_ASSESSMENT)
    ledger.post_entry(charge.association, day, [{'account': back_to, 'debit': charge.amount, 'memo': f'Void: {charge.description}'},
                                               {'account': sc.AR, 'credit': charge.amount, 'unit': charge.unit, 'owner': charge.owner, 'memo': f'Void: {charge.description}'}],
                      memo=f'Void: {charge.description}', basis=JournalEntry.Basis.ACCRUAL, source=JournalEntry.Source.REVERSAL, source_ref=f'charge:{charge.pk}', user=user)
    charge.is_void = True
    charge.save(update_fields=['is_void'])


# ---------------------------------------------------------------- what owners pay

def open_charges(unit, owner, loan=False):
    """What the owner still owes, in the order a payment pays it: interest, late fees, costs, then assessments, the oldest first within each. An owner loan is never in this list
    (a payment never pays it by itself); with loan=True it is the only thing in the list, for a payment the person says is for the loan."""
    if request_memo.active() and unit.property_id:
        # While a page is being built (a GET: core/memo.py) every charge of the unit's property is read once, with its payments, and each owner's list is cut from it.
        mine = request_memo.get(('charges.by_owner', unit.property_id), lambda: _charges_by_owner(unit.property_id)).get((unit.pk, getattr(owner, 'pk', None)), [])
        rows = [c for c in mine if (c.kind == Charge.Kind.LOAN) == bool(loan) and c.balance > 0]
    else:
        qs = Charge.objects.filter(unit=unit, owner=owner, is_void=False)
        qs = qs.filter(kind=Charge.Kind.LOAN) if loan else qs.exclude(kind=Charge.Kind.LOAN)
        rows = [c for c in qs.order_by('due_date', 'id').prefetch_related('applications') if c.balance > 0]
    return sorted(rows, key=lambda c: (PAY_ORDER.get(c.kind, 3), c.due_date, c.id))


def _charges_by_owner(property_id):
    out = defaultdict(list)
    for c in Charge.objects.filter(unit__property_id=property_id, is_void=False).order_by('due_date', 'id').prefetch_related('applications'):
        out[(c.unit_id, c.owner_id)].append(c)
    return out


def loan_balance(unit, owner=None):
    """What an owner still owes on their long-term loan."""
    qs = Charge.objects.filter(unit=unit, is_void=False, kind=Charge.Kind.LOAN)
    if owner is not None:
        qs = qs.filter(owner=owner)
    return sum((c.balance for c in qs.prefetch_related('applications')), ZERO)


def _income_lines(applied):
    """Group (charge, amount) by income account into credit lines."""
    by_account = defaultdict(lambda: ZERO)
    for charge, amt in applied:
        by_account[charge.income_account_id or None] += amt
    return by_account


@transaction.atomic
def receive_payment(association, unit, owner, day, amount, method=Receipt.Method.CHECK, reference='', bank_account=None, memo='', user=None, to_loan=False):
    """Record money an owner paid. It pays their oldest charges first; anything left over is held as a credit. Posts to both books. With `to_loan` the money pays the owner's loan
    and nothing else (it cannot be more than the loan balance); without it the loan is never touched."""
    amount = ledger.money(amount)
    if amount <= 0:
        raise ChargeError('A payment needs an amount above zero.')
    if to_loan:
        owed_on_loan = loan_balance(unit, owner)
        if owed_on_loan <= 0:
            raise ChargeError('This owner has no loan balance.')
        if amount > owed_on_loan:
            raise ChargeError(f'That is more than the loan balance of ${owed_on_loan:,.2f}.')
    bank = bank_account or default_bank(association)
    receipt = Receipt.objects.create(association=association, owner=owner, unit=unit, date=day, amount=amount, method=method, reference=reference[:80],
                                     memo=memo[:200], bank_account=bank, created_by=user)
    remaining, applied = amount, []
    for charge in open_charges(unit, owner, loan=to_loan):
        if remaining <= 0:
            break
        take = min(charge.balance, remaining)
        applied.append((charge, take))
        remaining -= take
    who = {'unit': unit, 'owner': owner}
    desc = f'Payment from {owner.name}, {unit.label}' + (' (loan)' if to_loan else '')
    accrual = [{'account': bank.account, 'debit': amount, 'memo': desc, **who}]
    cash = [{'account': bank.account, 'debit': amount, 'memo': desc, **who}]
    if applied:
        on_loan = sum((a for c, a in applied if c.kind == Charge.Kind.LOAN), ZERO)
        on_dues = sum((a for c, a in applied if c.kind != Charge.Kind.LOAN), ZERO)
        if on_dues:
            accrual.append({'account': sc.AR, 'credit': on_dues, 'memo': desc, **who})
        if on_loan:
            accrual.append({'account': sc.OWNER_LOAN, 'credit': on_loan, 'memo': desc + ' (loan)', **who})
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

def _lease_late_fee(charge):
    """A commercial lease may set its own late fee (a flat amount, a percent, or both: the greater): (flat or None, percent or None), else None."""
    unit_id = getattr(charge, 'unit_id', None)
    if unit_id is None:
        return None
    try:
        from commercial.models import Lease
    except ImportError:
        return None
    # While a page is being built (a GET: core/memo.py) a unit's lease is read once, however many of its charges are looked at.
    return request_memo.get(('lease_late_fee', unit_id), lambda: _read_lease_late_fee(Lease, unit_id))


def _read_lease_late_fee(Lease, unit_id):
    lease = Lease.objects.filter(unit_id=unit_id).exclude(late_fee_flat__isnull=True, late_fee_percent__isnull=True).order_by('-start_date', '-id').first()
    return (lease.late_fee_flat, lease.late_fee_percent) if lease else None


def late_fee_amount(profile, charge, amount=None):
    """The late fee on a charge: on its amount, or on `amount` when that is given (a balance forward is charged on what is still unpaid of it)."""
    base = charge.amount if amount is None else amount
    own = _lease_late_fee(charge)
    if own is not None:
        flat, pct = own[0], own[1]
        fee_flat = ledger.money(flat) if flat is not None else None
        fee_pct = ledger.money((pct or ZERO) / Decimal('100') * base) if pct is not None else None
        return max(x for x in (fee_flat, fee_pct) if x is not None)
    kind = profile.late_fee_kind
    flat = profile.late_fee_flat or ZERO
    pct = (profile.late_fee_percent or ZERO) / Decimal('100') * base
    if kind == profile.LateFeeKind.FLAT:
        return ledger.money(flat)
    if kind == profile.LateFeeKind.PERCENT:
        return ledger.money(pct)
    if kind == profile.LateFeeKind.GREATER:
        return ledger.money(max(flat, pct))
    return ZERO


def balance_as_of(charge, as_of):
    """What was still unpaid of a charge on `as_of`: the amount less the payments applied on or before that day (a payment made later does not count)."""
    return charge.amount - sum((a.amount for a in charge.applications.all() if a.date <= as_of), ZERO)


def _late_fee_rows(association, today, units=None, as_of=None):
    """[(charge, fee, due)]: the late fees that are owed (past the grace period, still unpaid, none charged yet for that installment).
    Without `as_of` it is worked out for today and the balance is what is unpaid now. With `as_of` it is worked out for that day: a charge counts only if it was unpaid on that day
    (a payment made before it settles the charge, one made after does not), so choosing the 31st of a month with a 10-day grace period is a 31-day grace period."""
    profile = ledger.profile_for(association)
    if not profile.late_fee_is_set():
        return []
    grace = profile.late_fee_grace_days or 0
    day = as_of or today
    books_start = profile.books_start
    rows = []
    qs = Charge.objects.filter(association=association, is_void=False, kind__in=DUES_KINDS, due_date__lt=day - timedelta(days=grace))
    if units is not None:
        qs = qs.filter(unit_id__in=units)
    for charge in qs.select_related('unit', 'owner').prefetch_related('applications'):
        open_ = balance_as_of(charge, as_of) if as_of is not None else charge.balance
        if open_ <= 0 or _has_late_fee(association, charge):
            continue
        fee = late_fee_amount(profile, charge, open_)       # the fee is on what is still unpaid of the installment (a part-paid one is charged on the rest, as a balance forward always was)
        if fee > 0:
            rows.append((charge, fee, max(charge.due_date + timedelta(days=grace + 1), books_start + timedelta(days=1) if charge.kind == Charge.Kind.OPENING and books_start else date.min)))
    return rows


def _has_late_fee(association, charge):
    """Whether a late fee has already been charged on this installment (a voided one still counts)."""
    if request_memo.active():
        # While a page is being built (a GET: core/memo.py) the installments of the association that carry a late fee are read once.
        parents = request_memo.get(('late_fee_parents', association.pk), lambda: set(Charge.objects.filter(association=association, kind=Charge.Kind.LATE_FEE, parent__isnull=False).values_list('parent_id', flat=True)))
        return charge.pk in parents
    return Charge.objects.filter(parent=charge, kind=Charge.Kind.LATE_FEE).exists()


def assess_late_fees(association, today, user=None, units=None, as_of=None, post_day=None):
    """Charge a late fee on every assessment that is past its grace period and still unpaid, once each. Returns the late-fee charges made.
    `as_of` judges who is late on that day (see _late_fee_rows); `post_day` dates the charges (their ledger entries too) instead of the day each fee fell due."""
    made = []
    for charge, fee, due in _late_fee_rows(association, today, units, as_of=as_of):
        try:
            made.append(post_charge(association, charge.unit, charge.owner, Charge.Kind.LATE_FEE, f'Late fee on {charge.description}', post_day or max(due, today if due > today else due), fee, user=user, parent=charge))
        except ledger.LedgerError:
            continue
    return made


def interest_for_period(charge, start, end, rate, grace_days=0):
    """Simple daily interest on what is unpaid of an assessment, from `start` to `end`: each day after it fell due (and the grace days), the unpaid balance at the close of the
    day before times rate / 365. Paying on a day stops interest from the day after."""
    first = max(start, charge.due_date + timedelta(days=(grace_days or 0) + 1))
    if first > end:
        return ZERO
    apps = [(a.date, a.amount) for a in charge.applications.all()]
    total, day = ZERO, first
    while day <= end:
        before = day - timedelta(days=1)
        balance = charge.amount - sum((amt for d, amt in apps if d <= before), ZERO)
        if balance > 0:
            total += balance * Decimal(rate) / Decimal(100) / Decimal(365)
        day += timedelta(days=1)
    return ledger.money(total)


def _interest_description(charge, month_first):
    return f'Interest on {charge.description}, {month_first:%B %Y}'[:200]


def _interest_charged(charge, month_first, end):
    """Whether interest for this charge and month is already on the books (dated the month's end, or dated another day when a person chose the posting date)."""
    if request_memo.active() and charge.association_id:
        # While a page is being built (a GET: core/memo.py) the interest charges of the association are read once, grouped by the installment they are on.
        by_parent = request_memo.get(('interest_by_parent', charge.association_id), lambda: _interest_by_parent(charge.association_id))
        description = _interest_description(charge, month_first)
        return any(due == end or desc == description for due, desc in by_parent.get(charge.pk, ()))
    return Charge.objects.filter(parent=charge, kind=Charge.Kind.INTEREST, is_void=False).filter(Q(due_date=end) | Q(description=_interest_description(charge, month_first))).exists()


def _interest_by_parent(association_id):
    out = defaultdict(list)
    for parent_id, due, desc in Charge.objects.filter(association_id=association_id, kind=Charge.Kind.INTEREST, is_void=False, parent__isnull=False).values_list('parent_id', 'due_date', 'description'):
        out[parent_id].append((due, desc))
    return out


def _interest_months(today, as_of=None):
    """The months interest can be charged for: the last three completed months. For `as_of`, a month counts once it has ended on or before that day."""
    if as_of is None:
        cur = ledger.previous_month(ledger.month_start(today))
    else:
        cur = ledger.month_start(as_of)
        if as_of < ledger.month_end(cur):
            cur = ledger.previous_month(cur)
    months = [cur]
    for _ in range(2):
        months.append(ledger.previous_month(months[-1]))
    return months


def _interest_rows(association, today, units=None, as_of=None):
    """[(charge, month_first, month_end, amount)]: interest owed for the last three completed months that has not been charged yet.
    With `as_of`, as that day stood: only a charge that was still unpaid on that day carries interest (one paid before it does not), and the months are those over by that day."""
    profile = ledger.profile_for(association)
    if not profile.interest_is_set():
        return []
    months = _interest_months(today, as_of)
    rows = []
    qs = Charge.objects.filter(association=association, is_void=False, kind__in=DUES_KINDS)
    if units is not None:
        qs = qs.filter(unit_id__in=units)
    books_start = profile.books_start
    for charge in qs.select_related('unit', 'owner').prefetch_related('applications'):
        if as_of is not None and (charge.due_date > as_of or balance_as_of(charge, as_of) <= 0):
            continue
        for month_first in sorted(months):
            end = ledger.month_end(month_first)
            if charge.due_date > end or _interest_charged(charge, month_first, end):
                continue
            start = month_first
            if charge.kind == Charge.Kind.OPENING and books_start:
                if end <= books_start:
                    continue                                                   # the old system charged these months
                start = max(month_first, books_start + timedelta(days=1))
            amount = interest_for_period(charge, start, end, profile.interest_rate, profile.interest_grace_days)
            if amount >= Decimal('0.01'):
                rows.append((charge, month_first, end, amount))
    return rows


def assess_interest(association, today, user=None, units=None, as_of=None, post_day=None):
    """Charge the interest that has built up on unpaid assessments, one charge a month for each, dated the last day of the month (or `post_day`). Months already closed are left alone."""
    made = []
    for charge, month_first, end, amount in _interest_rows(association, today, units, as_of=as_of):
        try:
            made.append(post_charge(association, charge.unit, charge.owner, Charge.Kind.INTEREST, f'Interest on {charge.description}, {month_first:%B %Y}', post_day or end, amount, user=user, parent=charge))
        except ledger.LedgerError:
            continue
    return made


def _plain(value):
    """5.000 as 5, 7.500 as 7.5 (a percent as people write it)."""
    if value is None:
        return None
    d = Decimal(value)
    return d.quantize(Decimal(1)) if d == d.to_integral() else d.normalize()


def terms_sentences(profile, example=None):
    """The late fee and interest rules in plain words, for the person to check against the declaration. `example` is an assessment amount to show the fee on."""
    out = []
    kind = profile.late_fee_kind
    flat, pct = profile.late_fee_flat, _plain(profile.late_fee_percent)
    days = profile.late_fee_grace_days or 0
    when = 'the day after it is due' if not days else f'{days} days after it is due'
    if kind == profile.LateFeeKind.NONE:
        out.append('No late fee is charged.')
    else:
        what = {'flat': f'${flat or 0:,.2f}', 'percent': f'{pct or 0}% of the installment', 'greater': f'the greater of ${flat or 0:,.2f} or {pct or 0}% of the installment'}[kind]
        line = f'A late fee of {what} is charged {when}, once for each late installment.'
        if example:
            class _C:
                amount = Decimal(example)
            line += f' On a ${Decimal(example):,.2f} assessment that is ${late_fee_amount(profile, _C):,.2f}.'
        out.append(line)
    rate = _plain(profile.interest_rate)
    if not rate or rate <= 0:
        out.append('No interest is charged.')
    else:
        start = 'from the due date' if not profile.interest_grace_days else f'starting {profile.interest_grace_days} days after the due date'
        daily = Decimal(rate) / Decimal(365)
        out.append(f'Interest at {rate}% a year (simple, on the unpaid balance, {daily:.4f}% a day) is charged {start}, once a month.')
    out.append('A payment goes first to interest, then late fees, then costs, then the assessment.')
    return out


# ---------------------------------------------------------------- reading it back

def accounts(association):
    """One row per (unit, owner) of the association (see _accounts). While a page is being built (a GET: core/memo.py) the rows are worked out once and each caller gets its own copy; elsewhere it is
    worked out every time, exactly as before."""
    rows = request_memo.get(('charges.accounts', association.pk), lambda: _accounts(association))
    return [dict(r) for r in rows]


def _accounts(association):
    """One row per (unit, owner) that has ever been charged or has paid: what was charged, what came in, and the balance (owed is plus, paid ahead is minus)."""
    rows = {}
    new = lambda unit, owner: {'unit': unit, 'owner': owner, 'charged': ZERO, 'received': ZERO, 'loan_charged': ZERO, 'loan_paid': ZERO}
    for c in Charge.objects.filter(association=association, is_void=False).select_related('unit', 'owner'):
        r = rows.setdefault((c.unit_id, c.owner_id), new(c.unit, c.owner))
        r['charged'] += c.amount
        if c.kind == Charge.Kind.LOAN:
            r['loan_charged'] += c.amount
    for p in Receipt.objects.filter(association=association, is_void=False).select_related('unit', 'owner'):
        r = rows.setdefault((p.unit_id, p.owner_id), new(p.unit, p.owner))
        r['received'] += p.amount
    for a in Application.objects.filter(charge__association=association, charge__kind=Charge.Kind.LOAN, charge__is_void=False, receipt__is_void=False).select_related('charge'):
        rows[(a.charge.unit_id, a.charge.owner_id)]['loan_paid'] += a.amount
    for r in rows.values():
        r['loan'] = r['loan_charged'] - r['loan_paid']                                                    # what is owed on the loan: its own figure
        r['balance'] = (r['charged'] - r['loan_charged']) - (r['received'] - r['loan_paid'])             # dues only: the loan is not in it
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
    cq = Charge.objects.filter(unit=unit, is_void=False).exclude(kind=Charge.Kind.LOAN)
    loan_receipts = set(Application.objects.filter(charge__unit=unit, charge__kind=Charge.Kind.LOAN).values_list('receipt_id', flat=True))
    rq = Receipt.objects.filter(unit=unit, is_void=False).exclude(pk__in=loan_receipts)
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


def loan_rows(unit, owner=None):
    """The owner's loan on its own: what was set up, each payment on it, and the balance after each. [] when the unit has none."""
    cq = Charge.objects.filter(unit=unit, is_void=False, kind=Charge.Kind.LOAN)
    if owner is not None:
        cq = cq.filter(owner=owner)
    items = [{'date': c.due_date, 'order': 0, 'what': c.description, 'charge': c.amount, 'payment': None} for c in cq]
    for a in Application.objects.filter(charge__in=cq, receipt__is_void=False).select_related('receipt'):
        how = a.receipt.get_method_display() + (f' {a.receipt.reference}' if a.receipt.reference else '')
        items.append({'date': a.date, 'order': 1, 'what': f'Payment, {how}', 'charge': None, 'payment': a.amount})
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
    for c in Charge.objects.filter(association=association, is_void=False).exclude(kind=Charge.Kind.LOAN).select_related('unit', 'owner').prefetch_related('applications'):
        bal = c.amount - sum((a.amount for a in c.applications.all() if a.date <= as_of), ZERO)       # what was still owed on that date, not today
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
