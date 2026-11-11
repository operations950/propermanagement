"""The annual budget builder. Late in the year we take the actuals of the books that are CLOSED, project the rest of the year, and propose next year's budget.

A budget for year Y is built from the fiscal year before it (Y-1): its actuals through the last closed month, plus a projection for the months not closed yet. An open
(half-finished) month is never counted. The budget passes through four stages, each kept as it was: Draft (what we propose), Board draft (after the board's comments),
Meeting final (edited at the meeting) and Approved (locked). Approving writes the amounts into the association's Budget, which the income report already reads.
"""
from datetime import date
from decimal import Decimal, InvalidOperation

from django.db import transaction

from . import ledger
from . import standard_chart as sc
from .models import ZERO, Account, Budget, BudgetLine, BudgetVersion, BudgetVersionLine, JournalEntry, MonthLock, PriorYearToDate

STAGES = [BudgetVersion.Stage.DRAFT, BudgetVersion.Stage.BOARD, BudgetVersion.Stage.MEETING, BudgetVersion.Stage.APPROVED]
STAGE_KEYS = [s.value for s in STAGES]
STAGE_LABEL = {s.value: s.label for s in STAGES}
STAGE_HINT = {
    'draft': 'What we propose. Working copy, not sent to the board yet.',
    'board': 'Draft 2.0: the draft after the board\'s comments.',
    'meeting': 'Edited live at the meeting. Change numbers while we talk.',
    'approved': 'Locked after the meeting. This is the budget in the system.',
}
NEXT_LABEL = {'draft': 'Send to the board: start the Board draft', 'board': 'Prepare the meeting version', 'meeting': 'Approve and lock (after the meeting)'}

SECTIONS = ['Administrative', 'Utilities', 'Operating', 'Reserves']
UTILITIES = {'6410', '6420', '6425', '6429', '6430', '6431', '6435', '6440', '6450'}
ADMIN = {'6320', '6350', '6460', '6510', '6610', '6611', '6800', '6820', '6830', '6880', '6881', '6882', '6883', '6884', '6885', '6886', '6891', '6894', '6895', '6899', '6902', '6904', '6916', '6917', '6920'}
RESERVE_INTEREST = sc.RESERVE_INTEREST
LEFT_OUT = {'6900'}   # the transfer to reserves: the reserve lines carry it


def is_reserve(acct):
    return acct.kind == Account.Kind.EQUITY and sc.RESERVE_FIRST <= acct.code <= sc.RESERVE_LAST and acct.code not in RESERVE_INTEREST


def is_budgetable(acct):
    return is_reserve(acct) or (acct.kind == Account.Kind.EXPENSE and acct.code not in LEFT_OUT)


def section_of(acct):
    if is_reserve(acct):
        return 'Reserves'
    if acct.code in UTILITIES:
        return 'Utilities'
    if acct.code in ADMIN:
        return 'Administrative'
    return 'Operating'


def _add_months(day, n):
    y, m = divmod(day.year * 12 + day.month - 1 + n, 12)
    return date(y, m + 1, 1)


def _range_label(first_month, last_month):
    if first_month == last_month:
        return first_month.strftime('%b')
    return f'{first_month:%b}–{last_month:%b}'


def _window(fy_start, first, through):
    """What follows from how far the actuals run: the months left to project, the months counted as actual (from the start of the fiscal year, so the months before our books
    began count too: they came across in the opening entry), and how many of the last three months are months we closed ourselves."""
    used = ((through.year - fy_start.year) * 12 + through.month - fy_start.month + 1) if through else 0
    rest = [_add_months(fy_start, i) for i in range(used, 12)]
    names = [m.strftime('%b') for m in rest]
    if not rest:
        rest_label = ''
    elif len(rest) == 1:
        rest_label = names[0]
    elif len(rest) == 2:
        rest_label = f'{names[0]} + {names[1]}'
    else:
        rest_label = f'{names[0]}–{names[-1]}'
    recent_n = 0
    if through and through >= first:
        lo = max(first, _add_months(through, -2))
        recent_n = (through.year - lo.year) * 12 + through.month - lo.month + 1
    carried = (first.year - fy_start.year) * 12 + first.month - fy_start.month
    return {'n': used, 'remaining': 12 - used, 'rest_label': rest_label, 'actual_label': _range_label(fy_start, through) if through else '', 'recent_n': recent_n, 'carried': max(carried, 0)}


