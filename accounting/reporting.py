"""The financial statements as tables: balance sheet, profit and loss, and budget vs actual.

Each builder returns one plain structure that a single template draws (accounting/_stmt.html):

    {'columns': [{'label', 'sub'}], 'blocks': [...], 'finals': [...], 'notes': [...]}

A block is a section (Assets, Expenses...) made of groups of account lines. A total line is shown only where it adds something: a group or a section with a
single line gets no total (the line already says it), and a section with one group shows the group's lines under the section's own heading.
Numbers are formatted here, so the template only prints: negatives in parentheses, zero as a dash.
"""
from datetime import date
from decimal import Decimal

from django.db.models import Sum

from . import budgeting as bg
from . import ledger
from .models import ZERO, Account, Budget, JournalEntry

ACCRUAL = ledger.ACCRUAL


# ---------------------------------------------------------------- formatting

def fmt(v, kind='cents'):
    """{'t': text, 'neg': bool}: (1,234.56) for a negative, a dash for zero, blank for nothing."""
    if v is None:
        return {'t': '', 'neg': False}
    d = Decimal(str(v))
    if kind == 'pct':
        return {'t': '–' if abs(d) < Decimal('0.05') else f'{d:.1f}%', 'neg': d < 0}
    q = d.quantize(Decimal('1')) if kind == 'dollars' else d.quantize(Decimal('0.01'))
    if q == 0:
        return {'t': '–', 'neg': False}
    return {'t': f'({abs(q):,})' if q < 0 else f'{q:,}', 'neg': q < 0}


def _sum(rows, n):
    return [sum((r['raw'][i] for r in rows), ZERO) for i in range(n)]


def _line(label, raw, kinds, code=''):
    return {'label': label, 'code': code, 'raw': raw, 'values': [fmt(v, k) for v, k in zip(raw, kinds)]}


def _block(title, groups, total_label, kinds):
    """A section of groups of lines with its totals. The one-line rule lives here."""
    groups = [g for g in groups if g['lines']]
    if not groups:
        return None
    n = len(kinds)
    n_lines = sum(len(g['lines']) for g in groups)
    many = len(groups) > 1
    out = []
    for g in groups:
        raw = _sum(g['lines'], n)
        out.append({'title': g['title'], 'lines': g['lines'], 'total_label': f'Total {g["title"].lower()}', 'raw': raw, 'values': [fmt(v, k) for v, k in zip(raw, kinds)],
                    'show_total': many and len(g['lines']) > 1})
    raw = [sum((g['raw'][i] for g in out), ZERO) for i in range(n)]
    return {'title': title, 'groups': out, 'show_group_titles': many, 'total_label': total_label, 'raw': raw, 'values': [fmt(v, k) for v, k in zip(raw, kinds)],
            'show_total': bool(total_label) and n_lines > 1}


def _final(label, raw, kinds, style='grand'):
    return {'label': label, 'raw': raw, 'values': [fmt(v, k) for v, k in zip(raw, kinds)], 'style': style}


def _with_change(columns, blocks, finals, kinds):
    """Add a last column: the first column less the second."""
    columns.append({'label': 'Change', 'sub': ''})
    kinds.append(kinds[0])
    for b in blocks:
        for g in b['groups']:
            for l in g['lines']:
                l['raw'] = l['raw'] + [l['raw'][0] - l['raw'][1]]
                l['values'].append(fmt(l['raw'][-1], kinds[0]))
            g['raw'] = g['raw'] + [g['raw'][0] - g['raw'][1]]
            g['values'].append(fmt(g['raw'][-1], kinds[0]))
        b['raw'] = b['raw'] + [b['raw'][0] - b['raw'][1]]
        b['values'].append(fmt(b['raw'][-1], kinds[0]))
    for f in finals:
        f['raw'] = f['raw'] + [f['raw'][0] - f['raw'][1]]
        f['values'].append(fmt(f['raw'][-1], kinds[0]))


# ---------------------------------------------------------------- periods

def month_ends(prop, end):
    """The end of each month from the start of the fiscal year through `end` (the last one is `end` itself)."""
    fy = ledger.profile_for(prop).fiscal_year_start(end)
    out, cur = [], fy
    while cur <= end:
        out.append(min(ledger.month_end(cur), end))
        cur = ledger.next_month(cur)
    return out


