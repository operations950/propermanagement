"""Scheduled journal entries: the monthly reserve contribution, and recurring entries (a loan payment split into principal and interest).

An association collects everything in operating and moves part of it to the reserve account every month. The transfer itself is cash: the bank statements show it and the
bank reconciliation posts and matches it. What is left to book is the contribution: which reserve fund each part belongs to. That does not touch cash, so it is a scheduled
entry: Reserve Transfer (an expense) against the reserve funds, split by the approved budget's reserve lines divided by 12, dated the day the transfer happens.
"""
from decimal import ROUND_HALF_UP, Decimal

from . import budgeting as bg
from . import ledger
from . import standard_chart as sc
from .models import ZERO, BankAccount, Budget, JournalEntry, JournalLine, JournalRule, JournalRuleLine, JournalRuleSkip

CENT = Decimal('0.01')


def reserve_split(prop, on_day):
    """{'year', 'has_budget', 'total', 'lines': [(Account, amount)]}: one month's contribution from the budget of the fiscal year `on_day` is in. Each fund gets its yearly amount
    divided by 12; the total is the yearly total divided by 12, and the pennies that rounding leaves over go on the largest fund."""
    fy = ledger.profile_for(prop).fiscal_year_start(on_day)
    budget = Budget.objects.filter(association=prop, year=fy.year).first()
    yearly = [(l.account, l.annual_amount) for l in budget.lines.select_related('account').order_by('account__code') if bg.is_reserve(l.account) and l.annual_amount > 0] if budget else []
    if not yearly:
        return {'year': fy.year, 'has_budget': False, 'total': ZERO, 'lines': []}
    total = (sum((a for _, a in yearly), ZERO) / 12).quantize(CENT, rounding=ROUND_HALF_UP)
    lines = [(acct, (amt / 12).quantize(CENT, rounding=ROUND_HALF_UP)) for acct, amt in yearly]
    off = total - sum((a for _, a in lines), ZERO)
    if off:
        biggest = max(range(len(lines)), key=lambda i: lines[i][1])
        lines[biggest] = (lines[biggest][0], lines[biggest][1] + off)
    return {'year': fy.year, 'has_budget': True, 'total': total, 'lines': lines}


def rule_day(rule, month_first):
    """The day of the month the transfer happens (31 or more means the last day)."""
    end = ledger.month_end(month_first)
    return month_first.replace(day=min(rule.day_of_month, end.day))


def posted_entry(prop, rule, month_first):
    """This month's contribution, if it is in the books: one the schedule posted, or one made by hand (any expense on Reserve Transfer that month)."""
    end = ledger.month_end(month_first)
    mine = JournalEntry.objects.filter(association=prop, source_ref=f'jrule:{rule.pk}:{month_first:%Y-%m}', reversed_by__isnull=True, reverses__isnull=True).first()
    if mine:
        return mine
    if rule.kind != JournalRule.Kind.RESERVE:
        return None
    line = (JournalLine.objects.filter(entry__association=prop, entry__date__gte=month_first, entry__date__lte=end, account__code=sc.RESERVE_TRANSFER, debit__gt=0,
                                       entry__reversed_by__isnull=True, entry__reverses__isnull=True).select_related('entry').first())
    return line.entry if line else None


def status(prop, rule, month_first, today):
    """One month of a rule: posted (with the entry), no_budget, due (its day has come) or scheduled (a day still to come)."""
    if rule.kind == JournalRule.Kind.ENTRY:
        return entry_status(prop, rule, month_first, today)
    entry = posted_entry(prop, rule, month_first)
    day = rule_day(rule, month_first)
    split = reserve_split(prop, day)
    if entry is not None:
        return {'state': 'posted', 'entry': entry, 'day': day, 'split': split}
    skip = skipped(rule, month_first)
    if skip is not None:
        return {'state': 'skipped', 'entry': None, 'day': day, 'split': split, 'skip': skip}
    if not split['has_budget']:
        return {'state': 'no_budget', 'entry': None, 'day': day, 'split': split}
    return {'state': 'due' if day <= today else 'scheduled', 'entry': None, 'day': day, 'split': split}


