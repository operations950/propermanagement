"""Posting entries and reading the books back.

post_entry is the only way a JournalEntry is made: it refuses an entry that does not balance, one that touches another association's accounts, and one dated
in a closed month. Entries are never edited; reverse_entry posts the mirror image.

Two books come from one set of entries. An entry's `basis` is 'both', 'accrual' or 'cash'; the accrual book is every entry that is 'both' or 'accrual',
the cash book every entry that is 'both' or 'cash'.
"""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Max, Q, Sum

from . import standard_chart as sc
from .models import ZERO, Account, AssociationProfile, JournalEntry, JournalLine, MonthLock

ACCRUAL, CASH = 'accrual', 'cash'


class LedgerError(Exception):
    """Something that would put the books wrong; the message is written for the person at the screen."""


def money(value):
    return Decimal(str(value)).quantize(Decimal('0.01'))


def month_start(day):
    return day.replace(day=1)


def next_month(day):
    return (month_start(day) + timedelta(days=32)).replace(day=1)


def previous_month(day):
    return month_start(month_start(day) - timedelta(days=1))


def month_end(day):
    return next_month(day) - timedelta(days=1)


def is_locked(association, day):
    return MonthLock.objects.filter(association=association, month=month_start(day)).exists()


def profile_for(association):
    return AssociationProfile.objects.get_or_create(property=association)[0]


# ---------------------------------------------------------------- accounts

def ensure_chart(association):
    """Give an association the standard chart. Safe to run again: existing accounts are left alone."""
    have = set(Account.objects.filter(association=association).values_list('code', flat=True))
    new = [Account(association=association, code=code, name=name, kind=kind) for code, name, kind in sc.CHART if code not in have]
    Account.objects.bulk_create(new)
    return len(new)


def account(association, code):
    """An account by its number. Raises LedgerError if the association does not have it."""
    try:
        return Account.objects.get(association=association, code=str(code))
    except Account.DoesNotExist:
        raise LedgerError(f'This association has no account {code}.')


def _as_account(association, value):
    if isinstance(value, Account):
        if value.association_id != association.pk:
            raise LedgerError('That account belongs to a different association.')
        return value
    return account(association, value)


# ---------------------------------------------------------------- posting

@transaction.atomic
def post_entry(association, day, lines, memo='', basis=JournalEntry.Basis.BOTH, source=JournalEntry.Source.MANUAL, source_ref='', user=None,
               reverses=None, allow_locked=False):
    """Post one balanced entry. `lines` is a list of dicts: account (an Account or its number), debit, credit, and optionally unit, owner, vendor, memo."""
    if not allow_locked and is_locked(association, day):
        raise LedgerError(f'{day:%B %Y} is closed. Open the month again to post into it.')
    built = []
    debits = credits = ZERO
    for raw in lines:
        debit, credit = money(raw.get('debit') or 0), money(raw.get('credit') or 0)
        if debit < 0 or credit < 0:
            raise LedgerError('An amount cannot be negative.')
        if debit and credit:
            raise LedgerError('A line is a debit or a credit, not both.')
        if not debit and not credit:
            continue
        built.append(JournalLine(account=_as_account(association, raw['account']), debit=debit, credit=credit, unit=raw.get('unit'), owner=raw.get('owner'),
                                 vendor=raw.get('vendor'), memo=(raw.get('memo') or '')[:300]))
        debits += debit
        credits += credit
    if not built:
        raise LedgerError('An entry needs at least one line with an amount.')
    if debits != credits:
        raise LedgerError(f'The entry does not balance: debits ${debits:,.2f}, credits ${credits:,.2f}.')
    last = JournalEntry.objects.filter(association=association).aggregate(m=Max('number'))['m'] or 0
    entry = JournalEntry.objects.create(association=association, number=last + 1, date=day, memo=(memo or '')[:300], basis=basis, source=source,
                                        source_ref=source_ref, created_by=user, reverses=reverses)
    for line in built:
        line.entry = entry
    JournalLine.objects.bulk_create(built)
    return entry