def _label(day, full=False):
    return f'{day:%b %Y}' if not full else f'{day:%b} {day.day}, {day.year}'


# ---------------------------------------------------------------- balance sheet

def balance_sheet(prop, end, basis=ACCRUAL, view='compare'):
    """view: 'compare' (the date, a month earlier, the change), 'year' (the date, a year earlier, the change) or 'months' (every month-end of the year so far)."""
    if view == 'months':
        days = month_ends(prop, end)
        cols = [{'label': _label(d), 'sub': ''} for d in days]
    elif view == 'year':
        try:
            before = end.replace(year=end.year - 1)
        except ValueError:
            before = end.replace(year=end.year - 1, day=28)
        days = [end, before]
        cols = [{'label': _label(end, True), 'sub': ''}, {'label': _label(before, True), 'sub': ''}]
    else:
        before = ledger.month_end(ledger.previous_month(end))
        days = [end, before]
        cols = [{'label': _label(end, True), 'sub': ''}, {'label': _label(before, True), 'sub': ''}]
    n = len(days)
    kinds = ['cents'] * n
    sheets = [ledger.balance_sheet(prop, d, basis) for d in days]

    def group(title, sections, extra=()):
        by = {}
        for i, bs in enumerate(sheets):
            for name in sections:
                for r in bs['sections'].get(name, []):
                    by.setdefault(r['account'].pk, {'account': r['account'], 'raw': [ZERO] * n})['raw'][i] = r['amount']
        lines = [_line(f'{r["account"].name}', r['raw'], kinds, r['account'].code) for r in sorted(by.values(), key=lambda r: r['account'].code)]
        for label, key in extra:
            raw = [bs[key] for bs in sheets]
            if any(raw):
                lines.append(_line(label, raw, kinds))
        return {'title': title, 'lines': lines}

    assets = _block('Assets', [group('Cash', ['Cash']), group('Receivables', ['Receivables']), group('Other assets', ['Other assets'])], 'Total assets', kinds)
    liabilities = _block('Liabilities', [group('Liabilities', ['Liabilities'])], 'Total liabilities', kinds)
    funds = _block('Funds', [group('Equity', ['Equity'], extra=[("Earlier years' results", 'prior_years'), ("This year's net income", 'net_year')]), group('Reserve funds', ['Reserve funds'])], 'Total funds', kinds)
    blocks = [b for b in (assets, liabilities, funds) if b]
    total_assets = [bs['assets'] for bs in sheets]
    total_lf = [bs['liabilities'] + bs['equity'] for bs in sheets]
    finals = [_final('Total liabilities and funds', total_lf, kinds)]
    notes = []
    off = [a - b for a, b in zip(total_assets, total_lf)]
    if any(off):
        finals.append(_final('Out of balance', off, kinds, 'warn'))
    else:
        notes.append('In balance: total assets equal total liabilities and funds.')
    if not blocks:
        blocks = []
    if view != 'months':
        _with_change(cols, blocks, finals, kinds)
    return {'columns': cols, 'blocks': blocks, 'finals': finals, 'notes': notes, 'balanced': not any(off), 'days': days}


# ---------------------------------------------------------------- profit and loss

EXPENSE_GROUPS = ['Administrative', 'Utilities', 'Operating', 'Reserve transfer']


def expense_group(acct):
    return 'Reserve transfer' if acct.code in bg.LEFT_OUT else bg.section_of(acct)


