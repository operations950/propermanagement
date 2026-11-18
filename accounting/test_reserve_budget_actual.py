from datetime import date
from decimal import Decimal as D

from django.test import TestCase

from . import ledger, reporting
from .models import Budget, BudgetLine, JournalEntry, PriorYearToDate
from .tests import make_assoc


def flat(st):
    rows = []
    for b in st['blocks']:
        for g in b['groups']:
            rows += [(l['code'], l['label'], l['raw']) for l in g['lines']]
    return {code: raw for code, label, raw in rows}


class ReserveBudgetVsActualTests(TestCase):
    """The reserve lines of the budget comparison are what was PUT IN this year, never a fund's balance carried from the old system."""

    def setUp(self):
        self.prop = make_assoc('Glen-like Condos')
        prof = ledger.profile_for(self.prop)
        prof.books_start = date(2026, 9, 30)
        prof.save()
        a = lambda code: ledger.account(self.prop, code)
        # The opening entry carries the fund balances (credits) from the old system.
        ledger.post_entry(self.prop, date(2026, 9, 30), [
            {'account': a('1110'), 'debit': D('300000')},
            {'account': a('3826'), 'credit': D('57628')}, {'account': a('3834'), 'credit': D('123783')}, {'account': a('3835'), 'credit': D('26561.32')},
            {'account': a('3070'), 'credit': D('92027.68')}], memo='Opening balances', basis='accrual', source=JournalEntry.Source.OPENING, source_ref='yardi-sep30')
        bud = Budget.objects.create(association=self.prop, year=2026)
        for code, amount in (('3826', '1500'), ('3834', '2500'), ('3835', '88739')):
            BudgetLine.objects.create(budget=bud, account=a(code), annual_amount=D(amount))

    def contribute(self, day, code, amount):
        a = lambda c: ledger.account(self.prop, c)
        ledger.post_entry(self.prop, day, [{'account': a('6900'), 'debit': D(amount)}, {'account': a(code), 'credit': D(amount)}], memo='Reserve contribution', basis='accrual')

    def test_a_fund_that_only_carries_its_balance_shows_nothing_put_in(self):
        rows = flat(reporting.budget_vs_actual(self.prop, date(2026, 10, 31)))
        for code in ('3826', '3834', '3835'):
            self.assertEqual(rows[code][3], D('0'), code)                        # year to date actual: the balance is not a contribution
            self.assertEqual(rows[code][0], D('0'), code)                        # and not the month's either

    def test_only_contributions_after_the_opening_count(self):
        self.contribute(date(2026, 10, 27), '3826', '125')
        self.contribute(date(2026, 10, 27), '3834', '208.33')
        rows = flat(reporting.budget_vs_actual(self.prop, date(2026, 10, 31)))
        self.assertEqual(rows['3826'][3], D('125.00'))                           # year to date
        self.assertEqual(rows['3826'][0], D('125.00'))                           # this month
        self.assertEqual(rows['3834'][3], D('208.33'))
        self.assertEqual(rows['3826'][7], D('1500.00'))                          # annual budget
        self.assertEqual(rows['3826'][4].quantize(D('0.01')), D('1250.00'))      # 10 of 12 months of 1,500

    def test_contributions_before_our_books_are_part_of_the_year_to_date_only(self):
        PriorYearToDate.objects.create(association=self.prop, account=ledger.account(self.prop, '3826'), year=2026, amount=D('1125'))
        self.contribute(date(2026, 10, 27), '3826', '125')
        rows = flat(reporting.budget_vs_actual(self.prop, date(2026, 10, 31)))
        self.assertEqual(rows['3826'][3], D('1250.00'))                          # 1,125 before the books + 125 after
        self.assertEqual(rows['3826'][0], D('125.00'))                           # the month is only this month's

    def test_a_release_out_of_a_fund_is_not_a_negative_contribution(self):
        a = lambda c: ledger.account(self.prop, c)
        ledger.post_entry(self.prop, date(2026, 9, 30), [{'account': a('3835'), 'debit': D('26561.32')}, {'account': a('3070'), 'credit': D('26561.32')}], memo='Release', basis='accrual')
        rows = flat(reporting.budget_vs_actual(self.prop, date(2026, 10, 31)))
        self.assertEqual(rows['3835'][3], D('0'))
        self.assertEqual(rows['3835'][7], D('88739.00'))                         # still listed, because the budget names it

    def test_an_unbudgeted_fund_with_only_a_balance_is_not_listed(self):
        rows = flat(reporting.budget_vs_actual(self.prop, date(2026, 10, 31)))
        a = ledger.account(self.prop, '3827')
        ledger.post_entry(self.prop, date(2026, 9, 29), [{'account': ledger.account(self.prop, '1110'), 'debit': D('10')}, {'account': a, 'credit': D('10')}], memo='Opening', basis='accrual',
                          source=JournalEntry.Source.OPENING, source_ref='x')
        rows2 = flat(reporting.budget_vs_actual(self.prop, date(2026, 10, 31)))
        self.assertNotIn('3827', rows2)
        self.assertEqual(set(rows), set(rows2))

    def test_the_cash_book_reads_the_same_way(self):
        ledger.post_entry(self.prop, date(2026, 9, 30), [{'account': ledger.account(self.prop, '1110'), 'debit': D('300000')}, {'account': ledger.account(self.prop, '3826'), 'credit': D('300000')}],
                          memo='Opening', basis='cash', source=JournalEntry.Source.OPENING, source_ref='yardi-sep30')
        rows = flat(reporting.budget_vs_actual(self.prop, date(2026, 10, 31), basis='cash'))
        self.assertEqual(rows.get('3826', [None] * 4)[3], D('0'))
