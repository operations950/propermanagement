"""A budget draft that is last year's approved budget, line for line ("Same as last year"), plus the lines typed on top."""
import io
from datetime import date
from decimal import Decimal as D

from django.test import TestCase
from pypdf import PdfReader

from . import budget_export, budgeting as bg, ledger, views_budget as vb
from .models import Budget, BudgetLine
from .tests import close, make_assoc, spend


class FromPriorTests(TestCase):
    def setUp(self):
        self.prop = make_assoc('Same Condos')
        bud = Budget.objects.create(association=self.prop, year=2026)
        BudgetLine.objects.create(budget=bud, account=ledger.account(self.prop, '6906'), annual_amount=D('12000'))      # pest control
        BudgetLine.objects.create(budget=bud, account=ledger.account(self.prop, '6893'), annual_amount=D('6000'))      # landscaping
        BudgetLine.objects.create(budget=bud, account=ledger.account(self.prop, bg.assessment_account(self.prop).code), annual_amount=D('18000'))
        for m in range(1, 7):
            spend(self.prop, date(2026, m, 10), '6906', '2500')        # far over the budget: a special project
        spend(self.prop, date(2026, 3, 5), '6898', '300')              # spending with no budget line last year
        # income with no budget line last year
        ledger.post_entry(self.prop, date(2026, 2, 5), [{'account': ledger.account(self.prop, '1110'), 'debit': D('90'), 'credit': D('0')},
                                                        {'account': ledger.account(self.prop, '4702'), 'debit': D('0'), 'credit': D('90')}], memo='late fee')
        close(self.prop, range(1, 7))

    def test_every_line_is_last_years_budget_whatever_the_months_did(self):
        ver = bg.start_draft(self.prop, 2027, None, from_prior=True)
        self.assertTrue(ver.from_prior)
        by = {l.account.code: bg.compute(l, bg.info_for(ver))['proposed'] for l in ver.lines.select_related('account')}
        self.assertEqual(by['6906'], D('12000.00'))
        self.assertEqual(by['6893'], D('6000.00'))

    def test_spending_and_income_last_year_had_no_budget_for_are_not_carried(self):
        ver = bg.start_draft(self.prop, 2027, None, from_prior=True)
        codes = set(ver.lines.values_list('account__code', flat=True))
        self.assertNotIn('6898', codes)
        self.assertNotIn('4702', codes)

    def test_opening_the_screens_never_adds_income_or_refreshes_a_same_as_last_year_draft(self):
        ver = bg.start_draft(self.prop, 2027, None, from_prior=True)
        before = ver.lines.count()
        have = bg.versions(ver.budget)
        self.assertFalse(bg.heal(ver, have))
        self.assertFalse(bg.is_stale(ver))
        self.assertEqual(ver.lines.count(), before)

    def test_an_ordinary_draft_is_still_worked_out_from_the_actuals(self):
        ver = bg.start_draft(self.prop, 2027, None)
        self.assertFalse(ver.from_prior)
        self.assertIn('6898', set(ver.lines.values_list('account__code', flat=True)))
        pest = ver.lines.get(account__code='6906')
        self.assertEqual(bg.compute(pest, bg.info_for(ver))['proposed'], D('30000.00'))        # 15,000 so far at 2,500 a month, six months left

    def test_the_board_draft_keeps_the_mode(self):
        bg.start_draft(self.prop, 2027, None, from_prior=True)
        bud = bg.get_budget(self.prop, 2027)
        board = bg.advance(bud, None)
        self.assertTrue(board.from_prior)

    def test_the_regular_assessments_line_finds_last_years_budget_on_either_account(self):
        ver = bg.start_draft(self.prop, 2027, None, from_prior=True)
        row = bg.assessment_row(ver, bg.info_for(ver), D('18000'))
        self.assertEqual(row['prior'], 18000.0)

    def test_the_sheet_pdf_and_excel_leave_the_projection_column_out(self):
        ver = bg.start_draft(self.prop, 2027, None, from_prior=True)
        sheet = vb._sheet(ver, bg.info_for(ver), bg.unit_count(self.prop))
        self.assertTrue(sheet['hide_proj'])
        sheet.update({'assoc': self.prop.name, 'stage_label': 'Management draft', 'on': date(2026, 11, 25)})
        text = ' '.join(p.extract_text() for p in PdfReader(io.BytesIO(budget_export.pdf(sheet))).pages)
        self.assertNotIn('projected', text)
        self.assertIn('2026 budget', text)
        import openpyxl
        ws = openpyxl.load_workbook(io.BytesIO(budget_export.xlsx(sheet))).active
        self.assertTrue(ws.column_dimensions['C'].hidden)

    def test_the_start_button_makes_the_mode(self):
        from django.contrib.auth import get_user_model
        from django.urls import reverse
        admin = get_user_model().objects.create_superuser('boss', 'boss@example.com', 'x' * 20)
        self.client.force_login(admin)
        url = reverse('accounting:budget_year', args=[self.prop.pk, 2027])
        self.assertContains(self.client.get(url), 'Same as last year')
        self.client.post(url, {'action': 'start', 'from_prior': '1'})
        self.assertTrue(bg.versions(bg.get_budget(self.prop, 2027))['draft'].from_prior)
