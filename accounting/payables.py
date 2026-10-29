"""Bills, what is approved to pay, and paying.

A bill is posted to the books when it is approved, not when it arrives: a draft read off an invoice is only a suggestion. Who must approve:
  * routine bills (a recurring bill we set up once) are approved when they post;
  * a one-off bill at or under the spending limit in the management contract can be approved by staff;
  * everything else, and ALL reserve spending, needs the board.
Paying posts to both books: the accrual book clears payables; the cash book is the expense itself.
"""
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from core.models import Contact

from . import ledger
from . import standard_chart as sc
from .charges import add_months, default_bank, FREQ_MONTHS
from .models import ZERO, Bill, BillLine, JournalEntry, Payment, PaymentBill, RecurringBill, VendorTerms


class PayableError(Exception):
    pass


# ---------------------------------------------------------------- vendors

def vendor_for_name(name, create=True):
    name = ' '.join((name or '').split())
    if not name:
        return None
    found = Contact.objects.filter(name__iexact=name, contact_type=Contact.ContactType.VENDOR).first() or Contact.objects.filter(name__iexact=name).first()
    if found is None and create:
        found = Contact.objects.create(name=name, contact_type=Contact.ContactType.VENDOR, source=Contact.Source.MANUAL)
    return found


# ---------------------------------------------------------------- approval rules

def requirement(association, bill):
    """'none' (routine), 'staff' (a one-off under the limit) or 'board'."""
    profile = ledger.profile_for(association)
    if bill.reserve or bill.kind == Bill.Kind.RESERVE:
        return 'board'
    if bill.kind == Bill.Kind.ROUTINE:
        return 'none'
    if profile.spending_limit is not None and bill.amount <= profile.spending_limit:
        return 'staff'
    return 'board'


@transaction.atomic
def create_bill(association, day, lines, vendor=None, vendor_text='', number='', due=None, kind=Bill.Kind.EXCEPTION, reserve=False, source=Bill.Source.MANUAL,
                file=None, user=None, recurring=None, memo='', email_message_id='', opening=False):
    """Make a bill from its lines [(account, amount, memo)]. A routine bill is approved and posted straight away; any other waits as a draft.
    An opening bill is one already on the books when they moved here: it is ready to pay and posts nothing, because the opening balances hold it."""
    if opening:
        source = Bill.Source.OPENING
    clean = [(l[0], ledger.money(l[1]), l[2] if len(l) > 2 else '') for l in lines if ledger.money(l[1]) != 0]
    if not clean:
        raise PayableError('A bill needs at least one line with an amount.')
    total = sum((a for _, a, _ in clean), ZERO)
    if total <= 0:
        raise PayableError('A bill needs a total above zero.')
    if vendor is None and not vendor_text:
        raise PayableError('Say who the bill is from.')
    bill = Bill.objects.create(association=association, vendor=vendor, vendor_text=vendor_text[:200], number=number[:60], date=day, due_date=due, amount=total, kind=kind,
                               reserve=reserve, source=source, recurring=recurring, memo=memo[:300], email_message_id=email_message_id)
    if file:
        bill.file.save(getattr(file, 'name', 'invoice.pdf'), file, save=True)
    for account_or_code, amt, m in clean:
        BillLine.objects.create(bill=bill, account=ledger._as_account(association, account_or_code), amount=amt, memo=m[:200])
    if opening:
        bill.status = Bill.Status.APPROVED
        bill.save(update_fields=['status'])
        return bill
    if kind == Bill.Kind.ROUTINE and not reserve:
        approve_bill(bill, user)
    return bill


def _post_bill(bill, user):
    lines = [{'account': l.account, 'debit': l.amount, 'vendor': bill.vendor, 'memo': l.memo or bill.payee} for l in bill.lines.all()]
    lines.append({'account': sc.AP, 'credit': bill.amount, 'vendor': bill.vendor, 'memo': f'{bill.payee} {bill.number}'.strip()})
    bill.entry = ledger.post_entry(bill.association, bill.date, lines, memo=f'{bill.payee} {bill.number}'.strip(), basis=JournalEntry.Basis.ACCRUAL, source=JournalEntry.Source.BILL,
                                   source_ref=f'bill:{bill.pk}', user=user)


@transaction.atomic
def approve_bill(bill, user=None, board_by='', board_on=None):
    """Approve a bill and post it. If the board must approve and nobody has said they did, it moves to "waiting for the board" instead."""
    if bill.status in (Bill.Status.APPROVED, Bill.Status.PAID):
        return bill
    if bill.status == Bill.Status.VOID:
        raise PayableError('That bill is void.')
    need = requirement(bill.association, bill)
    if need == 'board' and not board_by.strip():
        bill.status = Bill.Status.AWAITING_BOARD
        bill.save(update_fields=['status'])
        return bill
    if board_by.strip():
        bill.board_approved_by, bill.board_approved_on = board_by.strip()[:120], board_on or timezone.localdate()
    bill.status = Bill.Status.APPROVED
    bill.approved_by, bill.approved_at = user, timezone.now()
    _post_bill(bill, user)
    bill.save()
    return bill