def post_contribution(prop, rule, month_first, user=None, day=None):
    """Post one month's contribution: Reserve Transfer against the funds, dated the transfer day. Refuses when there is no approved budget or the month is already done."""
    if posted_entry(prop, rule, month_first) is not None:
        raise ledger.LedgerError(f'{month_first:%B %Y} already has its reserve contribution.')
    day = day or rule_day(rule, month_first)
    split = reserve_split(prop, day)
    if not split['has_budget']:
        raise ledger.LedgerError(f'There is no budget with reserve lines for {split["year"]}: approve the budget first.')
    lines = [{'account': sc.RESERVE_TRANSFER, 'debit': split['total'], 'memo': 'Reserve contribution'}]
    lines += [{'account': acct, 'credit': amount, 'memo': 'Reserve contribution'} for acct, amount in split['lines']]
    return ledger.post_entry(prop, day, lines, memo=f'Reserve contribution, {month_first:%B}', source=JournalEntry.Source.MANUAL, source_ref=f'jrule:{rule.pk}:{month_first:%Y-%m}', user=user)


def post_due(prop, today, user=None, automatic=False):
    """Post what has come due, for this month and last (older months are left alone: they are closed or done by hand). Returns the entries made.
    With `automatic` (the scheduled job), only the entries whose own automatic posting is switched on."""
    made = []
    rules = JournalRule.objects.filter(association=prop, is_active=True)
    if automatic:
        rules = rules.filter(auto_post=True)
    for rule in rules:
        for month_first in (ledger.previous_month(today), ledger.month_start(today)):
            st = status(prop, rule, month_first, today)
            if st['state'] != 'due':
                continue
            try:
                made.append(post_entry_rule(prop, rule, month_first, user) if rule.kind == JournalRule.Kind.ENTRY else post_contribution(prop, rule, month_first, user))
            except ledger.LedgerError:
                continue         # that month is closed
    return made


def reserve_tie(prop, end):
    """Do the reserve funds equal the reserve cash? Reserve cash is every reserve bank account; the funds are the reserve fund accounts. `diff` is cash less funds."""
    bs = ledger.balance_sheet(prop, end)
    funds = sum((r['amount'] for r in bs['sections'].get('Reserve funds', [])), ZERO)
    banks = [b for b in BankAccount.objects.filter(association=prop, kind=BankAccount.Kind.RESERVE, is_active=True)]
    cash = sum((ledger.cash_balance(b, end) for b in banks), ZERO)
    return {'funds': funds, 'cash': cash, 'diff': cash - funds, 'has_reserve_bank': bool(banks)}


# ---------------------------------------------------------------- recurring entries (a loan payment: principal and interest)

def skipped(rule, month_first):
    """The skip recorded for a month of a rule, or None."""
    return JournalRuleSkip.objects.filter(rule=rule, month=month_first).select_related('skipped_by').first()


def entry_amounts(rule, overrides=None):
    """The lines of a recurring entry as they would post: [{'line', 'account', 'side', 'amount', 'debit', 'credit', 'memo', 'is_rest'}]. `overrides` is {line id: amount} for the lines whose amount is
    changed this month. The line marked 'the rest' is worked out so the entry balances; with none marked, the amounts stand as given."""
    overrides = overrides or {}
    out = []
    for l in rule.lines.select_related('account').all():
        amount = ledger.money(overrides[l.pk]) if (l.pk in overrides and not l.is_rest) else l.amount
        out.append({'line': l, 'account': l.account, 'side': l.side, 'amount': amount, 'memo': l.memo, 'is_rest': l.is_rest})
    rest = [r for r in out if r['is_rest']]
    if len(rest) == 1:
        d = sum((r['amount'] for r in out if not r['is_rest'] and r['side'] == 'debit'), ZERO)
        c = sum((r['amount'] for r in out if not r['is_rest'] and r['side'] == 'credit'), ZERO)
        rest[0]['amount'] = (c - d) if rest[0]['side'] == 'debit' else (d - c)
    for r in out:
        r['debit'], r['credit'] = (r['amount'], ZERO) if r['side'] == 'debit' else (ZERO, r['amount'])
    return out