def base_info(prop, year):
    """What the budget for `year` stands on: the fiscal year before it, and how far its books are closed (months closed one after another from the start; a gap stops the count).
    The months of that year before our books began (we took over mid-year) count as actual too: Yardi's year to date came across in the opening entry."""
    profile = ledger.profile_for(prop)
    start_month = profile.fiscal_year_start_month or 1
    fy_start = date(year - 1, start_month, 1)
    first = fy_start
    if profile.books_start:
        first = max(fy_start, ledger.next_month(profile.books_start))
    locked = set(MonthLock.objects.filter(association=prop, month__gte=first, month__lt=_add_months(fy_start, 12)).values_list('month', flat=True))
    n, cur = 0, first
    while cur in locked and cur < _add_months(fy_start, 12):
        n += 1
        cur = _add_months(cur, 1)
    through = _add_months(first, n - 1) if n else None
    info = {'fy_start': fy_start, 'first': first, 'n_closed': n, 'through': through, 'window_start': fy_start, 'window_end': ledger.month_end(through) if through else None, 'year': year}
    info.update(_window(fy_start, first, through))
    return info


def closed_note(info):
    if not info['n_closed']:
        return f'No month of {info["fy_start"].year} is closed yet'
    return f'Books closed through {info["through"]:%B %Y}'


def can_start(info):
    return info['n_closed'] >= 1


def prior_ytd(prop, fy_year):
    return {r.account_id: r.amount for r in PriorYearToDate.objects.filter(association=prop, year=fy_year)}


def _actuals(prop, start, end, accounts, include_opening=True, prior=None):
    """Per account, what happened in the window: expenses debits less credits, reserves what was put in. A reserve account's line in the opening entry is the fund's balance, not this
    year's contribution, so it is left out and the year's contributions before our books are added from `prior`. `include_opening=False` leaves the opening entry out altogether
    (it is a whole year to date in one line, so it says nothing about any one month)."""
    qs = ledger.lines(prop).filter(entry__date__gte=start, entry__date__lte=end, account__in=accounts)
    sums = {k: list(v) for k, v in ledger._sums(qs).items()}
    reserve_ids = [a.pk for a in accounts if is_reserve(a)]
    opening = qs.filter(entry__source=JournalEntry.Source.OPENING)
    if include_opening:
        opening = opening.filter(account_id__in=reserve_ids)
    for pk, (d, c) in ledger._sums(opening).items():
        sums[pk][0] -= d
        sums[pk][1] -= c
    out = {}
    for a in accounts:
        if a.pk in sums:
            d, c = sums[a.pk]
            out[a.pk] = c if is_reserve(a) else d - c
        if prior and is_reserve(a) and a.pk in prior:
            out[a.pk] = out.get(a.pk, ZERO) + prior[a.pk]
    return out


def actuals_for(prop, info, accounts):
    """(actual, recent) per account for the months the budget counts as actual."""
    if not info['n']:
        return {}, {}
    prior = prior_ytd(prop, info['fy_start'].year) if info['carried'] else None
    act = _actuals(prop, info['window_start'], info['window_end'], accounts, True, prior)
    rec = _actuals(prop, max(info['first'], _add_months(info['through'], -2)), info['window_end'], accounts, include_opening=False) if info['recent_n'] else {}
    return act, rec


def prior_budget(prop, year):
    bud = Budget.objects.filter(association=prop, year=year - 1).first()
    return {l.account_id: l.annual_amount for l in bud.lines.all()} if bud else {}


def default_method(acct):
    return 'b12' if is_reserve(acct) else 'avg'


