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
from django.db.models import Max, Min, Q, Sum

from core import memo as request_memo

from . import standard_chart as sc
from .models import ZERO, Account, AssociationProfile, JournalEntry, JournalLine, MasterAccount, MonthLock

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
    # While a page is being built (a GET: core/memo.py) the settings row is read once for the page; elsewhere it is read every time, as before.
    return request_memo.get(('profile_for', association.pk), lambda: AssociationProfile.objects.get_or_create(property=association)[0])


# ---------------------------------------------------------------- accounts

OFFICE_NAME = 'proper realty co office'


def is_office(association):
    """True for the management company's own books (Proper Realty Co Office 309): a general property named for the office. Its chart is its own, built from the company's QuickBooks
    accounts, so the association master chart is never merged into it (ensure_chart skips it)."""
    return getattr(association, 'property_type', '') == 'general' and (association.name or '').strip().lower().startswith(OFFICE_NAME)


def trust_clearing_account(association):
    """The liability on the property a check from Proper's trust account paid for: 'Paid from Proper Realty trust account'. Made on the spot when the property's chart lacks it."""
    acct, _ = Account.objects.get_or_create(association=association, code=sc.TRUST_CLEARING, defaults={'name': 'Paid from Proper Realty trust account', 'kind': Account.Kind.LIABILITY})
    return acct


def master_chart():
    """The master chart a new association starts from, as (number, name, kind). Falls back to the built-in standard if the master list is empty."""
    rows = list(MasterAccount.objects.filter(is_active=True).order_by('code').values_list('code', 'name', 'kind'))
    return rows or list(sc.CHART)


def ensure_chart(association):
    """Make the association's accounts match the company's one chart: add any it lacks, and bring a name or an on/off setting in line. There is one chart for every association,
    so this is how a new association gets it and how an older one keeps up. Safe to run again; returns how many accounts it added. The company's own office books are not an association: skipped."""
    if is_office(association):
        return 0
    mine = {a.code: a for a in Account.objects.filter(association=association)}
    rows = {code: (name, kind, True, '') for code, name, kind in sc.CHART if code in sc.SYSTEM_CODES}
    masters = list(MasterAccount.objects.values_list('code', 'name', 'kind', 'is_active', 'budget_section'))
    rows.update({code: (name, kind, active, section) for code, name, kind, active, section in masters} if masters else {code: (name, kind, True, '') for code, name, kind in sc.CHART})
    new = []
    for code, (name, kind, active, section) in sorted(rows.items()):
        a = mine.get(code)
        if a is None:
            new.append(Account(association=association, code=code, name=name, kind=kind, is_active=active, budget_section=section))
        elif (a.name != name and not a.name_locked) or a.is_active != active or a.budget_section != section:
            if not a.name_locked:                  # an association's own wording for an account (name_locked) stays; its on/off and budget section still follow the company chart
                a.name = name
            a.is_active, a.budget_section = active, section
            a.save(update_fields=['name', 'is_active', 'budget_section'])
    if getattr(association, 'property_type', None) == 'commercial':
        have = set(mine) | {a.code for a in new}
        new += [Account(association=association, code=code, name=name, kind=kind, is_active=True, budget_section='') for code, name, kind in sc.COMMERCIAL_CHART if code not in have]
    Account.objects.bulk_create(new)
    return len(new)


def sync_all_charts():
    """ensure_chart for every association that has books."""
    from core.models import Property
    for prop in Property.objects.filter(pk__in=set(Account.objects.values_list('association_id', flat=True))):
        ensure_chart(prop)


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


ASSET_SECTIONS = ('Cash', 'Receivables', 'Other assets')


def balance_sheet(association, day, basis=ACCRUAL):
    bal = balances_as_of(association, day, basis)
    accts = {a.pk: a for a in Account.objects.filter(association=association)}
    sections = defaultdict(list)
    for account_id, amount in bal.items():
        a = accts[account_id]
        if not a.is_balance_sheet or not amount:
            continue
        section = sc.section(a.code)
        debit_side = section in ASSET_SECTIONS or a.normal_debit      # what sits in the asset sections counts as debits less credits, so a contra-asset (a credit balance) reduces them
        sections[section].append({'account': a, 'amount': amount if debit_side else -amount})
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


class _Plain:
    """A bare object with the attributes a general-ledger row's line and entry are read for (the old system's lines are not journal entries)."""
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def get_source_display(self):
        return 'Old system'


def _old_system_row(day, who, label, ref, remarks, debit, credit, eff=None):
    line = _Plain(debit=debit, credit=credit, memo=remarks, owner=None, owner_id=None, vendor=None, vendor_id=None, unit=None, unit_id=None)
    entry = _Plain(date=day, number=label, memo=remarks, source='old_system')
    return {'line': line, 'entry': entry, 'who': who, 'label': label, 'ref': ref, 'eff': eff or day}


def _old_system_first_month(association, basis):
    """The first month the old system's lines for this association and book cover, or None when none were loaded (remembered for the page being built)."""
    from .models import LedgerDetail
    return request_memo.get(('old_system_first', association.pk, basis), lambda: LedgerDetail.objects.filter(association=association, book=basis).aggregate(m=Min('period'))['m'])