def profit_and_loss(prop, end, basis=ACCRUAL, view='compare'):
    """view: 'compare' (this month, last month, year to date), 'month_ytd' (this month and year to date: the board package) or 'months' (each month of the year so far, and the year)."""
    fy = ledger.profile_for(prop).fiscal_year_start(end)
    first = ledger.month_start(end)
    if view == 'months':
        periods, cur = [], fy
        while cur <= end:
            periods.append((_label(cur), cur, min(ledger.month_end(cur), end)))
            cur = ledger.next_month(cur)
        periods.append(('Year to date', fy, end))
    elif view == 'month_ytd':
        periods = [(_label(first), first, end), ('Year to date', fy, end)]
    else:
        prev = ledger.previous_month(end)
        periods = [(_label(first), first, end), (_label(prev), prev, ledger.month_end(prev)), ('Year to date', fy, end)]
    n = len(periods)
    kinds = ['cents'] * n
    stmts = [ledger.income_statement(prop, s, e, basis) for _, s, e in periods]
    cols = [{'label': p[0], 'sub': ''} for p in periods]

    def lines_of(kind):
        by = {}
        for i, st in enumerate(stmts):
            for r in st[kind]:
                by.setdefault(r['account'].pk, {'account': r['account'], 'raw': [ZERO] * n})['raw'][i] = r['amount']
        return sorted(by.values(), key=lambda r: r['account'].code)

    inc = lines_of('income')
    exp = lines_of('expense')
    income = _block('Income', [{'title': 'Income', 'lines': [_line(r['account'].name, r['raw'], kinds, r['account'].code) for r in inc]}], 'Total income', kinds)
    groups = [{'title': g, 'lines': [_line(r['account'].name, r['raw'], kinds, r['account'].code) for r in exp if expense_group(r['account']) == g]} for g in EXPENSE_GROUPS]
    expenses = _block('Expenses', groups, 'Total expenses', kinds)
    total_inc = income['raw'] if income else [ZERO] * n
    total_exp = expenses['raw'] if expenses else [ZERO] * n
    net = [a - b for a, b in zip(total_inc, total_exp)]
    return {'columns': cols, 'blocks': [b for b in (income, expenses) if b], 'finals': [_final('Net income', net, kinds)], 'notes': basis_notes(prop, basis, first, end), 'periods': periods}


# ---------------------------------------------------------------- budget vs actual