def entry_total(rule, overrides=None):
    """What the entry comes to: its debits."""
    return sum((r['debit'] for r in entry_amounts(rule, overrides)), ZERO)


def entry_status(prop, rule, month_first, today):
    """One month of a recurring entry: posted (with the entry), skipped, due (its day has come) or scheduled. 'split' carries what the entry would post, in the shape the reserve rule uses,
    so a screen that only reads state, entry, day and split's total works for both kinds."""
    day = rule_day(rule, month_first)
    amounts = entry_amounts(rule)
    split = {'year': month_first.year, 'has_budget': True, 'total': sum((r['debit'] for r in amounts), ZERO),
             'lines': [(r['account'], r['debit'] or r['credit']) for r in amounts]}
    entry = posted_entry(prop, rule, month_first)
    if entry is not None:
        return {'state': 'posted', 'entry': entry, 'day': day, 'split': split}
    skip = skipped(rule, month_first)
    if skip is not None:
        return {'state': 'skipped', 'entry': None, 'day': day, 'split': split, 'skip': skip}
    return {'state': 'due' if day <= today else 'scheduled', 'entry': None, 'day': day, 'split': split}


def rule_status(prop, rule, month_first, today=None):
    """For the month-end close: what became of one rule for a month. {'posted', 'skipped', 'pending' (neither), 'state', 'entry', 'skip', 'day', 'total', 'rule'}."""
    from django.utils import timezone
    today = today or timezone.localdate()
    st = status(prop, rule, month_first, today)
    return {'rule': rule, 'posted': st['state'] == 'posted', 'skipped': st['state'] == 'skipped', 'pending': st['state'] not in ('posted', 'skipped'), 'state': st['state'],
            'entry': st.get('entry'), 'skip': st.get('skip'), 'day': st['day'], 'total': st['split']['total']}


def pending_entries(prop, month_first, today=None):
    """The active rules of an association that were neither posted nor skipped for a month, soonest day first: [rule_status(...)]. The month-end step is done when this is empty."""
    rows = [rule_status(prop, rule, month_first, today) for rule in JournalRule.objects.filter(association=prop, is_active=True).order_by('day_of_month', 'name')]
    return [r for r in rows if r['pending']]


def post_entry_rule(prop, rule, month_first, user=None, amounts=None, day=None):
    """Post one month of a recurring entry, with the amounts as they are on the rule or changed for this month (`amounts` is {line id: amount}; the line marked 'the rest' follows). The entry is
    dated the rule's day. Posting a month that was skipped takes the skip away. Refuses a month that already has it."""
    if rule.kind != JournalRule.Kind.ENTRY:
        raise ledger.LedgerError('That is not a recurring entry.')
    if posted_entry(prop, rule, month_first) is not None:
        raise ledger.LedgerError(f'{month_first:%B %Y} already has this entry.')
    rows = entry_amounts(rule, amounts)
    for r in rows:
        if r['debit'] < 0 or r['credit'] < 0:
            raise ledger.LedgerError(f'The amount for {r["account"]} comes to less than zero: change the other amounts.')
    lines = [{'account': r['account'], 'debit': r['debit'], 'credit': r['credit'], 'memo': r['memo'] or rule.name} for r in rows if r['debit'] or r['credit']]
    entry = ledger.post_entry(prop, day or rule_day(rule, month_first), lines, memo=f'{rule.name}, {month_first:%B %Y}', source=JournalEntry.Source.MANUAL,
                              source_ref=f'jrule:{rule.pk}:{month_first:%Y-%m}', user=user)
    JournalRuleSkip.objects.filter(rule=rule, month=month_first).delete()
    # what was posted becomes the amounts the next month starts from
    if amounts:
        for r in rows:
            JournalRuleLine.objects.filter(pk=r['line'].pk).update(amount=r['amount'])
    return entry


