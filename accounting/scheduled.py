"""Scheduled journal entries: the monthly reserve contribution.

An association collects everything in operating and moves part of it to the reserve account every month. The transfer itself is cash: the bank statements show it and the
bank reconciliation posts and matches it. What is left to book is the contribution: which reserve fund each part belongs to. That does not touch cash, so it is a scheduled
entry: Reserve Transfer (an expense) against the reserve funds, split by the approved budget's reserve lines divided by 12, dated the day the transfer happens.
"""
from decimal import ROUND_HALF_UP, Decimal

from . import budgeting as bg
from . import ledger
from . import standard_chart as sc
from .models import ZERO, BankAccount, Budget, JournalEntry, JournalLine, JournalRule

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
    mine = JournalEntry.objects.filter(association=prop, source_ref=f'jrule:{rule.pk}:{month_first:%Y-%m}', reversed_by__isnull=True).first()
    if mine:
        return mine
    line = (JournalLine.objects.filter(entry__association=prop, entry__date__gte=month_first, entry__date__lte=end, account__code=sc.RESERVE_TRANSFER, debit__gt=0,
                                       entry__reversed_by__isnull=True, entry__reverses__isnull=True).select_related('entry').first())
    return line.entry if line else None


def status(prop, rule, month_first, today):
    """One month of a rule: posted (with the entry), no_budget, due (its day has come) or scheduled (a day still to come)."""
    entry = posted_entry(prop, rule, month_first)
    day = rule_day(rule, month_first)
    split = reserve_split(prop, day)
    if entry is not None:
        return {'state': 'posted', 'entry': entry, 'day': day, 'split': split}
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


def post_due(prop, today, user=None):
    """Post what has come due, for this month and last (older months are left alone: they are closed or done by hand). Returns the entries made."""
    made = []
    for rule in JournalRule.objects.filter(association=prop, is_active=True, kind=JournalRule.Kind.RESERVE):
        for month_first in (ledger.previous_month(today), ledger.month_start(today)):
            st = status(prop, rule, month_first, today)
            if st['state'] != 'due':
                continue
            try:
                made.append(post_contribution(prop, rule, month_first, user))
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