def budget_vs_actual(prop, end, basis=ACCRUAL):
    """The month and the year to date against the budget of the fiscal year `end` is in. The budget is spread evenly across the months, except on a line that has its own monthly amounts
    (BudgetLine.months, when they add up to the annual amount)."""
    profile = ledger.profile_for(prop)
    fy = profile.fiscal_year_start(end)
    budget = Budget.objects.filter(association=prop, year=fy.year).first()
    blines = list(budget.lines.all()) if budget else []
    annual = {l.account_id: l.annual_amount for l in blines}
    timed = {}                                       # account -> twelve monthly amounts, when the line carries its own timing
    for l in blines:
        m = l.months
        if isinstance(m, list) and len(m) == 12:
            try:
                m = [Decimal(str(x or 0)) for x in m]
            except Exception:
                continue
            if sum(m, ZERO).quantize(Decimal('0.01')) == l.annual_amount.quantize(Decimal('0.01')):
                timed[l.account_id] = m
    first = ledger.month_start(end)
    months_done = (end.year - fy.year) * 12 + end.month - fy.month + 1
    accts = {a.pk: a for a in Account.objects.filter(association=prop)}

    prior = bg.prior_ytd(prop, fy.year)               # reserve contributions made before our books began (loaded from the old system), part of the year to date

    def actuals(start, stop, year_to_date=False):
        qs = ledger.lines(prop, basis).filter(entry__date__gte=start, entry__date__lte=stop)
        sums = ledger._sums(qs)
        # A reserve account's line in the opening entry is the fund's BALANCE carried from the old system, not what was put in this year: it is left out, and the contributions made
        # before our books are added for the year to date (the same rule the budget builder uses, budgeting._actuals).
        reserve_ids = [pk for pk, a in accts.items() if bg.is_reserve(a)]
        opening = ledger._sums(qs.filter(entry__source=JournalEntry.Source.OPENING, account_id__in=reserve_ids))
        moved = ledger._sums(qs.filter(account_id__in=reserve_ids, entry_id__in=bg.transfer_entry_ids(qs.exclude(entry__source=JournalEntry.Source.OPENING), reserve_ids)))      # fund-to-fund moves are not contributions
        for pk, (d, c) in moved.items():
            opening[pk] = [opening[pk][0] + d, opening[pk][1] + c] if pk in opening else [d, c]
        out = {}
        for pk, (d, c) in sums.items():
            a = accts[pk]
            if a.kind == Account.Kind.INCOME:
                out[pk] = c - d
            elif a.kind == Account.Kind.EXPENSE and a.code not in bg.LEFT_OUT:
                out[pk] = d - c
            elif bg.is_reserve(a):
                put_in = c - opening[pk][1] if pk in opening else c            # what was put into the reserve fund
                if put_in:                                                      # a fund that only carries its balance has no line (unless the budget names it)
                    out[pk] = put_in
        if year_to_date:
            for pk, amount in prior.items():
                if pk in accts and bg.is_reserve(accts[pk]):
                    out[pk] = out.get(pk, ZERO) + amount
        return out

    month, ytd = actuals(first, end), actuals(fy, end, True)
    ids = {pk for pk in set(month) | set(ytd) | set(annual) if pk in accts and (accts[pk].kind in (Account.Kind.INCOME, Account.Kind.EXPENSE) or bg.is_reserve(accts[pk])) and accts[pk].code not in bg.LEFT_OUT}
    kinds = ['dollars'] * 5 + ['dollars', 'pct', 'dollars', 'dollars']
    cols = [{'label': 'Actual', 'sub': _label(first)}, {'label': 'Budget', 'sub': _label(first)}, {'label': 'Over / (under)', 'sub': _label(first)},
            {'label': 'Actual', 'sub': 'Year to date'}, {'label': 'Budget', 'sub': 'Year to date'}, {'label': 'Over / (under)', 'sub': 'Year to date'}, {'label': '% of budget', 'sub': 'Year to date'},
            {'label': 'Annual budget', 'sub': str(fy.year)}, {'label': 'Remaining', 'sub': ''}]

    def row(pk):
        a = accts[pk]
        yb = annual.get(pk, ZERO)
        if pk in timed:
            mb, tb = timed[pk][months_done - 1], sum(timed[pk][:months_done], ZERO)
        else:
            mb, tb = yb / 12, yb * months_done / 12
        ma, ya = month.get(pk, ZERO), ytd.get(pk, ZERO)
        pctv = (ya / tb * 100) if tb else None
        raw = [ma, mb, ma - mb, ya, tb, ya - tb, pctv, yb, yb - ya]
        return {'label': a.name, 'code': a.code, 'raw': [r if r is not None else None for r in raw], 'values': [fmt(v, k) for v, k in zip(raw, kinds)]}

    def grp(title, test):
        return {'title': title, 'lines': [row(pk) for pk in sorted(ids, key=lambda p: accts[p].code) if test(accts[pk])]}

    def blk(title, groups, total_label):
        groups = [g for g in groups if g['lines']]
        if not groups:
            return None
        many = len(groups) > 1
        n_lines = sum(len(g['lines']) for g in groups)

        def tot(lines):
            r = [None if i == 6 else sum((l['raw'][i] for l in lines), ZERO) for i in range(9)]
            r[6] = (r[3] / r[4] * 100) if r[4] else None
            return r
        out = []
        for g in groups:
            r = tot(g['lines'])
            out.append({'title': g['title'], 'lines': g['lines'], 'total_label': f'Total {g["title"].lower()}', 'raw': r, 'values': [fmt(v, k) for v, k in zip(r, kinds)], 'show_total': many and len(g['lines']) > 1})
        allr = tot([l for g in groups for l in g['lines']])
        return {'title': title, 'groups': out, 'show_group_titles': many, 'total_label': total_label, 'raw': allr, 'values': [fmt(v, k) for v, k in zip(allr, kinds)], 'show_total': n_lines > 1}

    income = blk('Income', [grp('Income', lambda a: a.kind == Account.Kind.INCOME)], 'Total income')
    expenses = blk('Expenses', [grp(g, lambda a, g=g: a.kind == Account.Kind.EXPENSE and bg.section_of(a) == g) for g in ('Administrative', 'Utilities', 'Operating')], 'Total expenses')
    reserves = blk('Reserve contributions', [grp('Reserve contributions', bg.is_reserve)], 'Total reserve contributions')
    blocks = [b for b in (income, expenses, reserves) if b]
    finals = []
    if income and expenses:
        net = [a - b if a is not None and b is not None else None for a, b in zip(income['raw'], expenses['raw'])]
        net[6] = None
        net[2] = income['raw'][2] - expenses['raw'][2]
        net[5] = income['raw'][5] - expenses['raw'][5]
        finals.append({'label': 'Net income', 'raw': net, 'values': [fmt(v, k) for v, k in zip(net, kinds)], 'style': 'grand'})
    notes = []
    if not annual:
        notes.append(f'No budget is loaded for {fy.year}.')
    if timed and len(timed) == len(annual):
        notes.append('The budget is by month, as the budget was set (a line is not spread evenly). The transfer to reserves is not an expense here: the reserve lines carry it.')
    elif timed:
        notes.append('Lines with their own monthly budget follow it; the others are spread evenly across the months. The transfer to reserves is not an expense here: the reserve lines carry it.')
    else:
        notes.append('The budget is spread evenly across the months. The transfer to reserves is not an expense here: the reserve lines carry it.')
    special = special_assessment_summary(prop, accts, ytd, expenses)
    return {'columns': cols, 'blocks': blocks, 'finals': finals, 'notes': notes, 'has_budget': bool(annual), 'year': fy.year, 'special': special}