@transaction.atomic
def void_bill(bill, user=None, day=None):
    if bill.payments.exists():
        raise PayableError('That bill has a payment on it. Void the payment first.')
    if bill.source == Bill.Source.OPENING:
        raise PayableError('That balance came over from the old system and is in the opening balances. Fix it with a journal entry.')
    if bill.entry_id and not bill.entry.reversed_by.exists():
        ledger.reverse_entry(bill.entry, day or timezone.localdate(), user=user, memo=f'Void bill {bill.payee} {bill.number}'.strip())
    bill.status = Bill.Status.VOID
    bill.save(update_fields=['status'])


def bills_to_pay(association):
    return [b for b in Bill.objects.filter(association=association, status=Bill.Status.APPROVED).select_related('vendor').prefetch_related('payments') if b.balance > 0]


AGE_BUCKETS = (('0 to 30 days', 0, 30), ('31 to 60 days', 31, 60), ('61 to 90 days', 61, 90), ('Over 90 days', 91, 10 ** 6))


def aging(association, as_of):
    """What we owe vendors on a day, bill by bill and grouped by vendor, by how long ago the invoice is (the old system's way of counting: the invoice date, the day itself counting as one).
    `books` is what the payables account holds, and `not_itemized` the part of it no bill explains."""
    rows = []
    for b in Bill.objects.filter(association=association, status__in=[Bill.Status.APPROVED, Bill.Status.PAID], date__lte=as_of).select_related('vendor').prefetch_related('payments__payment'):
        paid = sum((pb.amount for pb in b.payments.all() if pb.payment.date <= as_of and pb.payment.status != 'void'), ZERO)
        owed = b.amount - paid
        if owed <= 0:
            continue
        age = (as_of - b.date).days + 1
        buckets = [ZERO] * len(AGE_BUCKETS)
        for i, (_, lo, hi) in enumerate(AGE_BUCKETS):
            if lo <= age <= hi:
                buckets[i] = owed
                break
        rows.append({'bill': b, 'vendor': b.payee, 'buckets': buckets, 'owed': owed, 'age': age})
    rows.sort(key=lambda r: (r['vendor'].lower(), r['bill'].date))
    vendors = []
    for r in rows:
        if not vendors or vendors[-1]['name'] != r['vendor']:
            vendors.append({'name': r['vendor'], 'rows': [], 'buckets': [ZERO] * len(AGE_BUCKETS), 'owed': ZERO})
        v = vendors[-1]
        v['rows'].append(r)
        v['owed'] += r['owed']
        v['buckets'] = [a + b for a, b in zip(v['buckets'], r['buckets'])]
    total = sum((r['owed'] for r in rows), ZERO)
    ap = ledger.account(association, sc.AP)
    books = -ledger.balances_as_of(association, as_of, JournalEntry.Basis.ACCRUAL).get(ap.pk, ZERO)
    return {'vendors': vendors, 'rows': rows, 'labels': [b[0] for b in AGE_BUCKETS], 'totals': [sum((r['buckets'][i] for r in rows), ZERO) for i in range(len(AGE_BUCKETS))],
            'total': total, 'books': books, 'not_itemized': books - total, 'as_of': as_of}


# ---------------------------------------------------------------- paying

def _next_check_number(association):
    profile = ledger.profile_for(association)
    n = profile.next_check_number
    profile.next_check_number = n + 1
    profile.save(update_fields=['next_check_number'])
    return str(n)


def _allocate(amount, parts):
    """Split `amount` over [(key, weight)] by weight, to the cent, the remainder on the last."""
    total = sum((w for _, w in parts), ZERO)
    out, used = [], ZERO
    for i, (key, w) in enumerate(parts):
        share = (amount - used) if i == len(parts) - 1 else ledger.money(amount * w / total)
        out.append((key, share))
        used += share
    return out


