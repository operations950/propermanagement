import io
import json
import unittest.mock as mock
from datetime import date
from decimal import Decimal as D

import openpyxl
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from . import budgeting as bg
from . import views_budget as vb
from .models import Budget, BudgetLine
from .tests import close, make_assoc, spend

STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}


def post_json(client, url, body):
    return client.post(url, json.dumps(body), content_type='application/json')


@override_settings(STORAGES=STORAGES)
class BoardSheetAddsUpTests(TestCase):
    """The sheet's figures are rounded lines or sums of the lines shown, so a total never disagrees with what is above it, and the total income equals the total expenses with reserves."""

    ROWS = [
        {'section': 'Income', 'name': 'Late Fee', 'prior': 500.0, 'projected': 520.4, 'proposed': 500.5},
        {'section': 'Income', 'name': 'Tiny One', 'prior': 0.0, 'projected': 0.24, 'proposed': 0.24},               # a line of $1 or less: left off the sheet in whole dollars
        {'section': 'Income', 'name': 'Tiny Two', 'prior': 0.0, 'projected': -0.4, 'proposed': -0.4},
        {'section': 'Income', 'name': 'Tiny Three', 'prior': 0.0, 'projected': -0.49, 'proposed': -0.49},
        {'section': 'Administrative', 'name': 'Insurance', 'prior': 1000.4, 'projected': 900.4, 'proposed': 1000.4},
        {'section': 'Operating', 'name': 'Pest Control', 'prior': 2000.4, 'projected': 1900.4, 'proposed': 2000.4},
        {'section': 'Operating', 'name': 'Pool', 'prior': 300.5, 'projected': 300.5, 'proposed': 300.5},
        {'section': 'Reserves', 'name': 'Roof Reserve', 'prior': 700.4, 'projected': 700.4, 'proposed': 700.4},
    ]

    def sheet(self, cents=False):
        rows = self.ROWS
        exp = sum(r['proposed'] for r in rows if r['section'] != 'Income')
        oi = sum(r['proposed'] for r in rows if r['section'] == 'Income')
        zero = dict(prior=0, projected=0, proposed=0)
        totals = {'sections': {s: dict(zero) for s in bg.SECTIONS}, 'op': dict(zero), 'rs': dict(zero), 'total_prior': 0, 'total_projected': 0, 'total': exp, 'unit_month_prior': 0, 'unit_month': 0,
                  'unit_year': 0, 'oi': dict(zero), 'needed': exp - oi, 'needed_prior': 0, 'needed_projected': 0}
        groups = ([{'label': 'All 4 units, the same dues', 'units': '1, 2, 3, 4', 'count': 4, 'fraction': 0.25}], 'equal')
        with mock.patch.object(bg, 'line_rows', return_value=rows), mock.patch.object(bg, 'totals', return_value=totals), mock.patch.object(bg, 'unit_groups', return_value=groups), \
                mock.patch.object(bg, 'assessment_row', return_value={'prior': 4001.0, 'actual': 0.0, 'nd': 0.0, 'projected': 4000.0, 'proposed': exp - oi}):
            return vb._sheet(mock.Mock(budget=mock.Mock(year=2027)), {'remaining': 2}, 4, cents)

    def check_adds_up(self, sheet):
        for col in ('prior', 'projected', 'proposed'):
            for sec in sheet['sections']:
                self.assertAlmostEqual(sum(l[col] for l in sec['lines']), sec[col], places=6, msg=f'{sec["name"]} {col}')
            by = {s['key']: s for s in sheet['sections']}
            op = sum(by[k][col] for k in ('Administrative', 'Utilities', 'Operating'))
            self.assertAlmostEqual(op, sheet['op'][col], places=6)
            self.assertAlmostEqual(op + by['Reserves'][col], sheet['total'][col], places=6)
        for x in [l for s in sheet['sections'] for l in s['lines']] + sheet['sections'] + [sheet['op'], sheet['total'], sheet['needed']]:
            self.assertAlmostEqual(x['d'], x['proposed'] - x['prior'], places=6)                  # a change is the difference of the two figures beside it
        by = {s['key']: s for s in sheet['sections']}
        self.assertAlmostEqual(by['Income']['proposed'], sheet['total']['proposed'], places=6)           # next year the income is exactly the expenses with reserves

    def test_whole_dollars_every_total_is_the_sum_of_the_lines_shown_and_income_equals_expenses(self):
        sheet = self.sheet()
        self.check_adds_up(sheet)
        inc = sheet['sections'][0]
        self.assertEqual([l['name'] for l in inc['lines']], ['Regular assessments', 'Late Fee'])          # the three small lines are left off
        # exact: expenses 4001.70 (rounds line by line: 1000 + 2000 + 301 + 700), other income 500.50 (shown as 501)
        reg, late = inc['lines']
        self.assertEqual((late['proposed'], sheet['total']['proposed'], reg['proposed']), (501.0, 4001.0, 3500.0))
        self.assertEqual(inc['proposed'], sheet['total']['proposed'])
        self.assertFalse(sheet['cents'])

    def test_the_old_one_dollar_mismatch_is_gone(self):
        """Exact the assessments need is 3,501.20 + the three small lines (-0.65), which the old sheet rounded to a figure one dollar away from the income total beside it."""
        sheet = self.sheet()
        inc = sheet['sections'][0]
        self.assertEqual(inc['lines'][0]['proposed'] + sum(l['proposed'] for l in inc['lines'][1:]), inc['proposed'])
        self.assertEqual(inc['proposed'], sheet['total']['proposed'])

    def test_cents_every_line_with_a_figure_is_shown_and_it_still_adds_up_to_the_cent(self):
        sheet = self.sheet(cents=True)
        self.check_adds_up(sheet)
        inc = sheet['sections'][0]
        self.assertEqual([l['name'] for l in inc['lines']], ['Regular assessments', 'Late Fee', 'Tiny One', 'Tiny Two', 'Tiny Three'])
        self.assertEqual(sheet['total']['proposed'], 4001.7)
        self.assertEqual(round(inc['proposed'], 2), 4001.7)
        self.assertTrue(sheet['cents'])


