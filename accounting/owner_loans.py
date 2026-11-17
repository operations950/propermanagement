"""Owner loans: a long-term loan an owner repays on a schedule, kept apart from their dues.

An owner loan is a charge of its own kind (Charge.Kind.LOAN) whose receivable sits in its own balance-sheet account (1305, Owner Loans Receivable), not in Accounts Receivable. So:

  * it is not in the owner's balance, the delinquency list, the aging report, a late letter or the "behind" amount;
  * no late fee and no interest is charged on it (those only ever look at assessments);
  * a payment never pays it by itself: the person says "this payment is for the loan" (charges.receive_payment(to_loan=True));
  * the owner's page shows it on its own line, "Loan balance", with its own small ledger.

The loan balance is what is left to pay on the schedule (principal and the interest still to come), because that is what the old system carried in the owner's receivable, against the
deferred special assessment accounts. A payment on the loan lowers it by the whole installment.

move_into_loan() is the one-time move: it takes the loan out of an owner's balance forward (and out of Accounts Receivable, with a journal entry) and sets it up as their loan. The balance
forward was a lump, so payments made since it was carried over were applied to it oldest-first: those payments are taken off, the loan installments among them are put on the loan, and the
rest go back onto the dues, so the owner's dues and loan each show what was really paid on them.
"""
from decimal import Decimal

from django.db import transaction

from . import ledger
from . import standard_chart as sc
from .charges import ChargeError, loan_balance, open_charges
from .models import Application, Charge, JournalEntry

ZERO = Decimal('0.00')


def has_loan(unit, owner):
    return Charge.objects.filter(unit=unit, owner=owner, kind=Charge.Kind.LOAN, is_void=False).exists()


def source_charge(unit, owner, amount):
    """The charge the loan comes out of: the owner's balance forward (what came over from the old system) that was carried for at least `amount`, whatever has been paid on it since.
    None when there is no such charge."""
    rows = [c for c in Charge.objects.filter(unit=unit, owner=owner, is_void=False, kind=Charge.Kind.OPENING).order_by('due_date', 'id') if c.amount >= amount]
    return rows[0] if rows else None


def loan_income_account(association):
    """The income account a loan payment lands in on the cash book (a collected special assessment), where the books have it."""
    for code in ('4515', sc.INCOME_SPECIAL):
        try:
            return ledger.account(association, code)
        except ledger.LedgerError:
            continue
    return ledger.account(association, sc.INCOME_ASSESSMENT)


@transaction.atomic
def move_into_loan(association, unit, owner, amount, user=None, day=None, allow_locked=False, description='Loan balance', loan_payments=None):
    """Take `amount` out of the owner's balance forward and make it their owner loan. Returns (the loan Charge, [what was re-applied]).

    `loan_payments` is {receipt id: the part of that receipt that was a loan installment}. Posts: debit Owner Loans Receivable, credit Accounts Receivable (this unit and owner) for the loan,
    and for each loan installment the books are corrected (accrual: it paid the loan, not receivables; cash: it is special assessment income, not assessment income)."""
    amount = ledger.money(amount)
    loan_payments = {k: ledger.money(v) for k, v in (loan_payments or {}).items()}
    if amount <= 0:
        raise ChargeError('A loan needs an amount above zero.')
    if has_loan(unit, owner):
        raise ChargeError(f'{unit.label} already has a loan set up.')
    source = source_charge(unit, owner, amount)
    if source is None:
        raise ChargeError(f'{unit.label}: no balance forward was carried for ${amount:,.2f}, so it cannot be moved.')
    day = day or source.due_date
    memo = f'{description}: moved out of {source.description}'
    # 1. the payments that were applied to the balance forward come off it; they are put back below
    paid = list(source.applications.select_related('receipt').order_by('date', 'id'))
    taken = [(a.receipt, a.amount, a.date) for a in paid]
    Application.objects.filter(pk__in=[a.pk for a in paid]).delete()
    # 2. the loan leaves receivables and becomes its own charge
    entry = ledger.post_entry(association, day, [{'account': sc.OWNER_LOAN, 'debit': amount, 'unit': unit, 'owner': owner, 'memo': memo},
                                                 {'account': sc.AR, 'credit': amount, 'unit': unit, 'owner': owner, 'memo': memo}],
                              memo=memo, basis=JournalEntry.Basis.ACCRUAL, source=JournalEntry.Source.MANUAL, source_ref=f'ownerloan:{unit.pk}:{owner.pk}', user=user, allow_locked=allow_locked)
    income = loan_income_account(association)
    loan = Charge.objects.create(association=association, unit=unit, owner=owner, kind=Charge.Kind.LOAN, description=description[:200], due_date=day, amount=amount, income_account=income, entry=entry)
    source.amount -= amount
    if source.amount <= 0:
        source.is_void = True              # all of it was the loan: the entry that brought it in stays, the reclass above took it out of receivables
    source.save(update_fields=['amount', 'is_void'])
    # 3. put each payment back: its loan installment on the loan, the rest on the dues, oldest first
    notes = []
    for receipt, was, on in taken:
        want = min(loan_payments.get(receipt.pk, ZERO), was, loan.amount - sum((a.amount for a in loan.applications.all()), ZERO))
        if want > 0:
            Application.objects.create(receipt=receipt, charge=loan, amount=want, date=on)
            ledger.post_entry(association, receipt.date, [{'account': sc.AR, 'debit': want, 'unit': unit, 'owner': owner, 'memo': f'{receipt.reference or "Payment"}: loan installment'},
                                                          {'account': sc.OWNER_LOAN, 'credit': want, 'unit': unit, 'owner': owner, 'memo': f'{receipt.reference or "Payment"}: loan installment'}],
                              memo=f'Loan installment, {unit.label}', basis=JournalEntry.Basis.ACCRUAL, source=JournalEntry.Source.MANUAL, source_ref=f'ownerloan-pay:{receipt.pk}', user=user, allow_locked=allow_locked)
            ledger.post_entry(association, receipt.date, [{'account': ledger.account(association, sc.INCOME_ASSESSMENT), 'debit': want, 'memo': 'Loan installment: not assessment income'},
                                                          {'account': income, 'credit': want, 'memo': 'Loan installment: special assessment income'}],
                              memo=f'Loan installment, {unit.label}', basis=JournalEntry.Basis.CASH, source=JournalEntry.Source.MANUAL, source_ref=f'ownerloan-pay:{receipt.pk}', user=user, allow_locked=allow_locked)
            notes.append((receipt, 'loan', want))
        left = was - want
        for charge in open_charges(unit, owner):
            if left <= 0:
                break
            take = min(charge.balance, left)
            Application.objects.create(receipt=receipt, charge=charge, amount=take, date=on)
            notes.append((receipt, charge.description, take))
            left -= take
        if left > 0:
            raise ChargeError(f'{unit.label}: ${left:,.2f} of {receipt.reference or "a payment"} has no charge left to pay.')
    return loan, notes


def total_loans(association):
    """(owners with a loan, what they owe on it in all) for an association."""
    units = set(Charge.objects.filter(association=association, kind=Charge.Kind.LOAN, is_void=False).values_list('unit_id', 'owner_id'))
    total = Decimal('0.00')
    from core.models import Contact, Unit
    for unit_id, owner_id in units:
        total += loan_balance(Unit.objects.get(pk=unit_id), Contact.objects.get(pk=owner_id))
    return len(units), total
