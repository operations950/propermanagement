"""The month-end check that the reserve funds equal the reserve cash: every reserve bank account and every reserve fund in one place, the difference, and a person's acknowledgement of it."""
from django.utils import timezone

from . import ledger
from .models import ZERO, BankAccount, ReserveAcknowledgement


def check(association, month_first):
    """What the reserve check shows for a month: the reserve bank accounts with their balances, the reserve funds with theirs, the difference (cash less funds), and whether a person
    has acknowledged exactly this difference. `status` is 'done' when it matches or the difference is acknowledged, else 'attention'."""
    month_first = ledger.month_start(month_first)
    end = ledger.month_end(month_first)
    reserve_banks = list(BankAccount.objects.filter(association=association, kind=BankAccount.Kind.RESERVE, is_active=True).select_related('account').order_by('name', 'pk'))
    for b in reserve_banks:
        b.association = association          # already in hand: cash_balance asks for it
    banks = [{'bank': b, 'balance': ledger.cash_balance(b, end)} for b in reserve_banks]
    cash = sum((r['balance'] for r in banks), ZERO)
    funds = [{'account': r['account'], 'amount': r['amount']} for r in ledger.balance_sheet(association, end)['sections'].get('Reserve funds', [])]
    funds_total = sum((r['amount'] for r in funds), ZERO)
    diff = cash - funds_total
    ack = ReserveAcknowledgement.objects.filter(association=association, month=month_first).select_related('acknowledged_by').first()
    acknowledged = bool(ack and ack.difference == diff and diff != 0)
    return {'month': month_first, 'month_end': end, 'banks': banks, 'cash': cash, 'funds': funds, 'funds_total': funds_total, 'diff': diff, 'has_reserve_bank': bool(banks),
            'ack': ack, 'acknowledged': acknowledged, 'stale_ack': bool(ack and not acknowledged and diff != 0), 'status': 'done' if (diff == 0 or acknowledged) else 'attention'}


def acknowledge(association, month_first, reason, user=None):
    """Record that the difference as it stands now is understood, and why. A reason is required, and there is nothing to acknowledge when the two match."""
    reason = ' '.join((reason or '').split())
    if not reason:
        raise ValueError('Say why they differ: a reason is needed.')
    if len(reason) > 500:
        raise ValueError('Keep the reason under 500 characters.')
    info = check(association, month_first)
    if info['diff'] == 0:
        raise ValueError('The reserve cash and the reserve funds match: there is nothing to acknowledge.')
    ack, _ = ReserveAcknowledgement.objects.update_or_create(association=association, month=info['month'], defaults={'difference': info['diff'], 'reason': reason, 'acknowledged_by': user,
                                                                                                                      'acknowledged_at': timezone.now()})
    return ack


def remove(association, month_first):
    ReserveAcknowledgement.objects.filter(association=association, month=ledger.month_start(month_first)).delete()
