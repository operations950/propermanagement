"""The general ledger shows the old system's own lines where the year to date came across as one opening entry, and the figures do not move."""
from datetime import date
from decimal import Decimal as D

from django.test import TestCase

from . import budgeting, ledger
from .models import JournalEntry, LedgerDetail
from .tests import make_assoc

Z = D('0')


def post_opening(prop, day, lines, basis='accrual', ref=''):
    return ledger.post_entry(prop, day, lines, memo='Opening balances, moved from the old system', basis=basis, source=JournalEntry.Source.OPENING, source_ref=ref, allow_locked=True)


def detail(prop, code, book, day, who, ctrl, remarks, debit='0', credit='0', ref=''):
    return LedgerDetail.objects.create(association=prop, account=ledger.account(prop, code), book=book, date=day, period=day.replace(day=1), who=who, ctrl=ctrl, reference=ref,
                                       debit=D(debit), credit=D(credit), remarks=remarks)


class OldSystemLedgerTests(TestCase):
    def setUp(self):
        self.prop = make_assoc('Old Glen')
        # Carried at 9/30: cash 5,000; insurance expense 3,000; owner fees income 8,000 (credit)
        post_opening(self.prop, date(2026, 9, 30), [
            {'account': ledger.account(self.prop, '1110'), 'debit': D('5000'), 'credit': Z},
            {'account': ledger.account(self.prop, '6882'), 'debit': D('3000'), 'credit': Z},
            {'account': ledger.account(self.prop, '4500'), 'debit': Z, 'credit': D('8000')}])
        detail(self.prop, '6882', 'accrual', date(2026, 2, 3), 'Citizens', 'K-1529', 'Windstorm premium', debit='2000', ref='Check 12')
        detail(self.prop, '6882', 'accrual', date(2026, 5, 9), 'Citizens', 'K-1700', 'Flood premium', debit='1000')
        detail(self.prop, '4500', 'accrual', date(2026, 1, 5), 'Owner A', 'R-2792', 'Online payment', credit='7000')
        detail(self.prop, '1110', 'accrual', date(2026, 1, 5), 'Owner A', 'R-2792', 'Online payment', debit='7000')
        detail(self.prop, '1110', 'accrual', date(2026, 2, 3), 'Citizens', 'K-1529', 'Windstorm premium', credit='2000')
        detail(self.prop, '1110', 'accrual', date(2026, 5, 9), 'Citizens', 'K-1700', 'Flood premium', credit='1000')

    def gl(self, code, start=date(2026, 1, 1), end=date(2026, 12, 31), basis='accrual'):
        return ledger.general_ledger(self.prop, ledger.account(self.prop, code), start, end, basis)

    def test_the_old_lines_show_with_their_own_notes_in_place_of_the_opening_line(self):
        g = self.gl('6882')
        self.assertEqual([(r['label'], r['who'], r['ref'], r['line'].memo) for r in g['rows'] if r['label'] != 'Adjustment'],
                         [('K-1529', 'Citizens', 'Check 12', 'Windstorm premium'), ('K-1700', 'Citizens', '', 'Flood premium')])
        self.assertEqual([r['entry'].date for r in g['rows']], [date(2026, 2, 3), date(2026, 5, 9)])
        self.assertFalse(any('Opening balances' in (r['line'].memo or '') or 'Opening balances' in (r['entry'].memo or '') for r in g['rows']))

    def test_a_balance_sheet_account_begins_the_year_with_what_the_lines_do_not_explain(self):
        g = self.gl('1110')
        self.assertEqual(g['start_balance'], D('1000'))                      # 5,000 carried less the lines (7,000 in, 3,000 out = 4,000) = 1,000 at the start of the year
        self.assertEqual(g['rows'][0]['running'], D('8000'))
        self.assertEqual(g['end_balance'], D('5000'))

    def test_an_income_account_differing_from_the_old_lines_shows_a_labeled_adjustment(self):
        g = self.gl('4500')
        self.assertEqual(g['start_balance'], Z)
        adj = [r for r in g['rows'] if r['label'] == 'Adjustment']
        self.assertEqual(len(adj), 1)
        self.assertEqual((adj[0]['line'].credit, adj[0]['line'].debit), (D('1000'), Z))
        self.assertEqual(g['end_balance'], D('-8000'))

    def test_every_end_balance_is_exactly_what_it_was_without_the_old_lines(self):
        for code in ('1110', '6882', '4500'):
            for basis in ('accrual', 'cash'):
                for start, end in ((date(2026, 1, 1), date(2026, 10, 31)), (date(2026, 3, 1), date(2026, 12, 31))):
                    acct = ledger.account(self.prop, code)
                    new = ledger.general_ledger(self.prop, acct, start, end, basis)['end_balance']
                    before = ledger.lines(self.prop, basis).filter(account=acct, entry__date__lte=end)
                    if not acct.is_balance_sheet:
                        before = before.filter(entry__date__gte=ledger.profile_for(self.prop).fiscal_year_start(end))
                    old = sum((l.debit - l.credit for l in before), Z)
                    if basis == 'cash':
                        self.assertEqual(new, old, (code, basis))            # no cash-book lines were loaded: the ledger is as it always was
                    else:
                        self.assertEqual(new, old, (code, basis, start))

    def test_a_window_starting_inside_the_lines_carries_the_earlier_ones_into_the_beginning_balance(self):
        g = self.gl('6882', start=date(2026, 4, 1))
        self.assertEqual(g['start_balance'], D('2000'))
        self.assertEqual(g['rows'][0]['label'], 'K-1700')
        self.assertEqual(g['end_balance'], D('3000'))

    def test_entries_after_the_carried_date_follow_the_old_lines(self):
        ledger.post_entry(self.prop, date(2026, 10, 10), [{'account': ledger.account(self.prop, '6882'), 'debit': D('250'), 'credit': Z},
                                                          {'account': ledger.account(self.prop, '1110'), 'debit': Z, 'credit': D('250')}], memo='October premium')
        g = self.gl('6882')
        self.assertEqual(g['rows'][-1]['label'].startswith('J-'), True)
        self.assertEqual(g['end_balance'], D('3250'))

    def test_a_line_dated_before_its_period_is_counted_in_the_period(self):
        LedgerDetail.objects.filter(ctrl='K-1700').update(date=date(2025, 12, 28), period=date(2026, 1, 1))
        g = self.gl('6882', start=date(2026, 1, 1), end=date(2026, 1, 31))
        self.assertEqual([r['label'] for r in g['rows']], ['K-1700'])         # shown with the old system's date, counted in January like the old ledger did

    def test_an_account_the_old_system_had_no_lines_on_begins_the_year_with_what_was_carried(self):
        post_opening(self.prop, date(2026, 9, 30), [{'account': ledger.account(self.prop, '3070'), 'debit': Z, 'credit': D('2000')},
                                                    {'account': ledger.account(self.prop, '1130'), 'debit': D('2000'), 'credit': Z}], ref='more')
        g = self.gl('3070')
        self.assertEqual((g['start_balance'], g['rows'], g['end_balance']), (D('-2000'), [], D('-2000')))      # a beginning balance, not an entry on September 30
        self.assertEqual(self.gl('1130')['start_balance'], D('2000'))

    def test_an_account_with_no_old_lines_is_untouched(self):
        g = self.gl('6894')
        self.assertEqual((g['rows'], g['start_balance'], g['end_balance']), ([], Z, Z))

    def test_the_range_ledger_and_screen_work_with_the_old_lines(self):
        a, b = ledger.account(self.prop, '4500'), ledger.account(self.prop, '6882')
        gls = ledger.general_ledger_range(self.prop, a, b, date(2026, 1, 1), date(2026, 12, 31), 'accrual')
        self.assertTrue(all('debits' in g and 'credits' in g for g in gls))
        self.assertIn('4500', [g['account'].code for g in gls])


class AssessmentAccountTests(TestCase):
    def test_the_assessment_account_is_4500_or_where_the_old_chart_had_it_4701(self):
        prop = make_assoc('Chart A')
        self.assertEqual(budgeting.assessment_account(prop).code, '4500')
        other = make_assoc('Chart B')
        ledger.account(other, '4500').delete()
        self.assertEqual(budgeting.assessment_account(other).code, '4701')
