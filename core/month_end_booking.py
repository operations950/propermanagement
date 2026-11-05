"""Booking a month-end payment in QuickBooks: finding the one bank transaction it became and splitting it across the rentals.

A month-end payment (core/month_end_payments.py) is ONE amount in the bank. The bank feed puts it into QuickBooks as one transaction sitting in an "Uncategorized" account:
  * a withdrawal from the trust bank account (a Purchase / Expense, its line in "Uncategorized Expense") - for the owner payments, the reimbursable expenses and the commissions;
  * and, for the reimbursable expenses only, the matching deposit into the Proper Realty Operating account (a Deposit, its line in "Uncategorized Income"), which is what credits each
    rental's reimbursable-expense account.
The commissions' other side (income to Proper Realty) is not a rental's bookkeeping, so it is left alone.

  plan()   reads QuickBooks and says, without changing anything, which transaction each part would be, and what it would be split into
  apply()  re-reads that transaction, then replaces its uncategorized line(s) with one line per rental (the property's trust account, or its reimbursable-expense account for the
           deposit), described "Rental | what | month", and records exactly what it changed (MonthEndBooking) so it can be audited and undone by hand

It never guesses: no transaction with that amount and date, more than one equally likely, a rental with no QuickBooks account saved, or a transaction that has already been changed in
QuickBooks each leave it untouched with the reason. The amount has to equal the recorded total exactly."""
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from . import quickbooks
from .models import MonthEndBooking, MonthEndPayment, QuickBooksAccount

ZERO = Decimal('0.00')
BEFORE = timedelta(days=3)       # the bank may date it a little before the day it was recorded as paid
AFTER = timedelta(days=7)        # and usually posts it within a few days after
TRUST_BANK, OPERATING_BANK = 'Property Management', 'Proper Realty Operating'
UNCATEGORIZED_EXPENSE, UNCATEGORIZED_INCOME = 'Uncategorized Expense', 'Uncategorized Income'
Kind = MonthEndPayment.Kind


def _d(value):
    return Decimal(str(value)).quantize(Decimal('0.01'))


def _short(day):
    return f'{day:%b} {day.day}'


def _account(name):
    return QuickBooksAccount.objects.filter(name__iexact=name).first() or QuickBooksAccount.objects.filter(fully_qualified_name__iexact=name).first()


def _accounts():
    found, errors = {}, []
    for key, name in (('trust_bank', TRUST_BANK), ('operating_bank', OPERATING_BANK), ('uncat_expense', UNCATEGORIZED_EXPENSE), ('uncat_income', UNCATEGORIZED_INCOME)):
        account = _account(name)
        if account is None:
            errors.append(f'QuickBooks has no account called "{name}" (or it has not been synced yet).')
        found[key] = account
    return found, errors


def _targets(payment):
    """Which QuickBooks transactions this payment becomes: [(entity, label)]."""
    targets = [('Purchase', 'the withdrawal from the trust account')]
    if payment.kind == Kind.REIMBURSEMENT:
        targets.append(('Deposit', 'the deposit into Proper Realty Operating'))
    return targets


def _split_lines(payment, entity):
    """(lines, problems): one per rental line of the payment, with the QuickBooks account it goes to."""
    lines, problems = [], []
    month = f'{payment.month:%b %Y}'
    what = {Kind.OWNER: 'Owner payment', Kind.REIMBURSEMENT: 'Reimbursable expenses', Kind.COMMISSION: 'Commission'}[payment.kind]
    for line in payment.lines.select_related('property', 'unit', 'unit__qb_trust_account', 'unit__qb_expense_account', 'property__qb_trust_account', 'property__qb_expense_account'):
        owner = line.unit if line.unit_id and line.property.financials_level == 'unit' else line.property
        label = f'{line.property.name} - {line.unit.label}' if line.unit_id else line.property.name
        account = owner.qb_trust_account if entity == 'Purchase' else owner.qb_expense_account
        if account is None:
            problems.append(f'{label} has no QuickBooks {"balance-sheet (trust)" if entity == "Purchase" else "reimbursable-expense"} account saved')
            continue
        lines.append({'account_id': account.qb_id, 'account_name': account.name, 'where': label, 'amount': line.amount, 'description': f'{label} | {what} | {month}'})
    return lines, problems