def snapshot(prop, info):
    """Rows for a new draft: every expense and reserve account that has a budget last year or activity in the months counted as actual."""
    accounts = [a for a in Account.objects.filter(association=prop, is_active=True).order_by('code') if is_budgetable(a)]
    prior = prior_budget(prop, info['year'])
    act, rec = actuals_for(prop, info, accounts)
    rows = []
    for a in accounts:
        p, x, r = prior.get(a.pk, ZERO), act.get(a.pk, ZERO), rec.get(a.pk, ZERO)
        if p or x:
            rows.append(dict(account=a, prior_budget=p, actual=x, recent=r, method=default_method(a)))
    return rows


# ---------------------------------------------------------------- the arithmetic

METHODS = ['avg', 'recent', 'b12', 'rest', 'none', 'custom']


def method_label(code, info):
    n, k, r = info['n'], info['recent_n'], info['remaining']
    return {'avg': f'{n}-month average × {r}', 'recent': f'Last {k} month{"s" if k != 1 else ""} average × {r}', 'b12': f'Budget ÷ 12 × {r}',
            'rest': f'Rest of the budget lands in {info["rest_label"]}', 'none': 'Nothing more this year', 'custom': 'Custom amount'}[code]


def estimate(line, method, info):
    n, r = info['n'], info['remaining']
    if not r:
        return ZERO
    if method == 'avg':
        v = line.actual / n * r if n else ZERO
    elif method == 'recent':
        k = info['recent_n']
        v = line.recent / k * r if k else ZERO
    elif method == 'b12':
        v = line.prior_budget / 12 * r
    elif method == 'rest':
        v = max(line.prior_budget - line.actual, ZERO)
    elif method == 'none':
        v = ZERO
    else:
        v = line.custom
    return Decimal(v).quantize(Decimal('0.01'))


def parse_adjust(text):
    """('', 0) / a dollar amount or a percent. Raises ValueError with a message for the person."""
    s = (text or '').strip().replace('$', '').replace(',', '').replace(' ', '')
    if not s:
        return None
    try:
        if s.endswith('%'):
            return ('pct', Decimal(s[:-1]))
        return ('amt', Decimal(s))
    except InvalidOperation:
        raise ValueError(f'"{text}" is not a dollar amount or a percent. Type 500 or -3%.')


def adjustment(line, projected):
    try:
        p = parse_adjust(line.adjust)
    except ValueError:
        p = None
    if p is None:
        return ZERO
    kind, v = p
    return (projected * v / 100).quantize(Decimal('0.01')) if kind == 'pct' else v.quantize(Decimal('0.01'))


def compute(line, info):
    nd = estimate(line, line.method, info)
    projected = line.actual + nd
    adj = adjustment(line, projected)
    return {'nd': nd, 'projected': projected, 'adj': adj, 'proposed': projected + adj}


def recompute(line, info):
    line.proposed = compute(line, info)['proposed']


def info_for(version):
    """The base info a version was built with (through month and months counted are stored with it, so later closings do not change a version already made)."""
    prop, year = version.budget.association, version.budget.year
    info = base_info(prop, year)
    if version.through_month and (info['through'] != version.through_month or version.months_actual != info['n']):
        w = _window(info['fy_start'], info['first'], version.through_month)
        n = version.months_actual or w['n']
        label = _range_label(_add_months(version.through_month, -(n - 1)), version.through_month)
        info = dict(info, through=version.through_month, window_end=ledger.month_end(version.through_month), **dict(w, n=n, actual_label=label))
    return info


def is_stale(version):
    """The books have moved on since this version was made (another month closed, or the months counted changed)."""
    info = base_info(version.budget.association, version.budget.year)
    return bool(version.through_month) and (info['through'] != version.through_month or info['n'] != version.months_actual)


