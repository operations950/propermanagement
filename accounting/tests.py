import json
from datetime import date
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import Property, Unit

from . import budgeting as bg
from . import ledger
from .models import Budget, BudgetLine, BudgetVersion, MonthLock


def make_assoc(name='Test Condos'):
    prop = Property.objects.create(name=name, property_type=Property.Type.ASSOCIATION)
    prof = ledger.profile_for(prop)
    prof.we_do_financials = True
    prof.save()
    ledger.ensure_chart(prop)
    for i in range(4):
        Unit.objects.create(property=prop, label=f'10{i}')
    return prop


def spend(prop, day, code, amount):
    ledger.post_entry(prop, day, [{'account': ledger.account(prop, code), 'debit': D(amount), 'credit': D(0)},
                                  {'account': ledger.account(prop, '1110'), 'debit': D(0), 'credit': D(amount)}], memo='test')


def close(prop, months):
    for m in months:
        ledger.lock_month(prop, date(2026, m, 1))


class BaseInfoTests(TestCase):
    def setUp(self):
        self.prop = make_assoc()

    def test_counts_closed_months_in_a_row_and_stops_at_a_gap(self):
        close(self.prop, [1, 2, 3, 5])
        info = bg.base_info(self.prop, 2027)
        self.assertEqual(info['n'], 3)
        self.assertEqual(info['through'], date(2026, 3, 1))
        self.assertEqual(info['remaining'], 9)
        self.assertEqual(info['rest_label'], 'Apr–Dec')

    def test_nothing_closed(self):
        info = bg.base_info(self.prop, 2027)
        self.assertEqual(info['n'], 0)
        self.assertFalse(bg.can_start(info))

    def test_fully_closed_year_has_nothing_to_project(self):
        close(self.prop, range(1, 13))
        info = bg.base_info(self.prop, 2027)
        self.assertEqual((info['n'], info['remaining']), (12, 0))

    def test_two_months_left_is_labelled_nov_dec(self):
        close(self.prop, range(1, 11))
        self.assertEqual(bg.base_info(self.prop, 2027)['rest_label'], 'Nov + Dec')


class BuilderTests(TestCase):
    def setUp(self):
        self.prop = make_assoc()
        # last year's budget (2026): pest control 12,000 a year
        bud = Budget.objects.create(association=self.prop, year=2026)
        BudgetLine.objects.create(budget=bud, account=ledger.account(self.prop, '6906'), annual_amount=D('12000'))
        BudgetLine.objects.create(budget=bud, account=ledger.account(self.prop, '3834'), annual_amount=D('2400'))
        for m in range(1, 7):
            spend(self.prop, date(2026, m, 10), '6906', '600')      # 600 a month, closed
        spend(self.prop, date(2026, 7, 10), '6906', '9999')          # July is not closed: must never be counted
        spend(self.prop, date(2026, 3, 5), '6898', '300')            # general maintenance, not budgeted last year
        close(self.prop, range(1, 7))

    def test_cannot_start_without_a_closed_month(self):
        other = make_assoc('Other')
        with self.assertRaises(ledger.LedgerError):
            bg.start_draft(other, 2027, None)

    def test_draft_uses_closed_months_only(self):
        ver = bg.start_draft(self.prop, 2027, None)
        pest = ver.lines.get(account__code='6906')
        self.assertEqual(pest.actual, D('3600.00'))           # six closed months, not July's 9,999
        self.assertEqual(pest.prior_budget, D('12000.00'))
        gm = ver.lines.get(account__code='6898')
        self.assertEqual(gm.prior_budget, D('0.00'))
        info = bg.info_for(ver)
        self.assertEqual(info['remaining'], 6)
        # average 600 x 6 left = 3,600 more, so 7,200 projected; the default method for an expense is the average
        self.assertEqual(bg.compute(pest, info)['proposed'], D('7200.00'))
        # reserve line: budget / 12 x months left
        res = ver.lines.get(account__code='3834')
        self.assertEqual(bg.compute(res, info)['proposed'], D('1200.00'))

    def test_methods(self):
        ver = bg.start_draft(self.prop, 2027, None)
        info = bg.info_for(ver)
        pest = ver.lines.get(account__code='6906')
        self.assertEqual(bg.estimate(pest, 'rest', info), D('8400.00'))      # 12,000 - 3,600
        self.assertEqual(bg.estimate(pest, 'b12', info), D('6000.00'))       # 12,000 / 12 x 6
        self.assertEqual(bg.estimate(pest, 'none', info), D('0.00'))
        pest.custom = D('123')
        self.assertEqual(bg.estimate(pest, 'custom', info), D('123.00'))

    def test_adjustment_dollars_and_percent(self):
        ver = bg.start_draft(self.prop, 2027, None)
        info = bg.info_for(ver)
        pest = ver.lines.get(account__code='6906')
        pest.adjust = '500'
        self.assertEqual(bg.compute(pest, info)['proposed'], D('7700.00'))
        pest.adjust = '-10%'
        self.assertEqual(bg.compute(pest, info)['proposed'], D('6480.00'))
        with self.assertRaises(ValueError):
            bg.parse_adjust('lots')

    def test_stages_freeze_and_approval_writes_the_budget(self):
        draft = bg.start_draft(self.prop, 2027, None)
        bud = draft.budget
        board = bg.advance(bud, None)
        self.assertEqual(board.stage, 'board')
        have = bg.versions(bud)
        self.assertFalse(bg.is_editable(have['draft'], have))
        self.assertTrue(bg.is_editable(have['board'], have))
        bg.advance(bud, None)
        approved = bg.advance(bud, None)
        self.assertEqual(approved.stage, 'approved')
        pest_line = BudgetLine.objects.get(budget=bud, account__code='6906')
        self.assertEqual(pest_line.annual_amount, D('7200.00'))
        with self.assertRaises(ledger.LedgerError):
            bg.advance(bud, None)
        bg.reopen(bud)
        self.assertFalse(BudgetLine.objects.filter(budget=bud).exists())
        self.assertEqual(set(bg.versions(bud)), {'draft', 'board', 'meeting'})