@override_settings(STORAGES=STORAGES)
class RoundingChoiceScreenTests(TestCase):
    def setUp(self):
        self.prop = make_assoc('Rounding Condos')
        for m in range(1, 7):
            spend(self.prop, date(2026, m, 10), '6906', '600.57')
        close(self.prop, range(1, 7))
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.url = reverse('accounting:budget_year', args=[self.prop.pk, 2027])
        self.client.post(self.url, {'action': 'start'})

    def bud(self):
        return Budget.objects.get(association=self.prop, year=2027)

    def test_whole_dollars_is_the_default_and_the_choice_is_kept_on_the_budget(self):
        self.assertFalse(self.bud().show_cents)
        html = self.client.get(self.url + '?stage=draft&sheet=1').content.decode()
        self.assertIn('$7,207', html)                                      # 600.57 x 12 = 7,206.84, rounded to the dollar
        self.assertNotIn('$7,206.84', html)
        self.assertIn('Whole dollars', html)
        r = self.client.post(self.url, {'action': 'rounding', 'mode': 'cents', 'stage': 'draft', 'sheet': '1'})
        self.assertEqual((r.status_code, r['Location']), (302, self.url + '?stage=draft&sheet=1'))
        self.assertTrue(self.bud().show_cents)
        html = self.client.get(self.url + '?stage=draft&sheet=1').content.decode()
        self.assertIn('$7,206.84', html)
        self.client.post(self.url, {'action': 'rounding', 'mode': 'dollars', 'stage': 'draft'})
        self.assertFalse(self.bud().show_cents)

    def test_the_pdf_and_the_excel_follow_the_choice(self):
        xl = lambda: openpyxl.load_workbook(io.BytesIO(self.client.get(self.url + '?stage=draft&export=xlsx').content)).active
        ws = xl()
        row = next(r for r in range(1, ws.max_row + 1) if ws.cell(row=r, column=1).value == 'Total expenses with reserves')
        self.assertEqual((ws.cell(row=row, column=4).value, '.00' in ws.cell(row=row, column=4).number_format), (7207, False))
        self.client.post(self.url, {'action': 'rounding', 'mode': 'cents', 'stage': 'draft'})
        ws = xl()
        row = next(r for r in range(1, ws.max_row + 1) if ws.cell(row=r, column=1).value == 'Total expenses with reserves')
        self.assertEqual((ws.cell(row=row, column=4).value, '.00' in ws.cell(row=row, column=4).number_format), (7206.84, True))
        pdf = self.client.get(self.url + '?stage=draft&export=pdf')
        self.assertEqual((pdf.status_code, pdf.content[:4]), (200, b'%PDF'))