# ---------------------------------------------------------------- special assessments and billed vs collected

def special_assessment_accounts(accts):
    """The association's special assessment income accounts: an income account called 'Assessment Fees' or 'Special Assessment(s)'."""
    return [a for a in accts.values() if a.kind == Account.Kind.INCOME and (a.name.strip().lower() in ('assessment fees', 'special assessment', 'special assessments') or 'special assessment' in a.name.lower())]


def special_assessment_summary(prop, accts, ytd, expenses):
    """None, or {'collected', 'over', 'difference', 'rows'}: what special assessments brought in this year to date, and by how much the expense lines ran over their regular budget in the
    same period (only the lines over budget are added up), so the board can see the over-budget lines are funded. `expenses` is the Expenses block of the budget vs actual."""
    sa = special_assessment_accounts(accts)
    collected = sum((ytd.get(a.pk, ZERO) for a in sa), ZERO)
    if not sa or not collected or not expenses:
        return None
    over = ZERO
    for g in expenses['groups']:
        for l in g['lines']:
            v = l['raw'][5]                                  # year to date over / (under)
            if v is not None and v > 0:
                over += v
    over = over.quantize(Decimal('0.01'))
    collected = collected.quantize(Decimal('0.01'))
    return {'collected': collected, 'over': over, 'difference': (collected - over).quantize(Decimal('0.01')), 'names': ', '.join(a.name for a in sa),
            'rows': [('Special assessments collected, year to date', collected), ('Expense lines over their regular budget, year to date', over), ('Special assessments not yet spent' if collected >= over else 'Over budget beyond special assessments', abs(collected - over))]}


def assessments_billed_collected(prop, start, end, basis_note=True):
    """(billed, collected, unpaid) for the regular assessments in a period: billed is the income the accrual book counts, collected the same income in the cash book, unpaid the difference.
    None when the association has no regular assessment income (no account 4701/4500-style assessments line with activity)."""
    accts = [a for a in Account.objects.filter(association=prop, kind=Account.Kind.INCOME) if a.code in ('4701', '4500') or a.name.strip().lower() in ('maintenance assessments', 'assessments', 'association fees', 'fees - residential', 'regular assessments')]
    if not accts:
        return None
    ids = [a.pk for a in accts]

    def total(basis):
        sums = ledger._sums(ledger.lines(prop, basis).filter(entry__date__gte=start, entry__date__lte=end, account_id__in=ids))
        return sum((c - d for d, c in sums.values()), ZERO)
    billed, collected = total(ACCRUAL), total('cash')
    if not billed and not collected:
        return None
    return billed, collected, billed - collected


def basis_notes(prop, basis, start, end):
    """The lines every profit and loss carries: which book it is and how it differs from a report out of the old system, and (accrual) the month's assessments billed against collected."""
    if basis == ACCRUAL:
        out = ['Accrual book: income is counted when billed, expenses when incurred. Reports exported from the old system may be cash basis (counted when paid).']
        got = assessments_billed_collected(prop, start, end)
        if got:
            b, c, u = got
            out.append(f'Assessments billed ${b:,.2f}; collected ${c:,.2f}; unpaid ${u:,.2f} (see Owner aging).')
        return out
    return ['Cash book: income is counted when received, expenses when paid. Reports out of the accrual book count income when billed and expenses when incurred.']