@override_settings(STORAGES={'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                             'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class BudgetScreenTests(TestCase):
    def setUp(self):
        self.prop = make_assoc()
        for m in range(1, 7):
            spend(self.prop, date(2026, m, 10), '6906', '600')
        close(self.prop, range(1, 7))
        User = get_user_model()
        self.admin = User.objects.create_superuser('boss', 'b@example.com', 'pw12345678')
        self.client.force_login(self.admin)

    def test_list_defaults_to_next_year_from_july(self):
        from .views_budget import default_year
        self.assertEqual(default_year(date(2026, 6, 30)), 2026)
        self.assertEqual(default_year(date(2026, 7, 1)), 2027)

    def test_list_and_year_toggle(self):
        r = self.client.get(reverse('accounting:budgets') + '?year=2027')
        self.assertContains(r, 'Test Condos')
        self.assertContains(r, 'No budget in the system for 2027')
        r = self.client.get(reverse('accounting:budgets') + '?year=2026')
        self.assertContains(r, 'No budget in the system for 2026')

    def test_start_edit_advance_compare_approve(self):
        url = reverse('accounting:budget_year', args=[self.prop.pk, 2027])
        self.assertContains(self.client.get(url), 'Start the 2027 budget')
        self.client.post(url, {'action': 'start'})
        r = self.client.get(url)
        self.assertContains(r, 'bd-state')
        ver = BudgetVersion.objects.get(budget__association=self.prop, stage='draft')
        line = ver.lines.get(account__code='6906')
        edit = reverse('accounting:budget_edit', args=[self.prop.pk, 2027]) + '?stage=draft'
        r = self.client.post(edit, json.dumps({'op': 'set', 'line': line.pk, 'field': 'adjust', 'value': '1000'}), content_type='application/json')
        self.assertEqual(r.status_code, 200)
        row = next(x for x in r.json()['rows'] if x['id'] == line.pk)
        self.assertEqual(row['proposed'], 8200.0)
        bad = self.client.post(edit, json.dumps({'op': 'set', 'line': line.pk, 'field': 'adjust', 'value': 'abc'}), content_type='application/json')
        self.assertEqual(bad.status_code, 400)
        r = self.client.post(edit, json.dumps({'op': 'set', 'line': line.pk, 'field': 'method', 'value': 'none'}), content_type='application/json')
        self.assertEqual(next(x for x in r.json()['rows'] if x['id'] == line.pk)['proposed'], 4600.0)
        # add a line, then remove it again
        from .models import Account
        extra = Account.objects.get(association=self.prop, code='3815')
        r = self.client.post(edit, json.dumps({'op': 'add', 'account': extra.pk}), content_type='application/json')
        new_id = next(x for x in r.json()['rows'] if x['code'] == '3815')['id']
        r = self.client.post(edit, json.dumps({'op': 'remove', 'line': new_id}), content_type='application/json')
        self.assertFalse(any(x['code'] == '3815' for x in r.json()['rows']))
        # to the board; the draft is now frozen
        self.client.post(url, {'action': 'advance'})
        r = self.client.post(edit, json.dumps({'op': 'set', 'line': line.pk, 'field': 'note', 'value': 'x'}), content_type='application/json')
        self.assertEqual(r.status_code, 403)
        for stage in ('draft', 'board', 'compare'):
            self.assertEqual(self.client.get(url + f'?stage={stage}').status_code, 200)
        self.client.post(url, {'action': 'advance'})
        self.client.post(url, {'action': 'advance'})
        r = self.client.get(url + '?stage=approved')
        self.assertContains(r, 'Total budget (assessments)')
        self.assertContains(r, 'Monthly assessment')
        self.assertNotContains(r, 'class="bd-i')     # a clean sheet: nothing to type in
        r = self.client.get(reverse('accounting:budgets') + '?year=2027')
        self.assertContains(r, 'Complete')

    def test_start_needs_a_closed_month(self):
        MonthLock.objects.all().delete()
        url = reverse('accounting:budget_year', args=[self.prop.pk, 2027])
        self.assertContains(self.client.get(url), 'Close at least one month')
        self.client.post(url, {'action': 'start'})
        self.assertFalse(BudgetVersion.objects.exists())

    def test_staff_cannot_open_it(self):
        self.client.logout()
        User = get_user_model()
        self.client.force_login(User.objects.create_user('staffer', password='pw12345678'))
        r = self.client.get(reverse('accounting:budgets'))
        self.assertNotEqual(r.status_code, 200)
