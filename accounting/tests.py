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


from . import bankrec
from .models import Account, BankAccount, BankLine, BankStatement, JournalEntry, ReconMatch


def bank_for(prop):
    return BankAccount.objects.create(association=prop, name='Operating', bank_name='Truist', last4='4465', account=ledger.account(prop, '1110'))


def cash_entry(prop, day, amount, other_code='4700'):
    """A deposit (positive) or a payment (negative) on the bank account."""
    a = D(abs(amount))
    other = ledger.account(prop, other_code)
    cash = {'account': ledger.account(prop, '1110'), 'debit': a if amount > 0 else D(0), 'credit': D(0) if amount > 0 else a}
    oth = {'account': other, 'debit': D(0) if amount > 0 else a, 'credit': a if amount > 0 else D(0)}
    return ledger.post_entry(prop, day, [cash, oth], memo='test')


MARCH = date(2026, 3, 1)


class ClearSelectedTests(TestCase):
    def setUp(self):
        self.prop = make_assoc()
        self.bank = bank_for(self.prop)
        self.deposit = cash_entry(self.prop, date(2026, 3, 5), 1845)
        self.returned = cash_entry(self.prop, date(2026, 3, 20), -1845)

    def keys(self):
        return [it.key for it in bankrec.ledger_items(self.bank, MARCH)]

    def test_nothing_clears_by_itself(self):
        self.assertEqual(len(self.keys()), 2)
        self.assertEqual(bankrec.auto_match(self.bank, MARCH), 0)
        self.assertEqual(len(self.keys()), 2)

    def test_a_deposit_and_its_returned_item_clear_against_each_other(self):
        m = bankrec.clear_selected(self.bank, MARCH, [], self.keys(), note='returned ACH')
        self.assertEqual((m.how, m.note, m.bank_lines.count(), m.ledger_items.count()), ('cleared by hand', 'returned ACH', 0, 2))
        self.assertEqual(self.keys(), [])
        bankrec.unmatch(m)
        self.assertEqual(len(self.keys()), 2)

    def test_one_alone_needs_an_account_for_the_difference(self):
        key = next(k for k in self.keys() if bankrec._item_from_key(self.bank, MARCH, k).amount > 0)
        with self.assertRaises(bankrec.StatementError):
            bankrec.clear_selected(self.bank, MARCH, [], [key])
        self.assertEqual(len(self.keys()), 2)

    def test_old_difference_is_posted_and_cleared(self):
        old = cash_entry(self.prop, date(2026, 2, 10), -5.81, '6880')       # an old item the bank never had
        key = f'L{old.lines.get(account__code="1110").pk}'
        misc = ledger.account(self.prop, '6880')
        entries_before = JournalEntry.objects.count()
        m = bankrec.clear_selected(self.bank, MARCH, [], [key], account=misc, day=date(2026, 3, 31), memo='Reconciling difference')
        self.assertEqual(m.how, 'difference posted')
        self.assertEqual(JournalEntry.objects.count(), entries_before + 1)
        made = JournalEntry.objects.order_by('-number').first()
        self.assertEqual(made.date, date(2026, 3, 31))
        self.assertEqual(made.memo, 'Reconciling difference')
        self.assertEqual(sum(l.debit - l.credit for l in made.lines.filter(account__code='1110')), D('5.81'))     # cash up by the 5.81 the old item took out
        self.assertNotIn(key, self.keys())

    def test_date_after_the_month_is_refused(self):
        old = cash_entry(self.prop, date(2026, 2, 10), -5.81, '6880')
        key = f'L{old.lines.get(account__code="1110").pk}'
        with self.assertRaises(bankrec.StatementError):
            bankrec.clear_selected(self.bank, MARCH, [], [key], account=ledger.account(self.prop, '6880'), day=date(2026, 4, 2))

    def test_bank_line_and_item_that_agree_are_cleared(self):
        st = BankStatement.objects.create(bank_account=self.bank, period_end=date(2026, 3, 31), end_balance=D('0'))
        bl = BankLine.objects.create(statement=st, date=date(2026, 3, 6), description='DEPOSIT', amount=D('1845'), kind='deposit')
        dep_key = next(k for k in self.keys() if bankrec._item_from_key(self.bank, MARCH, k).amount > 0)
        m = bankrec.clear_selected(self.bank, MARCH, [bl.pk], [dep_key])
        self.assertEqual(m.bank_lines.count(), 1)


