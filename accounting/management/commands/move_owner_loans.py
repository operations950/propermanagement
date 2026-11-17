"""Take an owner's long-term loan out of their balance forward and set it up as their owner loan (kept apart from dues).

    manage.py move_owner_loans "Castlewood" --loan 114=27021.82 --loan 121=27021.82 --loan-payment R-4261=1000.81            (a dry run: everything is rolled back)
    manage.py move_owner_loans "Castlewood" --loan 114=27021.82 --apply

`--loan UNIT=AMOUNT` once per owner (UNIT is the unit's label). The amount comes out of the owner's balance forward and out of Accounts Receivable (a journal entry, debit Owner Loans
Receivable 1305, credit Accounts Receivable), dated the day the balance forward is dated. An amount larger than the balance forward that was carried is cut down to it (the old system's
figure rules). Payments made since the balance forward came over were applied to it oldest-first: they are taken off and put back, and `--loan-payment REF=AMOUNT` says how much of the
payment whose reference contains REF was a loan installment (the rest goes to the dues, oldest first). If a month it touches is closed the owner is skipped and said so;
`--into-closed-months` posts it anyway (it only moves money between accounts). Safe to run again: an owner who has a loan is skipped.
"""
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounting import charges, ledger, owner_loans, owners
from accounting.models import Receipt
from core.models import Property, Unit


class Rollback(Exception):
    pass


def _pairs(items, what):
    out = []
    for item in items:
        try:
            key, amount = item.split('=', 1)
            out.append((key.strip(), Decimal(amount.strip())))
        except (ValueError, InvalidOperation):
            raise CommandError(f'"{item}" is not {what}')
    return out


class Command(BaseCommand):
    help = 'Set up owner loans by moving them out of the balance forward. A dry run unless --apply.'

    def add_arguments(self, parser):
        parser.add_argument('association', help='A part of the association\'s name')
        parser.add_argument('--loan', action='append', default=[], help='UNIT=AMOUNT, once per owner')
        parser.add_argument('--loan-payment', action='append', default=[], help='RECEIPT-REFERENCE=AMOUNT that was a loan installment')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--into-closed-months', action='store_true')

    def handle(self, *args, **opts):
        props = Property.objects.filter(name__icontains=opts['association'])
        if props.count() != 1:
            raise CommandError(f'"{opts["association"]}" matches {props.count()} properties: {[p.name for p in props]}')
        prop = props.get()
        wanted = _pairs(opts['loan'], 'UNIT=AMOUNT')
        installments = _pairs(opts['loan_payment'], 'REFERENCE=AMOUNT')
        if not wanted:
            raise CommandError('Give at least one --loan UNIT=AMOUNT.')
        try:
            with transaction.atomic():
                ledger.ensure_chart(prop)                     # the loan account (1305) is added to an association's chart if it lacks it
                self.run(prop, wanted, installments, opts['into_closed_months'])
                if not opts['apply']:
                    raise Rollback
        except Rollback:
            self.stdout.write(self.style.WARNING('DRY RUN: rolled back. Add --apply to save.'))
            return
        self.stdout.write(self.style.SUCCESS('APPLIED.'))

    def run(self, prop, wanted, installments, closed_ok):
        for label, amount in wanted:
            unit = Unit.objects.filter(property=prop, label=label).first()
            if unit is None:
                self.stdout.write(f'  SKIP {label}: no such unit')
                continue
            owner = owners.billed_owner(unit)
            if owner is None:
                self.stdout.write(f'  SKIP {label}: no billed owner')
                continue
            if owner_loans.has_loan(unit, owner):
                self.stdout.write(f'  SKIP {label} {owner.name}: already has a loan')
                continue
            was = next((a for a in charges.accounts(prop) if a['unit'].pk == unit.pk and a['owner'].pk == owner.pk), None)
            opening = owner_loans.source_charge(unit, owner, Decimal('0.01'))
            if opening is None:
                self.stdout.write(f'  SKIP {label} {owner.name}: no balance forward')
                continue
            use = min(ledger.money(amount), opening.amount)
            pay = {}
            for ref, part in installments:
                for r in Receipt.objects.filter(unit=unit, is_void=False, reference__icontains=ref):
                    pay[r.pk] = part
            if ledger.is_locked(prop, opening.due_date) and not closed_ok:
                self.stdout.write(f'  SKIP {label} {owner.name}: {opening.due_date:%B %Y} is closed (add --into-closed-months to post it there anyway)')
                continue
            try:
                loan, notes = owner_loans.move_into_loan(prop, unit, owner, use, allow_locked=closed_ok, loan_payments=pay)
            except (charges.ChargeError, ledger.LedgerError) as e:
                self.stdout.write(f'  SKIP {label} {owner.name}: {e}')
                continue
            now = next(a for a in charges.accounts(prop) if a['unit'].pk == unit.pk and a['owner'].pk == owner.pk)
            cut = f' (cut down from ${amount:,.2f}: that is all the balance forward carried)' if use < ledger.money(amount) else ''
            self.stdout.write(f'  MOVE {label} {owner.name}: owed ${was["balance"] + was["loan"]:,.2f} -> dues ${now["balance"]:,.2f} + loan ${now["loan"]:,.2f}; loan ${use:,.2f}{cut}')
            for receipt, where, part in notes:
                self.stdout.write(f'         {receipt.date:%b %d} {receipt.reference[:28] or "payment"} ${part:,.2f} -> {where}')