@transaction.atomic
def refresh_actuals(version):
    """Bring a working version up to the books as they stand: the actuals and the recent months of every line, lines for accounts that now have activity, the months counted.
    Methods, adjustments, reasons and amounts typed in stay as they are."""
    prop, year = version.budget.association, version.budget.year
    info = base_info(prop, year)
    if not can_start(info):
        raise ledger.LedgerError('No month is closed yet, so there are no actuals to bring in.')
    accounts = [a for a in Account.objects.filter(association=prop, is_active=True).order_by('code') if is_budgetable(a)]
    act, rec = actuals_for(prop, info, accounts)
    prior = prior_budget(prop, year)
    have = {l.account_id: l for l in version.lines.all()}
    version.through_month, version.months_actual = info['through'], info['n']
    version.save(update_fields=['through_month', 'months_actual'])
    fresh = info_for(version)
    for a in accounts:
        x, r = act.get(a.pk, ZERO), rec.get(a.pk, ZERO)
        line = have.get(a.pk)
        if line is None:
            if not x:
                continue
            line = BudgetVersionLine(version=version, account=a, prior_budget=prior.get(a.pk, ZERO), method=default_method(a))
        line.actual, line.recent = x, r
        recompute(line, fresh)
        line.save()
    return version


def actual_detail(version, line):
    """Everything behind one line's actual, so the screen can show it: the part that came across from the old system as a single figure (the months before our books began), then
    every entry in our books after that. The total is the actual on the line; `matches` says whether it is (it is not when the books have moved since the version was made)."""
    prop = version.budget.association
    info = info_for(version)
    acct = line.account
    reserve = is_reserve(acct)
    carried, rows = ZERO, []
    if info['n']:
        qs = (ledger.lines(prop).filter(account=acct, entry__date__gte=info['window_start'], entry__date__lte=info['window_end'])
              .select_related('entry', 'vendor').order_by('entry__date', 'entry__number', 'id'))
        for l in qs:
            amount = l.credit if reserve else l.debit - l.credit
            if l.entry.source == JournalEntry.Source.OPENING:
                if not reserve:
                    carried += amount           # a reserve's opening line is the fund's balance, not what was put in this year
                continue
            if reserve and not l.credit:
                continue
            rows.append({'date': l.entry.date.isoformat(), 'what': l.memo or l.entry.memo, 'who': l.vendor.name if l.vendor_id else '', 'ref': f'J-{l.entry.number}',
                         'source': l.entry.get_source_display(), 'amount': float(amount)})
        if reserve and info['carried']:
            carried += prior_ytd(prop, info['fy_start'].year).get(acct.pk, ZERO)
    total = carried + sum((Decimal(str(r['amount'])) for r in rows), ZERO)
    carried_label = f'{_range_label(info["fy_start"], _add_months(info["first"], -1))}, carried from the old system' if info['carried'] else 'Carried from the old system'
    return {'code': acct.code, 'name': acct.name, 'label': info['actual_label'], 'carried': float(carried), 'carried_label': carried_label, 'rows': rows, 'total': float(total),
            'actual': float(line.actual), 'matches': abs(total - line.actual) < Decimal('0.005'), 'reserve': reserve}


def heal(version, have):
    """A working version made before the months of the year ahead of our books counted as actual still shows only the months we closed ourselves. When it was made through the same
    month, bring its actuals up to date without anyone asking."""
    if version.through_month and is_editable(version, have):
        info = base_info(version.budget.association, version.budget.year)
        if info['carried'] and info['through'] == version.through_month and version.months_actual < info['n']:
            refresh_actuals(version)
            return True
    return False


# ---------------------------------------------------------------- stages

def get_budget(prop, year):
    return Budget.objects.filter(association=prop, year=year).first()


def versions(budget):
    return {v.stage: v for v in budget.versions.all()} if budget else {}


def stage_summary(prop, year):
    """('none'|stage key, version dict) for the list."""
    bud = get_budget(prop, year)
    have = versions(bud)
    latest = next((k for k in reversed(STAGE_KEYS) if k in have), None)
    return latest


def is_editable(version, have):
    if version.stage == 'approved':
        return False
    nxt = STAGE_KEYS[STAGE_KEYS.index(version.stage) + 1]
    return nxt not in have