@override_settings(STORAGES={'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                             'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class ClearSelectedScreenTests(TestCase):
    def setUp(self):
        self.prop = make_assoc()
        self.bank = bank_for(self.prop)
        cash_entry(self.prop, date(2026, 3, 5), 1845)
        cash_entry(self.prop, date(2026, 3, 20), -1845)
        BankStatement.objects.create(bank_account=self.bank, period_end=date(2026, 3, 31), end_balance=D('0'))
        User = get_user_model()
        self.client.force_login(User.objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.url = reverse('accounting:bank', args=[self.prop.pk, self.bank.pk, '2026-03'])

    def test_page_has_tick_boxes_the_clear_bar_and_the_account_picker(self):
        r = self.client.get(self.url)
        self.assertContains(r, 'name="item"')
        self.assertContains(r, 'id="clear-form"')
        self.assertContains(r, 'ac-pick')
        self.assertContains(r, 'drag the file')

    def test_clearing_from_the_page_then_it_shows_as_matched(self):
        keys = [it.key for it in bankrec.ledger_items(self.bank, MARCH)]
        self.client.post(self.url, {'action': 'clear_selected', 'item': keys, 'note': 'returned ACH'})
        self.assertEqual(ReconMatch.objects.count(), 1)
        r = self.client.get(self.url)
        self.assertContains(r, 'Cleared against each other')
        self.assertContains(r, 'returned ACH')

    def test_difference_without_an_account_is_refused(self):
        keys = [it.key for it in bankrec.ledger_items(self.bank, MARCH)][:1]
        r = self.client.post(self.url, {'action': 'clear_selected', 'item': keys}, follow=True)
        self.assertContains(r, 'apart')
        self.assertEqual(ReconMatch.objects.count(), 0)


from . import reporting


def cash_in(prop, day, amount, income_code='4500'):
    a = D(str(amount))
    return ledger.post_entry(prop, day, [{'account': ledger.account(prop, '1110'), 'debit': a, 'credit': D(0)}, {'account': ledger.account(prop, income_code), 'debit': D(0), 'credit': a}], memo='in')


def cash_out(prop, day, amount, expense_code):
    a = D(str(amount))
    return ledger.post_entry(prop, day, [{'account': ledger.account(prop, expense_code), 'debit': a, 'credit': D(0)}, {'account': ledger.account(prop, '1110'), 'debit': D(0), 'credit': a}], memo='out')


def flat(st):
    """Every printed row label and its values, in order."""
    rows = []
    for b in st['blocks']:
        rows.append(('block', b['title']))
        for g in b['groups']:
            if b['show_group_titles']:
                rows.append(('group', g['title']))
            for l in g['lines']:
                rows.append(('line', l['label'], [v['t'] for v in l['values']]))
            if g['show_total']:
                rows.append(('subtotal', g['total_label'], [v['t'] for v in g['values']]))
        if b['show_total']:
            rows.append(('total', b['total_label'], [v['t'] for v in b['values']]))
    for f in st['finals']:
        rows.append(('final', f['label'], [v['t'] for v in f['values']]))
    return rows


class StatementTests(TestCase):
    def setUp(self):
        self.prop = make_assoc()
        cash_in(self.prop, date(2026, 9, 5), '1000.62')
        cash_in(self.prop, date(2026, 10, 5), '2000.82')
        cash_out(self.prop, date(2026, 10, 9), '300.10', '6820')
        cash_out(self.prop, date(2026, 10, 12), '50.00', '6510')

    def test_balance_sheet_balances_to_the_cent_and_has_totals(self):
        st = reporting.balance_sheet(self.prop, date(2026, 10, 31))
        self.assertTrue(st['balanced'])
        rows = flat(st)
        labels = [r[1] for r in rows]
        self.assertIn('Total liabilities and funds', labels)
        self.assertNotIn('Out of balance', labels)
        final = next(r for r in rows if r[0] == 'final')
        self.assertEqual(final[2][0], '2,651.34')       # cents kept: 1,000.62 + 2,000.82 - 300.10 - 50.00
        self.assertEqual(final[2][1], '1,000.62')       # a month earlier
        self.assertEqual(final[2][2], '1,650.72')       # the change

    def test_a_section_with_one_line_gets_no_total(self):
        st = reporting.balance_sheet(self.prop, date(2026, 10, 31))
        labels = [r[1] for r in flat(st)]
        self.assertIn('Operating Cash', labels)
        self.assertNotIn('Total assets', labels)         # cash is the only asset: the line already says it
        self.assertNotIn('Total cash', labels)

    def test_more_than_one_line_gets_a_total(self):
        ledger.post_entry(self.prop, date(2026, 10, 20), [{'account': ledger.account(self.prop, '1891'), 'debit': D('10'), 'credit': D(0)}, {'account': ledger.account(self.prop, '1110'), 'debit': D(0), 'credit': D('10')}])
        st = reporting.balance_sheet(self.prop, date(2026, 10, 31))
        rows = flat(st)
        self.assertIn('Total assets', [r[1] for r in rows])
        self.assertEqual(next(r for r in rows if r[1] == 'Total assets')[2][0], '2,651.34')

    def test_balance_sheet_months_view(self):
        st = reporting.balance_sheet(self.prop, date(2026, 10, 31), view='months')
        self.assertEqual(len(st['columns']), 10)         # January through October, no change column
        self.assertEqual(st['columns'][-1]['label'], 'Oct 2026')

    def test_profit_and_loss(self):
        st = reporting.profit_and_loss(self.prop, date(2026, 10, 31))
        rows = flat(st)
        by = {r[1]: r for r in rows if len(r) > 2}
        self.assertEqual(by['Fees - Residential'][2], ['2,000.82', '1,000.62', '3,001.44'])        # this month, last month, year to date
        self.assertEqual(by['Total expenses'][2], ['350.10', '–', '350.10'])
        self.assertEqual(by['Net income'][2], ['1,650.72', '1,000.62', '2,651.34'])
        self.assertNotIn('Total income', [r[1] for r in rows])        # one income line: no total of the same number
        self.assertEqual(len(reporting.profit_and_loss(self.prop, date(2026, 10, 31), view='months')['columns']), 11)

    def test_budget_vs_actual(self):
        bud = Budget.objects.create(association=self.prop, year=2026)
        BudgetLine.objects.create(budget=bud, account=ledger.account(self.prop, '6820'), annual_amount=D('3600'))
        BudgetLine.objects.create(budget=bud, account=ledger.account(self.prop, '4500'), annual_amount=D('24000'))
        st = reporting.budget_vs_actual(self.prop, date(2026, 10, 31))
        self.assertTrue(st['has_budget'])
        by = {r[1]: r for r in flat(st) if len(r) > 2}
        mgmt = by['Management Fees'][2]
        self.assertEqual(mgmt[0], '300')               # month actual (whole dollars)
        self.assertEqual(mgmt[1], '300')               # 3,600 / 12
        self.assertEqual(mgmt[4], '3,000')             # year to date budget: 10 months
        self.assertEqual(mgmt[7], '3,600')             # annual
        self.assertEqual(mgmt[8], '3,300')             # remaining

    def test_budget_vs_actual_without_a_budget_says_so(self):
        self.assertFalse(reporting.budget_vs_actual(self.prop, date(2026, 10, 31))['has_budget'])


@override_settings(STORAGES={'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                             'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class ReportScreenTests(TestCase):
    def setUp(self):
        self.prop = make_assoc()
        cash_in(self.prop, date(2026, 10, 5), '2000.82')
        cash_out(self.prop, date(2026, 10, 9), '300.10', '6820')
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))

    def test_every_report_opens(self):
        for name in ('report_bs', 'report_pl', 'report_bva', 'report_tb', 'report_is', 'report_aging', 'report_payables', 'report_checks', 'report_gl', 'reports'):
            r = self.client.get(reverse(f'accounting:{name}', args=[self.prop.pk]) + '?end=2026-10-31')
            self.assertEqual(r.status_code, 200, name)

    def test_views_and_totals_show(self):
        url = reverse('accounting:report_bs', args=[self.prop.pk])
        self.assertContains(self.client.get(url + '?end=2026-10-31&view=year'), 'Total liabilities and funds')
        self.assertContains(self.client.get(url + '?end=2026-10-31&view=months'), 'Oct 2026')
        self.assertContains(self.client.get(reverse('accounting:report_pl', args=[self.prop.pk]) + '?end=2026-10-31'), 'Net income')
        self.assertContains(self.client.get(reverse('accounting:report_bva', args=[self.prop.pk]) + '?end=2026-10-31'), 'No budget is loaded for 2026')


@override_settings(STORAGES={'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                             'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class SettingsCardTests(TestCase):
    def setUp(self):
        self.prop = make_assoc()
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))

    def test_where_things_stand_has_no_financials_row_and_open_goes_to_a_card(self):
        r = self.client.get(reverse('accounting:settings', args=[self.prop.pk]))
        html = r.content.decode()
        card = html[html.index('Where things stand'):html.index('Terms and defaults')]
        self.assertNotIn('We do the financials', card)
        self.assertIn('#terms', card)
        self.assertIn('#banks', card)
        self.assertIn('id="terms"', html)
        self.assertIn('id="banks"', html)


import json as _json
from types import SimpleNamespace
from unittest import mock

from . import docreader, documents
from .models import AssociationDocument, DocumentPage

GOOD = [{'kind': 'declaration', 'title': 'Declaration', 'start_page': 1, 'end_page': 3, 'confidence': 0.9},
        {'kind': 'exhibit', 'title': 'Survey', 'start_page': 4, 'end_page': 5, 'confidence': 0.8},
        {'kind': 'articles', 'title': 'Articles', 'start_page': 6, 'end_page': 7, 'confidence': 0.9},
        {'kind': 'articles', 'title': 'Articles (again)', 'start_page': 8, 'end_page': 9, 'confidence': 0.9, 'repeat_of_page': 6}]


def fake_client(*answers):
    """A stand-in for the Claude client: each call returns the next answer as the `documents` input of the tool call."""
    calls = []

    def create(**kw):
        calls.append(kw)
        block = SimpleNamespace(type='tool_use', name='report_documents', input={'documents': answers[min(len(calls), len(answers)) - 1]})
        return SimpleNamespace(content=[block])
    return SimpleNamespace(messages=SimpleNamespace(create=create)), calls


class SortingTests(TestCase):
    def setUp(self):
        self.prop = make_assoc()
        self.doc = AssociationDocument.objects.create(property=self.prop, name='Governing Docs', file='gov.pdf', auto_sort=True, sort_status='waiting', text_status='ready', page_count=9)
        DocumentPage.objects.bulk_create([DocumentPage(document=self.doc, number=i, text=f'page {i} text') for i in range(1, 10)])

    def sort_with(self, *answers):
        client, calls = fake_client(*answers)
        with mock.patch.object(documents, '_client', return_value=client):
            docreader.sort_document(self.doc)
        self.doc.refresh_from_db()
        return calls

    def test_as_dicts_accepts_every_shape_claude_sends(self):
        one = {'kind': 'bylaws', 'start_page': 1, 'end_page': 2}
        for value in ([one], _json.dumps([one]), '```json\n' + _json.dumps([one]) + '\n```', {'documents': [one]}, {'documents': _json.dumps([one])}, [_json.dumps(one)]):
            self.assertEqual(docreader.as_dicts(value, 'documents'), [one], value)
        for junk in ('', 'no documents here', None, 5, ['x', 3]):
            self.assertEqual(docreader.as_dicts(junk, 'documents'), [])

    def test_an_answer_sent_as_text_is_understood(self):
        calls = self.sort_with(_json.dumps(GOOD))                # this is what crashed in production
        self.assertEqual(self.doc.sort_status, 'proposed')
        self.assertEqual(len(calls), 1)
        kinds = [s['kind'] for s in self.doc.sections]
        self.assertEqual(kinds, ['declaration', 'exhibit', 'articles', 'articles'])
        self.assertEqual(self.doc.sections[3].get('repeat_of'), 6)

    def test_an_empty_answer_is_asked_for_again_once(self):
        calls = self.sort_with('', GOOD)
        self.assertEqual((len(calls), self.doc.sort_status), (2, 'proposed'))

    def test_two_unusable_answers_leave_it_for_a_person(self):
        calls = self.sort_with('nothing', 'still nothing')
        self.assertEqual((len(calls), self.doc.sort_status), (2, 'manual'))

    def test_pages_left_out_become_other(self):
        self.sort_with([{'kind': 'bylaws', 'title': 'Bylaws', 'start_page': 3, 'end_page': 5, 'confidence': 0.9}])
        self.assertEqual([(s['kind'], s['start'], s['end']) for s in self.doc.sections], [('other', 1, 2), ('bylaws', 3, 5), ('other', 6, 9)])

    def test_a_second_copy_and_the_exhibits_are_not_counted_as_the_document(self):
        self.sort_with(GOOD)
        documents.confirm_sections(self.doc, [(s['kind'], s['title']) for s in self.doc.sections])
        src = documents._sources(self.prop)
        self.assertEqual(len(src['articles']), 1)                 # the repeat is left out
        self.assertEqual(len(src['exhibit']), 1)
        rows = {r['kind']: r for r in documents.checklist(self.prop)}
        self.assertEqual(rows['exhibit']['label'], 'Exhibits and surveys')
        self.assertEqual(rows['articles']['count'], 1)