def _purchase_lines(obj, uncategorized_id):
    return [ln for ln in obj.get('Line', []) if (ln.get('AccountBasedExpenseLineDetail') or {}).get('AccountRef', {}).get('value') == uncategorized_id]


def _deposit_lines(obj, uncategorized_id):
    return [ln for ln in obj.get('Line', []) if (ln.get('DepositLineDetail') or {}).get('AccountRef', {}).get('value') == uncategorized_id]


def _find(token, payment, entity, accounts):
    """(candidate objects, error) in QuickBooks for one part of the payment: the right bank account, only uncategorized lines, and exactly the payment's total."""
    first, last = payment.paid_on - BEFORE, payment.paid_on + AFTER
    objs, error = quickbooks.query(token, f"select * from {entity} where TxnDate >= '{first.isoformat()}' and TxnDate <= '{last.isoformat()}' order by TxnDate")
    if error:
        return [], error
    used = set(MonthEndBooking.objects.filter(entity=entity).values_list('qb_id', flat=True))
    out = []
    for obj in objs:
        if obj['Id'] in used or not (first.isoformat() <= obj['TxnDate'] <= last.isoformat()):
            continue
        if entity == 'Purchase':
            bank, uncat = accounts['trust_bank'], _purchase_lines(obj, accounts['uncat_expense'].qb_id)
            on_bank = (obj.get('AccountRef') or {}).get('value') == bank.qb_id
        else:
            bank, uncat = accounts['operating_bank'], _deposit_lines(obj, accounts['uncat_income'].qb_id)
            on_bank = (obj.get('DepositToAccountRef') or {}).get('value') == bank.qb_id
        if not on_bank or not uncat or len(uncat) != len(obj.get('Line', [])):
            continue          # another account, or already partly coded: not the untouched bank transaction this is looking for
        if sum((_d(ln['Amount']) for ln in uncat), ZERO) == payment.total:
            out.append((obj, uncat))
    return out, ''


def plan(token, payment):
    """What would be done: {'targets': [{'entity', 'label', 'status', 'reason', 'qb_id', 'date', 'amount', 'lines', 'raw', 'uncat'}], 'errors': [...]}. A target's status is
    'booked' (already done), 'ready', 'blocked' (found, but it can not be split yet: the reason says why) or 'unsure' (none, or more than one, found)."""
    result = {'targets': [], 'errors': []}
    if payment.total <= 0:
        result['errors'].append('The total is not more than $0.00 (the rentals owed to the trust account outweigh the rest), so there is no single withdrawal to find: book it by hand.')
        return result
    accounts, errors = _accounts()
    if errors:
        result['errors'] += errors
        return result
    done = {b.entity: b for b in payment.bookings.all()}
    for entity, label in _targets(payment):
        target = {'entity': entity, 'label': label, 'status': 'unsure', 'reason': '', 'qb_id': '', 'date': None, 'amount': payment.total, 'lines': [], 'raw': None, 'uncat': []}
        result['targets'].append(target)
        if entity in done:
            target.update(status='booked', qb_id=done[entity].qb_id, date=done[entity].qb_date, reason='already split in QuickBooks')
            continue
        found, error = _find(token, payment, entity, accounts)
        if error:
            result['errors'].append(error)
            target['reason'] = error
            continue
        if not found:
            target['reason'] = (f'no uncategorized {"withdrawal from the trust account" if entity == "Purchase" else "deposit into Proper Realty Operating"} of exactly ${payment.total:,.2f} '
                                f'between {_short(payment.paid_on - BEFORE)} and {_short(payment.paid_on + AFTER)} - the bank may not have posted it yet, or it was already coded')
            continue
        if len(found) > 1:
            target['reason'] = f'{len(found)} transactions of ${payment.total:,.2f} are equally likely - left for a person'
            continue
        obj, uncat = found[0]
        lines, problems = _split_lines(payment, entity)
        target.update(qb_id=obj['Id'], date=date.fromisoformat(obj['TxnDate']), raw=obj, uncat=uncat)
        if problems:
            target.update(status='blocked', reason='; '.join(problems))
        elif len(uncat) > len(lines):
            target.update(status='blocked', reason=f'it has {len(uncat)} uncategorized lines but only {len(lines)} rental line(s) to code them to - needs a person to look')
        else:
            target.update(status='ready', lines=lines)
    return result