@override_settings(STORAGES=STORAGES)
class MeetingScreenTests(TestCase):
    """At the meeting: last year, the board draft, the two change columns, one box to type the final number, and the final column that everything adds up from."""

    def setUp(self):
        self.prop = make_assoc('Meeting Condos')
        for m in range(1, 7):
            spend(self.prop, date(2026, m, 10), '6906', '600')
        close(self.prop, range(1, 7))
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.url = reverse('accounting:budget_year', args=[self.prop.pk, 2027])
        self.edit = reverse('accounting:budget_edit', args=[self.prop.pk, 2027])
        self.client.post(self.url, {'action': 'start'})
        draft = bg.versions(self.bud())['draft']
        self.line_id = draft.lines.get(account__code='6906').pk              # 6 months at 600, projected to 7,200
        self.client.post(self.url, {'action': 'advance'})                    # the board draft
        self.client.post(self.url, {'action': 'advance'})                    # the meeting

    def bud(self):
        return Budget.objects.get(association=self.prop, year=2027)

    def meeting_line(self):
        return bg.versions(self.bud())['meeting'].lines.get(account__code='6906')

    def row(self, state):
        return next(r for r in state['rows'] if r['code'] == '6906')

    def set_final(self, value, stage='meeting'):
        line = bg.versions(self.bud())[stage].lines.get(account__code='6906')
        return post_json(self.client, self.edit + f'?stage={stage}', {'op': 'set', 'line': line.pk, 'field': 'final', 'value': value})

    def test_the_meeting_screen_is_the_pared_down_one(self):
        r = self.client.get(self.url + '?stage=meeting')
        self.assertEqual(r.context['view'], 'meeting')
        html = r.content.decode()
        for word in ('Change at this meeting', '2027 Board draft vs 2026 budget', '$ change', '% change', '2027 Final', 'type final number', 'id="mbody"'):
            self.assertIn(word, html)
        for word in ('Adjust<br>', 'Where 2026 will land', 'Your changes', 'click to choose how', 'id="kOp"'):
            self.assertNotIn(word, html)                                   # no projection, no plus or minus, none of the draft screen's clutter
        state = r.context['state_json']
        row = self.row(state)
        self.assertEqual((row['prior'], row['board'], row['typed'], row['proposed']), (0.0, 7200.0, None, 7200.0))

    def test_the_meeting_screen_leaves_lines_with_nothing_off_and_offers_to_add_one(self):
        r = self.client.get(self.url + '?stage=meeting')
        html = r.content.decode()
        for word in ('function visible(l)', 'Add a line…', 'data-add', 'Not in the 2027 budget', 'Lines with nothing in 2027 are left off'):
            self.assertIn(word, html)
        state = r.context['state_json']
        self.assertTrue(state['editable'])
        self.assertTrue(any(state['addable'].values()))                       # accounts with no line yet can be added right here

    def test_a_line_added_at_the_meeting_starts_at_nothing_until_its_number_is_typed(self):
        state = self.client.get(self.url + '?stage=meeting').context['state_json']
        sec, options = next((k, v) for k, v in state['addable'].items() if v)
        account = options[0]['id']
        added = post_json(self.client, self.edit + '?stage=meeting', {'op': 'add', 'account': account})
        self.assertEqual(added.status_code, 200)
        row = next(x for x in added.json()['rows'] if x['code'] == options[0]['label'].split(' ')[0])
        self.assertEqual((row['board'], row['proposed'], row['typed']), (0.0, 0.0, None))      # nothing in it until a number is typed
        line = bg.versions(self.bud())['meeting'].lines.get(pk=row['id'])
        typed = post_json(self.client, self.edit + '?stage=meeting', {'op': 'set', 'line': line.pk, 'field': 'final', 'value': '1,250'})
        row = next(x for x in typed.json()['rows'] if x['id'] == line.pk)
        self.assertEqual((row['board'], row['typed'], row['proposed']), (0.0, 1250.0, 1250.0))
        board = bg.versions(self.bud())['board']
        self.assertFalse(board.lines.filter(account_id=account).exists())                        # the board draft is left exactly as it was mailed

    def test_the_draft_and_board_screens_are_unchanged(self):
        for stage in ('draft', 'board'):
            r = self.client.get(self.url + f'?stage={stage}')
            self.assertEqual(r.context['view'], 'editor')
            self.assertIn('Adjust<br>', r.content.decode())

    def test_typing_a_final_number_is_the_number_not_a_change_and_the_board_draft_stays_visible(self):
        r = self.set_final('$9,000')
        self.assertEqual(r.status_code, 200)
        state = r.json()
        row = self.row(state)
        self.assertEqual((row['board'], row['typed'], row['proposed']), (7200.0, 9000.0, 9000.0))       # the box holds the final number itself
        self.assertEqual(self.meeting_line().proposed, D('9000.00'))
        self.assertEqual(state['totals']['total'], 9000.0)                                              # the totals follow the final column
        board = bg.versions(self.bud())['board'].lines.get(account__code='6906')
        self.assertEqual(board.proposed, D('7200.00'))                                                  # the board draft is kept exactly as it was
        self.assertIsNone(board.meeting_amount)

    def test_the_same_number_as_the_board_draft_is_not_a_change_and_a_blank_box_puts_it_back(self):
        self.set_final('7200')
        self.assertIsNone(self.meeting_line().meeting_amount)
        self.set_final('8000')
        self.assertEqual(self.meeting_line().meeting_amount, D('8000.00'))
        r = self.set_final('')
        self.assertEqual((self.row(r.json())['typed'], self.row(r.json())['proposed']), (None, 7200.0))
        self.assertIsNone(self.meeting_line().meeting_amount)

    def test_a_bad_number_is_refused_and_only_the_meeting_takes_a_final_number(self):
        self.assertEqual(self.set_final('abc').status_code, 400)
        self.assertIsNone(self.meeting_line().meeting_amount)
        line = bg.versions(self.bud())['draft'].lines.get(account__code='6906')
        r = post_json(self.client, self.edit + '?stage=draft', {'op': 'set', 'line': line.pk, 'field': 'final', 'value': '5'})
        self.assertEqual(r.status_code, 403)                               # the draft is frozen once the board draft is made; a final number belongs to the meeting anyway

    def test_the_final_number_flows_to_the_approved_budget_the_sheet_and_the_comparison(self):
        self.set_final('9000')
        self.client.post(self.url, {'action': 'advance'})                  # approve
        bud = self.bud()
        approved = bg.versions(bud)['approved'].lines.get(account__code='6906')
        self.assertEqual((approved.meeting_amount, approved.proposed), (D('9000.00'), D('9000.00')))
        self.assertEqual(BudgetLine.objects.get(budget=bud, account__code='6906').annual_amount, D('9000.00'))
        r = self.client.get(self.url + '?stage=approved')
        html = r.content.decode()
        self.assertIn('$9,000', html)
        self.assertNotContains(r, 'class="bd-i')                            # still a clean read-only sheet
        self.assertEqual(r.context['sheet']['total']['proposed'], 9000.0)
        cmp = self.client.get(self.url + '?stage=compare').context['cmp']
        line = next(l for s in cmp['sections'] for l in s['lines'] if 'Pest' in l['name'])
        self.assertEqual([c['v'] for c in line['cells']], [D('7200.00'), D('7200.00'), D('9000.00')])
        self.assertTrue(line['changed'])

    def test_once_approved_the_meeting_version_is_frozen(self):
        self.client.post(self.url, {'action': 'advance'})
        self.assertEqual(self.set_final('9000').status_code, 403)
        r = self.client.get(self.url + '?stage=meeting')
        self.assertEqual((r.context['view'], r.context['state_json']['editable']), ('meeting', False))
        self.assertIn('frozen', r.content.decode())

    def test_opening_the_approved_budget_again_keeps_what_was_typed(self):
        self.set_final('9000')
        self.client.post(self.url, {'action': 'advance'})
        self.client.post(self.url, {'action': 'reopen'})
        self.assertEqual(self.meeting_line().meeting_amount, D('9000.00'))
        self.assertEqual(self.row(self.client.get(self.url + '?stage=meeting').context['state_json'])['proposed'], 9000.0)
