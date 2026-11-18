from datetime import date
from decimal import Decimal as D

from django.test import TestCase

from core.models import Contact, Unit
from . import budgeting as bg
from . import charges, ledger, reporting
from .models import BankAccount, Budget, BudgetLine, Charge, JournalEntry
from .tests import make_assoc


class LateFeeIsOnWhatIsStillUnpaidTests(TestCase):
    """A part-paid installment is charged its late fee on the unpaid rest, not on the whole installment (a balance forward always was)."""

    def setUp(self):
        self.prop = make_assoc('Fee Base Condos')
        prof = ledger.profile_for(self.prop)
        prof.late_fee_kind, prof.late_fee_flat, prof.late_fee_percent, prof.late_fee_grace_days, prof.late_fee_confirmed = 'percent', None, D('5'), 10, True
        prof.save()
        BankAccount.objects.create(association=self.prop, name='Operating', last4='1234', kind='operating', account=ledger.account(self.prop, '1110'))
        self.unit = Unit.objects.filter(property=self.prop).first()
        self.owner = Contact.objects.create(name='Owner One', contact_type='owner')
        self.charge = charges.post_charge(self.prop, self.unit, self.owner, Charge.Kind.ASSESSMENT, 'Regular assessment Oct 2026', date(2026, 10, 1), D('7268.47'))

    def fee(self):
        rows = charges._late_fee_rows(self.prop, date(2026, 10, 31))
        return rows[0][1] if rows else None

    def test_an_unpaid_installment_is_charged_on_the_whole_amount(self):
        self.assertEqual(self.fee(), D('363.42'))                              # 5% of 7,268.47

    def test_a_part_paid_installment_is_charged_on_what_is_left(self):
        charges.receive_payment(self.prop, self.unit, self.owner, date(2026, 9, 30), D('261.43'), memo='paid ahead')
        self.assertEqual(self.fee(), D('350.35'))                              # 5% of 7,007.04

    def test_a_fully_paid_installment_has_no_fee(self):
        charges.receive_payment(self.prop, self.unit, self.owner, date(2026, 10, 5), D('7268.47'))
        self.assertIsNone(self.fee())

    def test_the_greater_of_a_flat_amount_and_a_percent_uses_the_unpaid_rest_too(self):
        prof = ledger.profile_for(self.prop)
        prof.late_fee_kind, prof.late_fee_flat = 'greater', D('400')
        prof.save()
        charges.receive_payment(self.prop, self.unit, self.owner, date(2026, 9, 30), D('261.43'))
        self.assertEqual(self.fee(), D('400.00'))                              # the flat amount is the greater of 400 and 350.35


class FundMovesAreNotContributionsTests(TestCase):
    """Moving money from one reserve fund to another (or reclassifying a fund) is not a contribution to the year's reserve budget."""

    def setUp(self):
        self.prop = make_assoc('Reserve Move Condos')
        prof = ledger.profile_for(self.prop)
        prof.books_start = date(2026, 9, 30)
        prof.save()
        a = lambda code: ledger.account(self.prop, code)
        self.a = a
        ledger.post_entry(self.prop, date(2026, 9, 30), [{'account': a('1110'), 'debit': D('100000')}, {'account': a('3827'), 'credit': D('100000')}], memo='Opening balances', basis='accrual',
                          source=JournalEntry.Source.OPENING, source_ref='yardi-sep30')
        bud = Budget.objects.create(association=self.prop, year=2026)
        BudgetLine.objects.create(budget=bud, account=a('3829'), annual_amount=D('12000'))

    def ytd(self):
        st = reporting.budget_vs_actual(self.prop, date(2026, 10, 31))
        rows = {l['code']: l['raw'] for b in st['blocks'] for g in b['groups'] for l in g['lines']}
        return rows['3829'][3], rows['3829'][0]

    def test_a_contribution_paid_by_the_reserve_transfer_expense_counts(self):
        ledger.post_entry(self.prop, date(2026, 10, 27), [{'account': self.a('6900'), 'debit': D('1000')}, {'account': self.a('3829'), 'credit': D('1000')}], memo='Reserve contribution', basis='accrual')
        self.assertEqual(self.ytd(), (D('1000.00'), D('1000.00')))

    def test_a_move_from_one_fund_to_another_does_not_count(self):
        ledger.post_entry(self.prop, date(2026, 10, 27), [{'account': self.a('3827'), 'debit': D('60000')}, {'account': self.a('3829'), 'credit': D('60000')}], memo='Move to the SIRS fund', basis='accrual')
        ledger.post_entry(self.prop, date(2026, 10, 27), [{'account': self.a('6900'), 'debit': D('1000')}, {'account': self.a('3829'), 'credit': D('1000')}], memo='Reserve contribution', basis='accrual')
        self.assertEqual(self.ytd(), (D('1000.00'), D('1000.00')))              # only the 1,000 paid in

    def test_an_entry_paid_for_by_income_or_a_liability_counts(self):
        ledger.post_entry(self.prop, date(2026, 10, 27), [{'account': self.a('4511'), 'debit': D('500')}, {'account': self.a('3829'), 'credit': D('500')}], memo='Interest to the fund', basis='accrual')
        self.assertEqual(self.ytd()[0], D('500.00'))

    def test_the_budget_builders_actuals_leave_the_move_out_too(self):
        ledger.post_entry(self.prop, date(2026, 10, 27), [{'account': self.a('3827'), 'debit': D('60000')}, {'account': self.a('3829'), 'credit': D('60000')}], memo='Move', basis='accrual')
        ledger.post_entry(self.prop, date(2026, 10, 28), [{'account': self.a('6900'), 'debit': D('1000')}, {'account': self.a('3829'), 'credit': D('1000')}], memo='Contribution', basis='accrual')
        acct = self.a('3829')
        got = bg._actuals(self.prop, date(2026, 1, 1), date(2026, 12, 31), [acct], True, None)
        self.assertEqual(got[acct.pk], D('1000.00'))