def _old_system_ledger(association, acct, start, end, basis, detail, first_month):
    """The general ledger for an account whose year to date was carried across from the old system (Yardi) as one opening entry: the old system's own lines, as it kept them (date, who,
    control number, reference, remarks), stand in the place of that opening entry. The carried figure is unchanged: what the opening entry held, less the old lines, is the balance the
    account began the year with (a balance-sheet account) or a labeled adjustment (an income or expense account, where the figure we carry differs from the old ledger's lines)."""
    year_start = profile_for(association).fiscal_year_start(first_month)
    ls = lines(association, basis).filter(account=acct)
    # What came across as one number: the opening entry and the month-by-month entries made from it. Corrections made to the opening afterwards are real entries with their own wording and stay as they are.
    carry = Q(entry__source=JournalEntry.Source.OPENING) & (Q(entry__memo__startswith='Opening balances') | Q(entry__memo__startswith='Old system'))
    carried_agg = ls.filter(carry).aggregate(d=Sum('debit'), c=Sum('credit'), last=Max('entry__date'))
    if not detail and not carried_agg['last']:
        return None                                                  # nothing came across on this account: it is an ordinary ledger
    cutoff = max(([month_end(max(max(d.date, d.period) for d in detail))] if detail else []) + ([carried_agg['last']] if carried_agg['last'] else []))
    gap = money(carried_agg['d'] or 0) - money(carried_agg['c'] or 0) - sum((d.debit - d.credit for d in detail), ZERO)
    items = []                                              # (sort key, row)
    if gap:
        if acct.is_balance_sheet:
            day, label, text = year_start - timedelta(days=1), 'Balance forward', 'Balance the account began the year with, in the old system'
            items.append(((day, 0, 0), _old_system_row(day, '', label, '', text, gap if gap > 0 else ZERO, -gap if gap < 0 else ZERO)))
        else:
            text = 'The figure carried across differs from the old ledger lines (corrections made when the books moved)'
            items.append(((cutoff, 1, 0), _old_system_row(cutoff, '', 'Adjustment', '', text, gap if gap > 0 else ZERO, -gap if gap < 0 else ZERO)))
    for d in detail:
        eff = max(d.date, d.period)                           # the old system counts a line in its period, whatever day it carries
        items.append(((eff, 0, d.id), _old_system_row(d.date, d.who, d.ctrl or 'Old system', d.reference, d.remarks, d.debit, d.credit, eff)))
    native = ls.exclude(carry).filter(entry__date__lte=end).select_related('entry', 'owner', 'vendor')
    for l in native:
        items.append(((l.entry.date, 2, l.entry.number, l.id), {'line': l, 'entry': l.entry, 'who': '', 'label': f'J-{l.entry.number}', 'ref': '', 'eff': l.entry.date}))
    items.sort(key=lambda kr: kr[0])
    floor = None if acct.is_balance_sheet else profile_for(association).fiscal_year_start(start)       # an income or expense account starts over each fiscal year
    start_balance = sum((r['line'].debit - r['line'].credit for k, r in items if r['eff'] < start and (floor is None or r['eff'] >= floor)), ZERO)
    running, rows = start_balance, []
    for k, r in items:
        if start <= r['eff'] <= end:
            running += r['line'].debit - r['line'].credit
            r['running'] = running
            rows.append(r)
    return {'account': acct, 'rows': rows, 'start_balance': start_balance, 'end_balance': running}


def general_ledger(association, acct, start, end, basis=ACCRUAL):
    """Every line on one account in a period, with a running balance that starts from what the account held before. An account whose year to date came across from the old system shows
    that system's own lines (see _old_system_ledger)."""
    from .models import LedgerDetail
    detail = list(LedgerDetail.objects.filter(association=association, account=acct, book=basis).order_by('date', 'id'))
    first_month = _old_system_first_month(association, basis)
    if first_month is not None:
        carried = _old_system_ledger(association, acct, start, end, basis, detail, first_month)
        if carried is not None:
            return carried
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


def general_ledger_range(association, first, last, start, end, basis=ACCRUAL):
    """The general ledger for every account from `first` through `last` (by account number, either order), one section each, as a list of general_ledger() results. An account with no
    activity in the period and nothing in it before is left out; `last` None is just `first`."""
    if last is None or last.pk == first.pk:
        return [general_ledger(association, first, start, end, basis)]
    low, high = sorted((first.code, last.code))
    out = []
    for acct in Account.objects.filter(association=association, code__gte=low, code__lte=high).order_by('code'):
        gl = general_ledger(association, acct, start, end, basis)
        if gl['rows'] or gl['start_balance']:
            gl['debits'] = sum((r['line'].debit for r in gl['rows']), ZERO)
            gl['credits'] = sum((r['line'].credit for r in gl['rows']), ZERO)
            out.append(gl)
    return out


def cash_balance(bank_account, day):
    """What the books say is in a bank account on a day (accrual book; cash lines are the same in both)."""
    agg = lines(bank_account.association, ACCRUAL).filter(account=bank_account.account, entry__date__lte=day).aggregate(d=Sum('debit'), c=Sum('credit'))
    return money(agg['d'] or 0) - money(agg['c'] or 0)
