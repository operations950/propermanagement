"""A balance forward (what an owner owed when the books moved over from the old system) can be charged a late fee, on what is still unpaid of it, and interest from the day the books
start: the old system charged its own before that. A loan never is."""
from datetime import date
from decimal import Decimal as D

from django.test import TestCase

from core.models import Unit

from . import charges, ledger, owner_loans
from .models import BankAccount, Charge, JournalEntry
from .tests import make_assoc, owner_of

START = date(2026, 9, 30)


class BalanceForwardFeeTests(TestCase):
    def setUp(self):
        self.prop = make_assoc()
        self.unit = Unit.objects.filter(property=self.prop).first()
        self.owner = owner_of(self.prop, self.unit)
        BankAccount.objects.create(association=self.prop, name='Operating', bank_name='Seacoast', last4='1234', kind='operating', account=ledger.account(self.prop, '1110'))
        profile = ledger.profile_for(self.prop)
        profile.late_fee_kind, profile.late_fee_flat, profile.late_fee_percent, profile.late_fee_grace_days = 'greater', D('25'), D('5'), 10
        profile.interest_rate, profile.interest_grace_days, profile.books_start = D('18'), 0, START
        profile.late_fee_confirmed = profile.interest_confirmed = True
        profile.save()

    def opening(self, amount='4659.76', due=date(2026, 7, 17)):
        c = Charge.objects.create(association=self.prop, unit=self.unit, owner=self.owner, kind=Charge.Kind.OPENING, description='Balance forward from Yardi, 61 to 90 days', due_date=due,
                                  amount=D(amount), income_account=ledger.account(self.prop, '4500'))
        ledger.post_entry(self.prop, START, [{'account': '1300', 'debit': D(amount), 'unit': self.unit, 'owner': self.owner}, {'account': '3070', 'credit': D(amount)}],
                          memo='Opening balances', basis='accrual', source=JournalEntry.Source.OPENING)
        return c

    def test_a_balance_forward_gets_a_late_fee_on_what_is_unpaid_of_it(self):
        c = self.opening('4659.76')
        rows = charges._late_fee_rows(self.prop, date(2026, 10, 31))
        self.assertEqual([(r[0].pk, r[1]) for r in rows], [(c.pk, D('232.99'))])               # 5% of 4,659.76: more than the $25
        charges.receive_payment(self.prop, self.unit, self.owner, date(2026, 10, 20), D('4000.00'))
        self.assertEqual([r[1] for r in charges._late_fee_rows(self.prop, date(2026, 10, 31))], [D('32.99')])     # 5% of the 659.76 left

    def test_once_charged_it_is_not_charged_again(self):
        self.opening()
        self.assertEqual(len(charges.assess_late_fees(self.prop, date(2026, 10, 31))), 1)
        self.assertEqual(charges.assess_late_fees(self.prop, date(2026, 10, 31)), [])

    def test_interest_runs_on_a_balance_forward_only_from_the_day_the_books_start(self):
        self.opening('4659.76')
        rows = charges._interest_rows(self.prop, date(2026, 11, 17), as_of=date(2026, 10, 31))
        self.assertEqual([(m.month, a) for c, m, e, a in rows], [(10, D('71.24'))])          # October only: 4,659.76 x 18% x 31/365, nothing for July to September
        made = charges.assess_interest(self.prop, date(2026, 11, 17), as_of=date(2026, 10, 31))
        self.assertEqual([c.amount for c in made], [D('71.24')])
        self.assertEqual(made[0].parent.kind, 'opening')

    def test_a_loan_gets_neither(self):
        opening = self.opening('27021.82')
        owner_loans.move_into_loan(self.prop, self.unit, self.owner, D('27021.82'))
        self.assertEqual(charges._late_fee_rows(self.prop, date(2026, 10, 31)), [])
        self.assertEqual(charges._interest_rows(self.prop, date(2026, 11, 17), as_of=date(2026, 10, 31)), [])
        self.assertTrue(Charge.objects.get(pk=opening.pk).is_void)