def skip_rule(prop, rule, month_first, user=None, note=''):
    """Say a rule is not posted this month on purpose. Refuses a month already posted."""
    if posted_entry(prop, rule, month_first) is not None:
        raise ledger.LedgerError(f'{month_first:%B %Y} is already posted: reverse it first if it was wrong.')
    skip, _ = JournalRuleSkip.objects.get_or_create(rule=rule, month=month_first, defaults={'skipped_by': user if getattr(user, 'pk', None) else None, 'note': (note or '')[:200]})
    return skip


def unskip_rule(rule, month_first):
    JournalRuleSkip.objects.filter(rule=rule, month=month_first).delete()


def save_entry_rule(prop, name, day_of_month, rows, rule=None, auto_post=False):
    """Make or change a recurring entry. `rows` is [{'account': Account, 'side': 'debit' or 'credit', 'amount': D, 'memo': str, 'is_rest': bool}] in order. Needs two or more lines that balance (the
    line marked 'the rest' is worked out, so it is left out of the check), and at most one 'rest'."""
    name = (name or '').strip()[:120]
    if not name:
        raise ledger.LedgerError('Give the entry a name.')
    if not 1 <= int(day_of_month or 0) <= 31:
        raise ledger.LedgerError('Give the day of the month (1 to 31; 31 means the last day).')
    rows = [dict(r) for r in rows if r.get('account') is not None]
    if len(rows) < 2:
        raise ledger.LedgerError('An entry needs at least two lines.')
    if sum(1 for r in rows if r.get('is_rest')) > 1:
        raise ledger.LedgerError('Only one line can be "the rest".')
    for r in rows:
        r['side'] = 'credit' if r.get('side') == 'credit' else 'debit'
        r['amount'] = ledger.money(r.get('amount') or 0)
        if r['amount'] < 0:
            raise ledger.LedgerError('An amount cannot be negative: use the other side instead.')
        if not r.get('is_rest') and not r['amount']:
            raise ledger.LedgerError(f'Type an amount for {r["account"]}, or mark it as "the rest".')
    if not any(r.get('is_rest') for r in rows):
        d = sum((r['amount'] for r in rows if r['side'] == 'debit'), ZERO)
        c = sum((r['amount'] for r in rows if r['side'] == 'credit'), ZERO)
        if d != c:
            raise ledger.LedgerError(f'The entry does not balance: debits ${d:,.2f}, credits ${c:,.2f}. Mark one line "the rest" or fix the amounts.')
    if rule is None:
        rule = JournalRule.objects.create(association=prop, kind=JournalRule.Kind.ENTRY, name=name, day_of_month=int(day_of_month), auto_post=auto_post)
    else:
        rule.name, rule.day_of_month = name, int(day_of_month)
        rule.save(update_fields=['name', 'day_of_month'])
        rule.lines.all().delete()
    for i, r in enumerate(rows):
        JournalRuleLine.objects.create(rule=rule, account=r['account'], side=r['side'], amount=r['amount'], memo=(r.get('memo') or '')[:200], is_rest=bool(r.get('is_rest')), order=i)
    # a rest line's stored amount is what balances the rest of the entry, so the template always reads as a whole entry
    for item in entry_amounts(rule):
        if item['is_rest']:
            if item['amount'] <= 0:
                raise ledger.LedgerError('"The rest" would come to nothing or less: check the other amounts.')
            JournalRuleLine.objects.filter(pk=item['line'].pk).update(amount=item['amount'])
    return rule