def apply(token, payment, target, user=None):
    """Splits one planned transaction. Returns (True, '') or (False, why). It is read again first, so a change made in QuickBooks since the plan was drawn is never overwritten."""
    from django.core.cache import cache
    lock = f'month_end_booking_{target["entity"]}_{target["qb_id"]}'
    if not cache.add(lock, True, timeout=120):
        return False, 'already being sent to QuickBooks - try again shortly'
    try:
        return _apply_locked(token, payment, target, user)
    finally:
        cache.delete(lock)


def _apply_locked(token, payment, target, user):
    if target['status'] != 'ready':
        return False, target['reason'] or 'not ready'
    entity = target['entity']
    if MonthEndBooking.objects.filter(entity=entity, qb_id=target['qb_id']).exists():
        return False, 'already split'
    accounts, errors = _accounts()
    if errors:
        return False, errors[0]
    fresh, error = quickbooks.query(token, f"select * from {entity} where Id = '{target['qb_id']}'")
    if error or not fresh:
        return False, error or 'the transaction is no longer in QuickBooks'
    obj = fresh[0]
    purchase = entity == 'Purchase'
    uncategorized = (_purchase_lines if purchase else _deposit_lines)(obj, (accounts['uncat_expense'] if purchase else accounts['uncat_income']).qb_id)
    if obj.get('SyncToken') != target['raw'].get('SyncToken') or sum((_d(ln['Amount']) for ln in uncategorized), ZERO) != payment.total:
        return False, 'the transaction was changed in QuickBooks after the check - run it again'
    detail_key = 'AccountBasedExpenseLineDetail' if purchase else 'DepositLineDetail'
    klass = next((ln[detail_key]['ClassRef'] for ln in uncategorized if (ln.get(detail_key) or {}).get('ClassRef')), None)

    def coded(split):
        detail = {'AccountRef': {'value': split['account_id'], 'name': split['account_name']}}
        if klass:
            detail['ClassRef'] = klass
        return {'Amount': float(split['amount']), 'Description': split['description'], 'DetailType': detail_key, detail_key: detail}

    remaining = list(target['lines'])
    new_lines = []
    for ln in uncategorized:          # an existing line's Id is reused (a line can be edited in place but not removed - see quickbooks.update_object)
        edited = dict(ln)
        edited.update(coded(remaining.pop(0)))
        new_lines.append(edited)
    new_lines += [coded(ln) for ln in remaining]
    body = {'Id': obj['Id'], 'SyncToken': obj['SyncToken'], 'Line': new_lines}
    if purchase:
        body['AccountRef'] = obj.get('AccountRef')
        body['PaymentType'] = obj.get('PaymentType')
    else:
        body['DepositToAccountRef'] = obj.get('DepositToAccountRef')
    saved, error = quickbooks.update_object(token, entity, body, sparse=True)
    if error:
        return False, error
    with transaction.atomic():
        MonthEndBooking.objects.create(payment=payment, entity=entity, qb_id=obj['Id'], qb_date=target['date'], amount=payment.total, old_lines=uncategorized, new_lines=new_lines,
                                       applied_by=user if getattr(user, 'pk', None) else None)
        if payment.bookings.count() >= len(_targets(payment)):
            payment.booked_at = timezone.now()
            payment.booked_by = user if getattr(user, 'pk', None) else None
            payment.save(update_fields=['booked_at', 'booked_by'])
    return True, ''