@transaction.atomic
def reverse_entry(entry, day, user=None, memo=''):
    """Undo an entry by posting its mirror image. The original stays, so the history is whole."""
    if entry.reversed_by.exists():
        raise LedgerError('That entry has already been reversed.')
    lines = [{'account': l.account, 'debit': l.credit, 'credit': l.debit, 'unit': l.unit, 'owner': l.owner, 'vendor': l.vendor, 'memo': l.memo} for l in entry.lines.all()]
    return post_entry(entry.association, day, lines, memo=memo or f'Reversal of J-{entry.number}', basis=entry.basis, source=JournalEntry.Source.REVERSAL,
                      source_ref=f'entry:{entry.pk}', user=user, reverses=entry)


def lock_month(association, month, user=None):
    MonthLock.objects.get_or_create(association=association, month=month_start(month), defaults={'locked_by': user})


def unlock_month(association, month):
    MonthLock.objects.filter(association=association, month=month_start(month)).delete()


# ---------------------------------------------------------------- reading the books

def _book(basis):
    return Q(entry__basis__in=[ACCRUAL if basis != CASH else CASH, 'both'])


def lines(association, basis=ACCRUAL):
    return JournalLine.objects.filter(entry__association=association).filter(_book(basis))


def _sums(qs):
    out = defaultdict(lambda: [ZERO, ZERO])
    for row in qs.values('account_id').annotate(d=Sum('debit'), c=Sum('credit')):
        out[row['account_id']] = [money(row['d'] or 0), money(row['c'] or 0)]
    return out


def balances_as_of(association, day, basis=ACCRUAL):
    """{account_id: signed balance (debits minus credits)} of every account on a day; income and expense accounts count only the fiscal year's activity."""
    fy = profile_for(association).fiscal_year_start(day)
    everything = _sums(lines(association, basis).filter(entry__date__lte=day))
    this_year = _sums(lines(association, basis).filter(entry__date__gte=fy, entry__date__lte=day))
    kinds = dict(Account.objects.filter(association=association).values_list('id', 'kind'))
    out = {}
    for account_id, (d, c) in everything.items():
        if kinds[account_id] in (Account.Kind.INCOME, Account.Kind.EXPENSE):
            d, c = this_year[account_id] if account_id in this_year else (ZERO, ZERO)
        out[account_id] = d - c
    return out


def prior_years_result(association, day, basis=ACCRUAL):
    """Net income of fiscal years before the one `day` is in, as a signed (debit minus credit) amount: what has rolled into retained earnings."""
    fy = profile_for(association).fiscal_year_start(day)
    qs = lines(association, basis).filter(entry__date__lt=fy, account__kind__in=[Account.Kind.INCOME, Account.Kind.EXPENSE])
    agg = qs.aggregate(d=Sum('debit'), c=Sum('credit'))
    return money(agg['d'] or 0) - money(agg['c'] or 0)


def trial_balance(association, start, end, basis=ACCRUAL):
    """Yardi's layout: for each account the balance forward, the period's debits and credits and the ending balance (debit positive)."""
    fy = profile_for(association).fiscal_year_start(start)
    before_all = _sums(lines(association, basis).filter(entry__date__lt=start))
    before_year = _sums(lines(association, basis).filter(entry__date__gte=fy, entry__date__lt=start))
    during = _sums(lines(association, basis).filter(entry__date__gte=start, entry__date__lte=end))
    rows = []
    for acct in Account.objects.filter(association=association):
        if acct.kind in (Account.Kind.INCOME, Account.Kind.EXPENSE):
            fd, fc = before_year[acct.pk] if acct.pk in before_year else (ZERO, ZERO)
        else:
            fd, fc = before_all[acct.pk] if acct.pk in before_all else (ZERO, ZERO)
        d, c = during[acct.pk] if acct.pk in during else (ZERO, ZERO)
        forward = fd - fc
        if not (forward or d or c):
            continue
        rows.append({'account': acct, 'forward': forward, 'debit': d, 'credit': c, 'ending': forward + d - c})
    prior = prior_years_result(association, start, basis)
    if prior:
        # earlier years' income and expense, rolled into retained earnings so the trial balance still adds up
        rows.append({'account': Account(code='', name="Earlier years' results (rolled into retained earnings)", kind=Account.Kind.EQUITY),
                     'forward': prior, 'debit': ZERO, 'credit': ZERO, 'ending': prior})
    rows.sort(key=lambda r: (r['account'].code or '9999'))
    totals = {k: sum((r[k] for r in rows), ZERO) for k in ('forward', 'debit', 'credit', 'ending')}
    return {'rows': rows, 'totals': totals, 'start': start, 'end': end, 'basis': basis}


