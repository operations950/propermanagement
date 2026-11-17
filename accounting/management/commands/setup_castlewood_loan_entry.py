"""Castlewood Villas: the recurring entry for the monthly special assessment loan payment, and the remembered split for its line on the 8021 statement.

    manage.py setup_castlewood_loan_entry            (a dry run: everything is rolled back)
    manage.py setup_castlewood_loan_entry --apply

The payment comes out of the 8021 account every month (about $6,554.76) and is part principal (2311 Special Assessment Loan) and part interest (6611 Special Assessment Loan Interest Expense).
The interest changes a little each month, so principal is "the rest": type the interest and the principal follows. Safe to run again: what is already there is left alone.
"""
from decimal import Decimal as D

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounting import bankrec, ledger, scheduled
from accounting.models import BankAccount, BankSplitTemplate, JournalRule
from core.models import Property

NAME = 'Monthly Spec Assmnt Loan Payment'
STATEMENT_WORDS = '#Automatic Ln Paymt AUTOMATIC LOAN PAY'        # as the 8021 statement words it; only its words are kept
DAY = 13
PRINCIPAL, INTEREST, PAYMENT = D('4232.73'), D('2322.03'), D('6554.76')


class Rollback(Exception):
    pass


class Command(BaseCommand):
    help = 'Set up Castlewood Villas\' recurring loan payment entry and its remembered split. A dry run unless --apply.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **opts):
        try:
            with transaction.atomic():
                self.run()
                if not opts['apply']:
                    raise Rollback
        except Rollback:
            self.stdout.write('DRY RUN: rolled back.')
        else:
            self.stdout.write('APPLIED.')

    def run(self):
        prop = Property.objects.filter(name='Castlewood Villas').first()
        if prop is None:
            raise CommandError('There is no property called Castlewood Villas.')
        bank = BankAccount.objects.filter(association=prop, last4='8021').first()
        if bank is None:
            raise CommandError('Castlewood has no bank account ending 8021.')
        principal, interest = ledger.account(prop, '2311'), ledger.account(prop, '6611')
        rule = JournalRule.objects.filter(association=prop, kind=JournalRule.Kind.ENTRY, name=NAME).first()
        if rule is None:
            rule = scheduled.save_entry_rule(prop, NAME, DAY, [
                {'account': principal, 'side': 'debit', 'amount': PRINCIPAL, 'memo': 'Special assessment loan principal', 'is_rest': True},
                {'account': interest, 'side': 'debit', 'amount': INTEREST, 'memo': 'Special assessment loan interest'},
                {'account': bank.account, 'side': 'credit', 'amount': PAYMENT, 'memo': 'Automatic loan payment'},
            ], auto_post=False)
            self.stdout.write(f'  NEW  recurring entry "{NAME}": day {DAY}, {principal.code} {PRINCIPAL} (the rest) + {interest.code} {INTEREST} = {bank.account.code} {PAYMENT}, automatic posting off')
        else:
            self.stdout.write(f'  have recurring entry "{NAME}"')
        words = bankrec._words(STATEMENT_WORDS)
        tpl, made = BankSplitTemplate.objects.get_or_create(bank_account=bank, words=words, outgoing=True, defaults={'lines': [
            {'account': principal.pk, 'amount': str(PRINCIPAL), 'memo': 'Special assessment loan principal', 'rest': True},
            {'account': interest.pk, 'amount': str(INTEREST), 'memo': 'Special assessment loan interest', 'rest': False}]})
        self.stdout.write(f"  {'NEW ' if made else 'have'} remembered split on {bank.name} for \"{words}\": {principal.code} (the rest) + {interest.code}")
