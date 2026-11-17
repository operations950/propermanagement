"""A budget is paid monthly or quarterly. It is chosen on the management draft or the Board draft, with a warning, and then locked; every sheet, PDF, notice and the loaded assessment rule follow it.
The by-unit calculation lines up under the same columns as the budget above it."""
import io
from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import Unit

from . import budget_export, budget_project as bp, budgeting as bg
from .models import Budget
from .test_budget_sections import STORAGES
from .tests import close, make_assoc, owner_of, spend


@override_settings(STORAGES=STORAGES)
class PaymentFrequencyTests(TestCase):
    def setUp(self):
        self.prop = make_assoc('Frequency Condos')
        for m in range(1, 7):
            spend(self.prop, date(2026, m, 10), '6906', '600')
        close(self.prop, range(1, 7))
        for u in Unit.objects.filter(property=self.prop):
            owner_of(self.prop, u, f'Owner {u.label}')
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.page = reverse('accounting:budget_year', args=[self.prop.pk, 2027])
        self.client.post(self.page, {'action': 'start'})

    def bud(self):
        return Budget.objects.get(association=self.prop, year=2027)

    def sheet(self, stage='draft'):
        from .views_budget import _sheet
        have = bg.versions(self.bud())
        return _sheet(have[stage], bg.info_for(have[stage]), bg.unit_count(self.prop), False)

    def set_frequency(self, value, confirm='yes', stage='draft'):
        return self.client.post(self.page, {'action': 'frequency', 'frequency': value, 'ack': confirm, 'stage': stage}, follow=True)

    def test_it_is_monthly_and_open_to_start_with(self):
        info = bg.frequency_info(self.bud())
        self.assertEqual((info['key'], info['locked'], info['per_year']), ('monthly', False, 12))
        html = self.client.get(self.page).content.decode()
        self.assertIn('name="frequency"', html)
        self.assertIn('Set and lock', html)
        self.assertIn('cannot be changed afterwards', html)                                # the warning that asks you to say yes

    def test_setting_quarterly_locks_it_and_the_screen_says_so(self):
        r = self.set_frequency('quarterly')
        self.assertContains(r, 'Payments are quarterly')
        b = self.bud()
        self.assertEqual((b.payment_frequency, b.frequency_locked), ('quarterly', True))
        html = self.client.get(self.page).content.decode()
        self.assertNotIn('Set and lock', html)
        self.assertIn('locked: every sheet and PDF shows the quarterly amount', html)

    def test_it_is_refused_without_the_confirmation_and_once_locked_it_cannot_change(self):
        r = self.set_frequency('quarterly', confirm='')
        self.assertContains(r, 'Confirm the payment frequency first')
        self.assertFalse(self.bud().frequency_locked)
        self.set_frequency('quarterly')
        r = self.set_frequency('monthly')
        self.assertContains(r, 'that is locked')
        self.assertEqual(self.bud().payment_frequency, 'quarterly')

    def test_it_cannot_be_set_after_the_meeting_version_is_made(self):
        self.client.post(self.page, {'action': 'advance'})                                 # the Board draft
        self.assertTrue(bg.can_set_frequency(self.bud(), bg.versions(self.bud())))         # still fine on the Board draft
        self.client.post(self.page, {'action': 'advance'})                                 # the meeting version
        self.assertFalse(bg.can_set_frequency(self.bud(), bg.versions(self.bud())))

    def test_the_by_unit_calculation_is_per_quarter_when_quarterly_and_per_month_when_not(self):
        monthly = self.sheet()['calc'][0]['rows']
        self.assertEqual(monthly[0]['label'], 'Monthly assessment')
        self.set_frequency('quarterly')
        quarterly = self.sheet()['calc'][0]['rows']
        self.assertEqual(quarterly[0]['label'], 'Quarterly assessment')
        self.assertAlmostEqual(quarterly[0]['new'], monthly[0]['new'] * 3, places=2)       # a quarter is three months
        self.assertAlmostEqual(quarterly[1]['new'], monthly[1]['new'], places=2)           # the year per unit does not change
        self.assertEqual(self.sheet()['frequency'], 'quarterly')

    def test_the_form_cannot_lose_its_warning_to_a_field_called_confirm(self):
        html = self.client.get(self.page).content.decode()
        self.assertIn('return window.confirm(', html)                                      # a field named confirm would hide the browser's own and the warning would never show
        self.assertNotIn('name="confirm"', html)

    def test_the_screen_figures_and_the_state_follow_the_frequency(self):
        self.set_frequency('quarterly')
        have = bg.versions(self.bud())
        t = bg.editor_state(have['draft'], have)['totals']
        self.assertEqual(t['periods'], 4)
        self.assertAlmostEqual(t['unit_period'], t['unit_year'] / 4, places=4)
        self.assertIn('Per unit per quarter', self.client.get(self.page).content.decode())

    def test_the_notice_wording_is_quarterly_too(self):
        self.client.post(self.page, {'action': 'advance'})
        self.set_frequency('quarterly', stage='board')
        t = bp.proposed_totals(self.prop, bg.versions(self.bud())['board'])
        text = bp.assessment_summary(self.prop, 2027, t, self.bud())
        self.assertIn('a quarter', text)
        self.assertNotIn('a month', text)
        self.assertIn('a month', bp.assessment_summary(self.prop, 2027, t))                # without a budget it is the old wording

    def test_the_pdf_and_excel_say_quarterly(self):
        self.set_frequency('quarterly')
        sheet = self.sheet()
        sheet.update({'assoc': 'Frequency Condos', 'stage_label': 'Management draft', 'on': date(2026, 11, 17)})
        from pypdf import PdfReader
        text = ' '.join(p.extract_text() for p in PdfReader(io.BytesIO(budget_export.pdf(sheet))).pages)
        self.assertIn('Quarterly assessment', text)
        self.assertNotIn('Monthly assessment', text)
        from openpyxl import load_workbook
        ws = load_workbook(io.BytesIO(budget_export.xlsx(sheet))).active
        cells = [c.value for row in ws.iter_rows() for c in row if isinstance(c.value, str)]
        self.assertIn('Quarterly assessment', cells)

    def test_loading_the_budget_makes_a_quarterly_rule_when_the_budget_is_quarterly(self):
        self.client.post(self.page, {'action': 'advance'})
        self.set_frequency('quarterly', stage='board')
        bg.advance(self.bud(), None)                                                       # the meeting version
        bg.advance(self.bud(), None)                                                       # approved
        plan = bp.load_plan(self.prop, self.bud(), bg.versions(self.bud()), today=date(2026, 12, 1))
        self.assertEqual(plan['rule']['frequency'], 'quarterly')
        self.assertEqual(plan['period_noun'], 'quarter')