@transaction.atomic
def pay_bills(association, bank_account, items, day, method=Payment.Method.CHECK, number=None, user=None, memo=''):
    """Pay approved bills [(bill, amount)] with one payment (one check can pay several bills from the same vendor)."""
    if not items:
        raise PayableError('Choose a bill to pay.')
    vendor_ids = {b.vendor_id for b, _ in items}
    if len(vendor_ids) > 1:
        raise PayableError('One payment goes to one vendor.')
    total = ZERO
    for bill, amount in items:
        amount = ledger.money(amount)
        if bill.status not in (Bill.Status.APPROVED,):
            raise PayableError(f'The bill from {bill.payee} is not ready to pay.')
        if amount <= 0 or amount > bill.balance:
            raise PayableError(f'The amount for {bill.payee} must be above zero and no more than ${bill.balance:,.2f}.')
        total += amount
    bill0 = items[0][0]
    check_number = (number if number is not None else (_next_check_number(association) if method == Payment.Method.CHECK else '')) or ''
    payment = Payment.objects.create(association=association, bank_account=bank_account, number=str(check_number), date=day, vendor=bill0.vendor, payee_text=bill0.vendor_text,
                                     amount=total, method=method, memo=memo[:200], created_by=user)
    desc = f'{bill0.payee}' + (f' check {check_number}' if check_number else '')
    accrual = [{'account': sc.AP, 'debit': total, 'vendor': bill0.vendor, 'memo': desc}, {'account': bank_account.account, 'credit': total, 'vendor': bill0.vendor, 'memo': desc}]
    cash = []
    for bill, amount in items:
        amount = ledger.money(amount)
        bill_lines = list(bill.lines.all())
        for line, part in _allocate(amount, [(l, l.amount) for l in bill_lines]):
            cash.append({'account': line.account, 'debit': part, 'vendor': bill.vendor, 'memo': desc})
        PaymentBill.objects.create(payment=payment, bill=bill, amount=amount)
    cash.append({'account': bank_account.account, 'credit': total, 'vendor': bill0.vendor, 'memo': desc})
    payment.entry = ledger.post_entry(association, day, accrual, memo=desc, basis=JournalEntry.Basis.ACCRUAL, source=JournalEntry.Source.PAYMENT, source_ref=f'payment:{payment.pk}', user=user)
    payment.cash_entry = ledger.post_entry(association, day, cash, memo=desc, basis=JournalEntry.Basis.CASH, source=JournalEntry.Source.PAYMENT, source_ref=f'payment:{payment.pk}', user=user)
    payment.save(update_fields=['entry', 'cash_entry'])
    for bill, _ in items:
        bill.refresh_from_db()
        if bill.balance <= 0:
            bill.status = Bill.Status.PAID
            bill.save(update_fields=['status'])
    return payment


@transaction.atomic
def void_payment(payment, user=None, day=None):
    if payment.status == Payment.Status.VOID:
        return
    if payment.status == Payment.Status.CLEARED:
        raise PayableError('That payment has cleared the bank. Reverse it with a journal entry instead.')
    day = day or timezone.localdate()
    for entry in (payment.entry, payment.cash_entry):
        if entry is not None and not entry.reversed_by.exists():
            ledger.reverse_entry(entry, day, user=user, memo=f'Void payment {payment.number} to {payment.payee}')
    for link in payment.bill_links.select_related('bill'):
        bill = link.bill
        link.delete()
        if bill.status == Bill.Status.PAID:
            bill.status = Bill.Status.APPROVED
            bill.save(update_fields=['status'])
    payment.status = Payment.Status.VOID
    payment.save(update_fields=['status'])


def outstanding_checks(association, bank_account=None):
    qs = Payment.objects.filter(association=association, status=Payment.Status.OUTSTANDING)
    if bank_account is not None:
        qs = qs.filter(bank_account=bank_account)
    return qs.order_by('date', 'number')


# ---------------------------------------------------------------- recurring bills

def post_recurring(association, today, user=None):
    """Post every recurring bill of a fixed amount that has come due; a bill whose amount changes (a utility) waits for the bank statement."""
    made = []
    for rec in RecurringBill.objects.filter(association=association, is_active=True).select_related('vendor', 'account'):
        if not rec.is_fixed:
            continue
        guard = 0
        while rec.next_date <= today and guard < 36:
            guard += 1
            day = rec.next_date
            if not Bill.objects.filter(recurring=rec, date=day).exists():
                try:
                    made.append(create_bill(association, day, [(rec.account, rec.amount, rec.description)], vendor=rec.vendor, number=f'{day:%Y-%m}', due=day,
                                            kind=Bill.Kind.ROUTINE, source=Bill.Source.RECURRING, user=user, recurring=rec, memo=rec.description))
                except ledger.LedgerError:
                    pass   # that month is closed
            rec.next_date = add_months(day, FREQ_MONTHS[rec.frequency])
            rec.save(update_fields=['next_date'])
    return made


def recurring_status(association, month_first):
    """For the close sheet: each recurring bill and whether this month's has posted (fixed) or is waiting on the statement (variable)."""
    rows = []
    nxt = ledger.next_month(month_first)
    for rec in RecurringBill.objects.filter(association=association, is_active=True).select_related('vendor'):
        posted = Bill.objects.filter(recurring=rec, date__gte=month_first, date__lt=nxt).exclude(status=Bill.Status.VOID).exists()
        due_this_month = rec.next_date < nxt or posted
        rows.append({'rec': rec, 'posted': posted, 'due': due_this_month, 'fixed': rec.is_fixed})
    return rows