@transaction.atomic
def start_draft(prop, year, user):
    info = base_info(prop, year)
    if not can_start(info):
        raise ledger.LedgerError(f'Close at least one month of {year - 1} first: a budget is built from closed months.')
    bud, _ = Budget.objects.get_or_create(association=prop, year=year)
    if bud.versions.exists():
        raise ledger.LedgerError('This budget already has a draft.')
    ver = BudgetVersion.objects.create(budget=bud, stage='draft', through_month=info['through'], months_actual=info['n'], created_by=user)
    for r in snapshot(prop, info):
        line = BudgetVersionLine(version=ver, **r)
        recompute(line, info)
        line.save()
    return ver


@transaction.atomic
def advance(budget, user):
    """Copy the latest stage into the next one (the earlier one is then frozen); the last step approves and locks."""
    have = versions(budget)
    cur = next((k for k in reversed(STAGE_KEYS) if k in have), None)
    if cur is None or cur == 'approved':
        raise ledger.LedgerError('There is nothing to move forward.')
    nxt = STAGE_KEYS[STAGE_KEYS.index(cur) + 1]
    src = have[cur]
    ver = BudgetVersion.objects.create(budget=budget, stage=nxt, through_month=src.through_month, months_actual=src.months_actual, created_by=user)
    BudgetVersionLine.objects.bulk_create([BudgetVersionLine(version=ver, account_id=l.account_id, prior_budget=l.prior_budget, actual=l.actual, recent=l.recent, method=l.method, custom=l.custom,
                                                              adjust=l.adjust, note=l.note, proposed=l.proposed) for l in src.lines.all()])
    if nxt == 'approved':
        write_budget_lines(budget, ver)
    return ver


def write_budget_lines(budget, version):
    """The approved amounts become the year's budget lines, which the income report compares the books against. Income lines are left alone."""
    budgetable = [a.pk for a in Account.objects.filter(association=budget.association) if is_budgetable(a)]
    assessments = Account.objects.filter(association=budget.association, code=sc.INCOME_ASSESSMENT).first()
    BudgetLine.objects.filter(budget=budget, account_id__in=budgetable + ([assessments.pk] if assessments else [])).delete()
    lines = [l for l in version.lines.select_related('account') if is_budgetable(l.account) and l.proposed]
    BudgetLine.objects.bulk_create([BudgetLine(budget=budget, account_id=l.account_id, annual_amount=l.proposed) for l in lines])
    total = sum((l.proposed for l in lines), ZERO)
    if assessments and total:      # what owners pay in a year: the expenses and the reserves together (one assessment, collected in operating)
        BudgetLine.objects.create(budget=budget, account=assessments, annual_amount=total)


@transaction.atomic
def reopen(budget):
    have = versions(budget)
    if 'approved' not in have:
        raise ledger.LedgerError('This budget is not approved.')
    budgetable = [a.pk for a in Account.objects.filter(association=budget.association) if is_budgetable(a) or a.code == sc.INCOME_ASSESSMENT]
    BudgetLine.objects.filter(budget=budget, account_id__in=budgetable).delete()
    have['approved'].delete()


# ---------------------------------------------------------------- what the screens show

def unit_count(prop):
    from . import owners
    return owners.active_units(prop).count()


def line_rows(version, info, prev_version=None):
    prev = {l.account_id: l.proposed for l in prev_version.lines.all()} if prev_version else None
    rows = []
    for l in version.lines.select_related('account'):
        if not is_budgetable(l.account):
            continue           # an income line (the assessments) is worked out from the expenses, never added up as one
        c = compute(l, info)
        options = {m: float(estimate(l, m, info)) for m in METHODS if m != 'custom'}
        rows.append({
            'id': l.pk, 'code': l.account.code, 'name': l.account.name, 'section': section_of(l.account),
            'prior': float(l.prior_budget), 'actual': float(l.actual), 'method': l.method, 'method_label': method_label(l.method, info) if l.method != 'custom' else f'Custom ${l.custom:,.0f}',
            'custom': float(l.custom), 'nd': float(c['nd']), 'projected': float(c['projected']), 'adjust': l.adjust, 'adj': float(c['adj']), 'note': l.note, 'proposed': float(c['proposed']),
            'since': (float(c['proposed'] - prev.get(l.account_id, ZERO)) if prev is not None else 0.0), 'options': options,
            'removable': not l.prior_budget and not l.actual, 'unbudgeted': not l.prior_budget and bool(l.actual),
        })
    rows.sort(key=lambda r: (SECTIONS.index(r['section']), r['code']))
    return rows