def income_statement(association, start, end, basis=ACCRUAL):
    sums = _sums(lines(association, basis).filter(entry__date__gte=start, entry__date__lte=end, account__kind__in=[Account.Kind.INCOME, Account.Kind.EXPENSE]))
    income, expense = [], []
    for acct in Account.objects.filter(association=association, kind__in=[Account.Kind.INCOME, Account.Kind.EXPENSE]):
        if acct.pk not in sums:
            continue
        d, c = sums[acct.pk]
        if acct.kind == Account.Kind.INCOME:
            income.append({'account': acct, 'amount': c - d})
        else:
            expense.append({'account': acct, 'amount': d - c})
    ti, te = sum((r['amount'] for r in income), ZERO), sum((r['amount'] for r in expense), ZERO)
    return {'income': income, 'expense': expense, 'total_income': ti, 'total_expense': te, 'net': ti - te, 'start': start, 'end': end, 'basis': basis}


def balance_sheet(association, day, basis=ACCRUAL):
    bal = balances_as_of(association, day, basis)
    accts = {a.pk: a for a in Account.objects.filter(association=association)}
    sections = defaultdict(list)
    for account_id, amount in bal.items():
        a = accts[account_id]
        if not a.is_balance_sheet or not amount:
            continue
        sections[sc.section(a.code)].append({'account': a, 'amount': amount if a.normal_debit else -amount})
    for rows in sections.values():
        rows.sort(key=lambda r: r['account'].code)
    net_year = sum((-v for k, v in bal.items() if accts[k].kind in (Account.Kind.INCOME, Account.Kind.EXPENSE)), ZERO)
    prior = -prior_years_result(association, day, basis)
    total = lambda names: sum((r['amount'] for n in names for r in sections.get(n, [])), ZERO)
    assets = total(['Cash', 'Receivables', 'Other assets'])
    liabilities = total(['Liabilities'])
    equity = total(['Equity', 'Reserve funds']) + net_year + prior
    return {'sections': dict(sections), 'net_year': net_year, 'prior_years': prior, 'assets': assets, 'liabilities': liabilities, 'equity': equity,
            'balanced': assets == liabilities + equity, 'day': day, 'basis': basis}


def general_ledger(association, acct, start, end, basis=ACCRUAL):
    """Every line on one account in a period, with a running balance that starts from what the account held before."""
    before = lines(association, basis).filter(account=acct, entry__date__lt=start)
    if not acct.is_balance_sheet:
        before = before.filter(entry__date__gte=profile_for(association).fiscal_year_start(start))
    agg = before.aggregate(d=Sum('debit'), c=Sum('credit'))
    running = money(agg['d'] or 0) - money(agg['c'] or 0)
    start_balance = running
    rows = []
    for l in lines(association, basis).filter(account=acct, entry__date__gte=start, entry__date__lte=end).select_related('entry', 'owner', 'vendor').order_by('entry__date', 'entry__number', 'id'):
        running += l.debit - l.credit
        rows.append({'line': l, 'entry': l.entry, 'running': running})
    return {'account': acct, 'rows': rows, 'start_balance': start_balance, 'end_balance': running}


def cash_balance(bank_account, day):
    """What the books say is in a bank account on a day (accrual book; cash lines are the same in both)."""
    agg = lines(bank_account.association, ACCRUAL).filter(account=bank_account.account, entry__date__lte=day).aggregate(d=Sum('debit'), c=Sum('credit'))
    return money(agg['d'] or 0) - money(agg['c'] or 0)
