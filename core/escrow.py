"""The month-end balance checks that work from a hand-kept list: the ESCROW account (security deposits and advance rents we hold for other people, a
separate account recorded in QuickBooks in one place) and the MISC PROPERTY MANAGEMENT TRUST account (a place to book what was spent or received by
mistake from the property management bank account, which should not carry a balance). For each, the list of what makes up the balance (EscrowLine: a name,
a property if any, an amount, a note) is kept here by hand, each month starting as a copy of the month before it. The check: the list must add up to what
the account holds in QuickBooks at month end (for escrow, also to the bank statement's ending balance if one is typed). Nothing here is income and none of
it touches a property's trust account or its owner's figures."""
from decimal import Decimal

from django.utils import timezone

from . import bank_tieout, ledger, quickbooks
from .models import EscrowLine, EscrowMonth, FinancialsSettings

ZERO = Decimal('0.00')
PENNY = bank_tieout.PENNY
ESCROW, MISC = EscrowMonth.Kind.ESCROW, EscrowMonth.Kind.MISC
SETTINGS_FIELD = {ESCROW: 'escrow_account', MISC: 'misc_trust_account'}


def account_for(kind):
    return getattr(FinancialsSettings.get(), SETTINGS_FIELD[kind])


def ensure_month(month, user=None, kind=ESCROW):
    """The month's record for this list. The first time a month is opened it starts as a copy of the month before it (the latest earlier month on file),
    because most of what is held carries on, with a change or two."""
    month = ledger.month_of(month)
    em, created = EscrowMonth.objects.get_or_create(kind=kind, month=month)
    if created and not EscrowLine.objects.filter(kind=kind, month=month).exists():
        prior = EscrowMonth.objects.filter(kind=kind, month__lt=month).order_by('-month').first()
        if prior is not None:
            for line in EscrowLine.objects.filter(kind=kind, month=prior.month):
                EscrowLine.objects.create(kind=kind, month=month, name=line.name, property=line.property, amount=line.amount, note=line.note, updated_by=user)
    return em


def lines(month, kind=ESCROW):
    return list(EscrowLine.objects.filter(kind=kind, month=ledger.month_of(month)).select_related('property'))


def _activity(token, account, month):
    """The month's transactions in the account, as QuickBooks shows them: [{date, type, name, memo, amount (its effect on the balance)}]."""
    entries, error = quickbooks.fetch_ledger_family(token, account.qb_id, month, bank_tieout.month_end(month))
    if error:
        return None, error
    out = []
    for e in entries:
        effect = bank_tieout._effect(e, account) if ('debit' in e or 'credit' in e) else e.get('amount', ZERO)
        out.append({'date': e['date'], 'type': e['txn_type'], 'name': e.get('name', ''), 'memo': e.get('memo', ''), 'split': e.get('split', ''), 'amount': str(effect)})
    return sorted(out, key=lambda r: (r['date'], r['type'])), ''


def pull_balance(token, month, kind=ESCROW):
    """Read the account's balance at month end from QuickBooks and keep it with the month (for the misc trust account, also the month's transactions in it).
    Returns (EscrowMonth, '') or (None, why)."""
    month = ledger.month_of(month)
    account = account_for(kind)
    if account is None:
        return None, 'Choose the account first.'
    end = bank_tieout.month_end(month)
    sheet, error = quickbooks.fetch_balance_sheet(token, end)
    if error:
        return None, error
    own, total = sheet['own'], sheet['total']
    value = own.get(account.qb_id) if own.get(account.qb_id) is not None else total.get(account.qb_id)
    if value is None:
        return None, f"QuickBooks's balance sheet at {end} has no line for that account - check the account chosen."
    activity = None
    if kind == MISC:
        activity, error = _activity(token, account, month)
        if error:
            return None, error
    em = ensure_month(month, kind=kind)
    em.qb_balance, em.qb_pulled_at = bank_tieout.money(value), timezone.now()
    fields = ['qb_balance', 'qb_pulled_at']
    if activity is not None:
        em.activity = activity
        fields.append('activity')
    em.save(update_fields=fields)
    return em, ''


def summary(month, kind=ESCROW):
    """Everything the page and the checklist need. `status`: 'not_started' (no QuickBooks balance pulled yet), 'attention' (the list does not match) or 'done'."""
    month = ledger.month_of(month)
    em = EscrowMonth.objects.filter(kind=kind, month=month).first()
    items = lines(month, kind) if em else []
    total = sum((l.amount for l in items), ZERO)
    out = {'kind': kind, 'month': month, 'em': em, 'lines': items, 'total': total, 'account': account_for(kind),
           'qb_balance': em.qb_balance if em else None, 'bank_balance': em.bank_balance if em else None, 'status': 'not_started',
           'negative': [l for l in items if l.amount < 0] if kind == ESCROW else [], 'qb_gap': None, 'bank_gap': None,
           'activity': (em.activity if em else []), 'activity_total': sum((Decimal(a['amount']) for a in (em.activity if em else [])), ZERO)}
    if em is not None and em.qb_balance is not None:
        out['qb_gap'] = total - em.qb_balance
        ok = abs(out['qb_gap']) < PENNY
        if kind == ESCROW and em.bank_balance is not None:
            out['bank_gap'] = total - em.bank_balance
            ok = ok and abs(out['bank_gap']) < PENNY
        out['status'] = 'done' if ok else 'attention'
    return out


def status(month, kind=ESCROW):
    return summary(month, kind)['status']