class ByUnitColumnsLineUpTests(TestCase):
    def test_the_by_unit_table_uses_the_same_column_widths_as_the_budget_above_it(self):
        self.assertEqual(len(budget_export.COLS), 6)
        self.assertFalse(hasattr(budget_export, 'UNIT_COLS'))                              # one set of columns for both tables, so 2026 and 2027 line up

    def test_the_excel_by_unit_rows_have_the_six_columns_with_2027_under_2027(self):
        none = {'prior': 0, 'projected': 0, 'proposed': 0, 'd': 0, 'p': None, 'tone': '', 'ptone': ''}
        sheet = {'year': 2027, 'proj_word': 'projected', 'assoc': 'A', 'stage_label': 'Management draft', 'units': 4, 'cents': False, 'on': date(2026, 11, 17), 'basis_note': '', 'op': none, 'total': none, 'needed': none,
                 'sections': [], 'calc': [{'header': 'All 4 units, the same dues', 'units': '', 'rows': [{'label': 'Monthly assessment', 'old': 100.0, 'new': 120.0, 'd': 20.0, 'p': 20.0, 'tone': '', 'ptone': '', 'bold': True}]}]}
        from openpyxl import load_workbook
        ws = load_workbook(io.BytesIO(budget_export.xlsx(sheet))).active
        row = next(r for r in ws.iter_rows() if r[0].value == 'Monthly assessment')
        self.assertEqual((row[1].value, row[2].value, row[3].value, row[4].value), (100.0, None, 120.0, 20.0))      # 2026 in B, nothing under the projection, 2027 in D, where the budget's 2027 is
        head = next(r for r in ws.iter_rows() if r[0].value == 'By unit calculation')
        self.assertEqual([c.value for c in head[:6]], ['By unit calculation', '2026', None, '2027', '$ change', '% change'])

    def test_the_pdf_by_unit_columns_end_where_the_budget_columns_above_them_end(self):
        """Measured from the PDF itself: the right edge of 2026 under 2026 budget, 2027 under 2027 budget, and the two change columns, to the tenth of a point."""
        from pypdf import PdfReader
        from reportlab.pdfbase.pdfmetrics import stringWidth
        none = {'prior': 1000.0, 'projected': 1000.0, 'proposed': 1200.0, 'd': 200.0, 'p': 20.0, 'tone': '', 'ptone': ''}
        sheet = {'year': 2027, 'proj_word': 'projected', 'assoc': 'A', 'stage_label': 'Management draft', 'units': 4, 'cents': False, 'on': date(2026, 11, 17), 'basis_note': '', 'op': none, 'total': none, 'needed': none,
                 'sections': [], 'calc': [{'header': 'All 4 units, the same dues', 'units': '', 'rows': [{'label': 'Monthly assessment', 'old': 100.0, 'new': 120.0, 'd': 20.0, 'p': 20.0, 'tone': '', 'ptone': '', 'bold': True}]}]}
        found = []

        def visit(text, cm, tm, font_dict, font_size):
            if text.strip():
                font = 'Helvetica-Bold' if 'Bold' in str((font_dict or {}).get('/BaseFont', '')) else 'Helvetica'
                found.append((text.strip(), round(tm[4] + cm[4] + stringWidth(text.strip(), font, font_size), 1), round(tm[5] + cm[5], 1)))
        PdfReader(io.BytesIO(budget_export.pdf(sheet))).pages[0].extract_text(visitor_text=visit)
        edge = lambda word, below=None: [e for t, e, y in found if t == word and (below is None or y < below)]
        main = {w: edge(w)[0] for w in ('2026 budget', '2027 budget')}
        by_unit_y = next(y for t, e, y in found if t == 'By unit calculation')
        self.assertEqual(edge('2026', by_unit_y + 1)[0], main['2026 budget'])
        self.assertEqual(edge('2027', by_unit_y + 1)[0], main['2027 budget'])
        change = sorted(set(e for t, e, y in found if t == '$ change'))
        self.assertEqual(len(change), 1)                                                      # the same right edge in both tables