def totals(rows, units):
    out = {}
    for s in SECTIONS:
        rs = [r for r in rows if r['section'] == s]
        out[s] = {k: sum(r[k] for r in rs) for k in ('prior', 'actual', 'nd', 'projected', 'adj', 'proposed')}
    op = {k: sum(out[s][k] for s in SECTIONS[:3]) for k in ('prior', 'projected', 'proposed')}
    rs = out['Reserves']
    tot, tot_b, tot_p = op['proposed'] + rs['proposed'], op['prior'] + rs['prior'], op['projected'] + rs['projected']
    u = units or 1
    income = {'assessments': {'prior': op['prior'], 'proposed': op['proposed']}, 'reserve': {'prior': rs['prior'], 'proposed': rs['proposed']}, 'total': {'prior': tot_b, 'proposed': tot}}
    return {'sections': out, 'op': op, 'rs': rs, 'income': income, 'total': tot, 'total_prior': tot_b, 'total_projected': tot_p,
            'unit_month': tot / u / 12, 'unit_month_prior': tot_b / u / 12, 'unit_year': tot / u, 'units': units}


def editor_state(version, have):
    info = info_for(version)
    idx = STAGE_KEYS.index(version.stage)
    prev = have.get(STAGE_KEYS[idx - 1]) if idx else None
    rows = line_rows(version, info, prev)
    units = unit_count(version.budget.association)
    t = totals(rows, units)
    used = {l.account_id for l in version.lines.all()}
    addable = {}
    for a in Account.objects.filter(association=version.budget.association, is_active=True).order_by('code'):
        if is_budgetable(a) and a.pk not in used:
            addable.setdefault(section_of(a), []).append({'id': a.pk, 'label': f'{a.code} {a.name}'})
    return {'sections': SECTIONS, 'rows': rows, 'totals': t, 'addable': addable, 'editable': is_editable(version, have), 'remaining': info['remaining'], 'actual_label': info['actual_label'], 'rest_label': info['rest_label'], 'recent_n': info['recent_n'], 'n': info['n'],
            'stale': is_stale(version) and is_editable(version, have), 'prev_label': STAGE_LABEL[STAGE_KEYS[idx - 1]] if idx else '', 'methods': [(m, method_label(m, info) if m != 'custom' else 'Custom amount') for m in METHODS]}


def compare_rows(have, units):
    """Every line across the stages up to the meeting: what we proposed, what the board changed, what changed at the meeting."""
    keys = [k for k in ('draft', 'board', 'meeting') if k in have]
    by = {}
    for k in keys:
        info = info_for(have[k])
        for l in have[k].lines.select_related('account'):
            if not is_budgetable(l.account):
                continue
            by.setdefault(l.account_id, {'account': l.account, 'prior': l.prior_budget, 'v': {}, 'note': {}})
            by[l.account_id]['v'][k] = compute(l, info)['proposed']
            by[l.account_id]['note'][k] = l.note
    rows = []
    for a in by.values():
        v = {k: a['v'].get(k, ZERO) for k in keys}
        changed = any(abs(v[keys[i]] - v[keys[i - 1]]) >= Decimal('0.5') for i in range(1, len(keys)))
        why = ''
        for k in reversed(keys[1:]):
            i = keys.index(k)
            if abs(v[k] - v[keys[i - 1]]) >= Decimal('0.5') and a['note'].get(k):
                why = a['note'][k]
                break
        rows.append({'account': a['account'], 'section': section_of(a['account']), 'prior': a['prior'], 'v': v, 'changed': changed, 'why': why})
    rows.sort(key=lambda r: (SECTIONS.index(r['section']), r['account'].code))
    return keys, rows
