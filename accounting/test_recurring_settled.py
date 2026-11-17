"""A recurring bill counts as posted for a month when its payment is in the books, however it got there: a bill made another way, or a statement line you put in the books yourself in the
reconciliation. The reconciliation also reads a fixed bill whose amount moved by a few cents."""
from datetime import date
from decimal import Decimal as D

from django.test import TestCase

from core.models import Contact

from . import bankrec, ledger, payables
from .models import Bill, BankLine, BankStatement, RecurringBill
from .tests import bank_for, make_assoc

OCT = date(2026, 10, 1)


class _Base(TestCase):
    def setUp(self):
        self.prop = make_assoc()
        self.bank = bank_for(self.prop)
        self.st = BankStatement.objects.create(bank_account=self.bank, period_end=date(2026, 10, 30), end_balance=D('0'))
        self.comcast = Contact.objects.create(name='Comcast')
        self.frontline = Contact.objects.create(name='Frontline Insurance')
        self.cable = RecurringBill.objects.create(association=self.prop, vendor=self.comcast, description='Bulk cable', account=ledger.account(self.prop, '6440'), amount=D('2077.97'),
                                                  pay_method='ach', statement_match='COMCAST', next_date=date(2026, 10, 11))
        self.internet = RecurringBill.objects.create(association=self.prop, vendor=self.comcast, description='Office internet', account=ledger.account(self.prop, '6440'), amount=D('128.67'),
                                                     pay_method='ach', statement_match='COMCAST', next_date=date(2026, 10, 6))
        self.insurance = RecurringBill.objects.create(association=self.prop, vendor=self.frontline, description='Insurance installment', account=ledger.account(self.prop, '6882'),
                                                      amount=D('12428.59'), pay_method='ach', next_date=date(2026, 10, 8))

    def line(self, text, amount, day=date(2026, 10, 13)):
        return BankLine.objects.create(statement=self.st, date=day, description=text, amount=D(amount), kind='withdrawal')

    def posted(self):
        return {r['rec'].description: r['posted'] for r in payables.recurring_status(self.prop, OCT)}


class SettledRecurringTests(_Base):
    def test_nothing_is_posted_to_begin_with(self):
        self.assertEqual(self.posted(), {'Bulk cable': False, 'Office internet': False, 'Insurance installment': False})

    def test_a_line_you_added_to_the_books_yourself_counts_when_its_words_and_amount_fit(self):
        bankrec.add_to_books(self.line('CABLE SVCS COMCAST-XFINITY 5552 LA *PENSEE', '-2078.49'), ledger.account(self.prop, '6440'))
        got = self.posted()
        self.assertTrue(got['Bulk cable'])                                       # 52 cents off the usual amount is still the cable bill
        self.assertFalse(got['Office internet'])                                 # the other Comcast bill is not settled by a $2,078 line

    def test_a_bill_made_another_way_for_about_the_amount_counts(self):
        payables.create_bill(self.prop, date(2026, 10, 7), [(ledger.account(self.prop, '6882'), D('12428.58'), 'Insurance')], vendor=self.frontline, number='2026-10', due=date(2026, 10, 7))
        self.assertTrue(self.posted()['Insurance installment'])                  # a penny off, made on the payables screen, not from the recurring bill

    def test_a_line_that_is_not_that_bill_does_not_count(self):
        bankrec.add_to_books(self.line('CABLE SVCS COMCAST-XFINITY 9999', '-500.00'), ledger.account(self.prop, '6440'))
        self.assertFalse(self.posted()['Bulk cable'])
        self.assertFalse(self.posted()['Office internet'])

    def test_the_schedule_does_not_post_a_second_bill_for_a_month_that_is_already_settled(self):
        bankrec.add_to_books(self.line('CABLE SVCS COMCAST-XFINITY 5552 LA *PENSEE', '-2078.49', date(2026, 10, 11)), ledger.account(self.prop, '6440'))
        before = Bill.objects.filter(recurring=self.cable).count()
        payables.post_recurring(self.prop, date(2026, 10, 31))
        self.assertEqual(Bill.objects.filter(recurring=self.cable).count(), before)

    def test_a_variable_bill_does_not_claim_a_line_that_belongs_to_a_fixed_bill_with_the_same_words(self):
        variable = RecurringBill.objects.create(association=self.prop, vendor=self.comcast, description='Cable (changes)', account=ledger.account(self.prop, '6440'), amount=None,
                                                pay_method='ach', statement_match='COMCAST', next_date=date(2026, 10, 9))
        bankrec.add_to_books(self.line('CABLE SVCS COMCAST-XFINITY 5552 LA *PENSEE', '-2078.49'), ledger.account(self.prop, '6440'))
        got = self.posted()
        self.assertTrue(got['Bulk cable'])
        self.assertFalse(got['Cable (changes)'])


class ReadingASlightlyDifferentAmountTests(_Base):
    def test_the_reconciliation_reads_a_fixed_bill_that_moved_by_a_few_cents(self):
        bankrec.auto_match(self.bank, OCT)                                       # nothing on the statement yet
        self.line('ACH CORP DEBIT WEB PAY Frontline Ins LA PENSEE CONDOMINIUM', '-12428.58', date(2026, 10, 7))
        self.line('CABLE SVCS COMCAST-XFINITY 5552 LA *PENSEE', '-2078.49', date(2026, 10, 13))
        report = {}
        n = bankrec.auto_match(self.bank, OCT, report=report)
        self.assertEqual(report.get('recurring'), 2)                             # both were read as their recurring bills, at the amounts the bank shows
        paid = {b.recurring.description: b.amount for b in Bill.objects.filter(recurring__isnull=False)}
        self.assertEqual(paid, {'Insurance installment': D('12428.58'), 'Bulk cable': D('2078.49')})
        got = self.posted()
        self.assertTrue(got['Bulk cable'] and got['Insurance installment'])
