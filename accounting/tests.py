import json
from datetime import date
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import Property, Unit

from . import budgeting as bg
from . import ledger
from .models import Budget, BudgetLine, BudgetVersion, BudgetVersionLine, MonthLock


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
        self.assertContains(r, 'Total expenses with reserves')
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
        self.assertEqual(rows['articles']['count'], 1)
        self.assertNotIn('exhibit', rows)                          # exhibits hang under the document they belong to, not in a row of their own


SAG_SECTIONS = [
    {'kind': 'declaration', 'title': 'Declaration of Condominium', 'start': 1, 'end': 55, 'confidence': 0.9},
    {'kind': 'amendment', 'title': 'Fourth Amendment to Declaration', 'start': 56, 'end': 59, 'confidence': 0.9, 'applies_to': 'declaration'},
    {'kind': 'exhibit', 'title': 'Surveys for the Fourth Amendment', 'start': 60, 'end': 73, 'confidence': 0.8, 'parent_page': 56},
    {'kind': 'articles', 'title': 'Articles of Incorporation', 'start': 74, 'end': 84, 'confidence': 0.9},
    {'kind': 'articles', 'title': 'Articles of Incorporation (again)', 'start': 85, 'end': 94, 'confidence': 0.8, 'repeat_of': 74},
    {'kind': 'bylaws', 'title': 'Bylaws', 'start': 95, 'end': 122, 'confidence': 0.9},
    {'kind': 'amendment', 'title': 'Second Amendment to Declaration of Condominium', 'start': 123, 'end': 125, 'confidence': 0.7},       # no applies_to: read from the title
    {'kind': 'exhibit', 'title': 'Survey of the property', 'start': 126, 'end': 133, 'confidence': 0.7},                                      # no parent_page: the amendment before it
    {'kind': 'amendment', 'title': 'Fourth Amendment (again)', 'start': 141, 'end': 144, 'confidence': 0.8, 'repeat_of': 56},
    {'kind': 'exhibit', 'title': 'Surveys (again)', 'start': 145, 'end': 183, 'confidence': 0.7, 'repeat_of': 60},
    {'kind': 'amendment', 'title': 'Some amendment', 'start': 184, 'end': 184, 'confidence': 0.6},                                         # cannot be placed
]


class DocumentTreeTests(TestCase):
    def setUp(self):
        self.prop = make_assoc()
        self.doc = AssociationDocument.objects.create(property=self.prop, name='Governing Docs', file='gov.pdf', auto_sort=True, sort_status='confirmed', text_status='ready', page_count=186, sections=SAG_SECTIONS)
        self.rows = {r['kind']: r for r in documents.checklist(self.prop)}

    def test_the_big_documents_are_the_rows(self):
        self.assertEqual([r['kind'] for r in documents.checklist(self.prop)][:6], ['declaration', 'articles', 'bylaws', 'rules', 'contract', 'budget'])
        self.assertTrue(self.rows['declaration']['doc'] and self.rows['articles']['doc'] and self.rows['bylaws']['doc'])
        self.assertFalse(self.rows['rules']['doc'])

    def test_amendments_sit_under_the_document_they_amend_and_exhibits_under_the_amendment(self):
        items = self.rows['declaration']['items']
        self.assertEqual([i['title'] for i in items], ['Fourth Amendment to Declaration', 'Second Amendment to Declaration of Condominium'])
        fourth, second = items
        self.assertEqual([c['role'] for c in fourth['children']], ['exhibit', 'copy'])                   # its surveys, and the second copy of the amendment
        self.assertEqual([c['role'] for c in second['children']], ['exhibit'])                         # placed after the amendment it follows
        copy = next(c for c in fourth['children'] if c['role'] == 'copy')
        self.assertEqual(copy['title'], 'Fourth Amendment (again)')
        surveys = next(c for c in fourth['children'] if c['role'] == 'exhibit')
        self.assertEqual([c['role'] for c in surveys['children']], ['copy'])                           # the copy of the surveys is under the surveys

    def test_a_second_copy_of_a_document_sits_under_that_document(self):
        art = self.rows['articles']
        self.assertEqual([(i['role'], i['title']) for i in art['items']], [('copy', 'Articles of Incorporation (again)')])
        self.assertEqual(art['summary'], '1 second copy')
        self.assertEqual(art['count'], 1)

    def test_summary_counts_everything_under_a_row(self):
        self.assertEqual(self.rows['declaration']['summary'], '2 amendments · 2 exhibits · 2 second copies')

    def test_what_cannot_be_placed_goes_in_a_last_row(self):
        last = documents.checklist(self.prop)[-1]
        self.assertEqual(last['kind'], 'unplaced')
        self.assertEqual([i['title'] for i in last['items']], ['Some amendment'])

    def test_the_file_reads_as_one_line(self):
        f = documents.files(self.prop)[0]
        self.assertEqual(f['summary'], 'Sorted into 3 documents and 8 attachments: see the list above')

    def test_confirming_keeps_which_document_an_amendment_amends(self):
        doc = AssociationDocument.objects.create(property=self.prop, name='Another', file='a.pdf', auto_sort=True, sort_status='proposed', text_status='ready',
                                                 sections=[{'kind': 'amendment', 'title': 'Amendment', 'start': 1, 'end': 2, 'confidence': 0.5}])
        documents.confirm_sections(doc, [('amendment', 'Amendment to the bylaws', 'bylaws')])
        doc.refresh_from_db()
        self.assertEqual(doc.sections[0]['applies_to'], 'bylaws')
        self.assertEqual(documents.checklist(self.prop)[2]['items'][0]['title'], 'Amendment to the bylaws')


@override_settings(STORAGES={'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                             'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class DocumentTreeScreenTests(TestCase):
    def test_the_settings_page_shows_the_tree_with_drop_downs(self):
        prop = make_assoc()
        AssociationDocument.objects.create(property=prop, name='Governing Docs', file='gov.pdf', auto_sort=True, sort_status='confirmed', text_status='ready', page_count=186, sections=SAG_SECTIONS)
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        html = self.client.get(reverse('accounting:settings', args=[prop.pk])).content.decode()
        self.assertIn('2 amendments', html)
        self.assertIn('Second copy', html)
        self.assertIn('<details class="dt-row', html)
        self.assertIn('Sorted into 3 documents', html)
        self.assertNotIn('Surveys (again): pages', html)          # no flat list of every piece


from . import charges, closing, payables, scheduled
from .models import ChargeRule, JournalRule, RecurringBill

RESERVES_2026 = {'3815': '4064', '3816': '2356', '3817': '1322', '3826': '10277', '3818': '2292', '3832': '10048', '3834': '26883'}


def budget_with_reserves(prop, year=2026):
    bud = Budget.objects.create(association=prop, year=year)
    for code, amt in RESERVES_2026.items():
        BudgetLine.objects.create(budget=bud, account=ledger.account(prop, code), annual_amount=D(amt))
    return bud


class ReserveContributionTests(TestCase):
    def setUp(self):
        self.prop = make_assoc()
        self.rule = JournalRule.objects.create(association=self.prop, day_of_month=27)

    def test_split_is_the_budget_divided_by_twelve_and_adds_to_the_transfer(self):
        budget_with_reserves(self.prop)
        sp = scheduled.reserve_split(self.prop, date(2026, 10, 27))
        self.assertEqual(sp['total'], D('4770.17'))                      # 57,242 / 12
        by = {a.code: amt for a, amt in sp['lines']}
        self.assertEqual(by['3834'], D('2240.25'))                       # 26,883 / 12
        self.assertEqual(sum(by.values()), D('4770.17'))

    def test_the_pennies_rounding_leaves_go_on_the_largest_fund(self):
        bud = Budget.objects.create(association=self.prop, year=2026)
        for code in ('3815', '3816', '3817'):
            BudgetLine.objects.create(budget=bud, account=ledger.account(self.prop, code), annual_amount=D('100'))      # 8.33 each; 100 / 12 x 3 = 25.00
        BudgetLine.objects.create(budget=bud, account=ledger.account(self.prop, '3834'), annual_amount=D('200'))      # 16.67
        sp = scheduled.reserve_split(self.prop, date(2026, 10, 27))
        self.assertEqual(sp['total'], D('41.67'))                        # 500 / 12
        self.assertEqual(sum(a for _, a in sp['lines']), sp['total'])

    def test_posting_books_the_split_dated_the_transfer_day(self):
        budget_with_reserves(self.prop)
        entry = scheduled.post_contribution(self.prop, self.rule, date(2026, 10, 1))
        self.assertEqual(entry.date, date(2026, 10, 27))
        debit = entry.lines.get(account__code='6900')
        self.assertEqual(debit.debit, D('4770.17'))
        self.assertEqual(entry.lines.filter(account__code__startswith='38').count(), 7)
        with self.assertRaises(ledger.LedgerError):                         # not twice
            scheduled.post_contribution(self.prop, self.rule, date(2026, 10, 1))

    def test_a_contribution_made_by_hand_counts_as_posted(self):
        budget_with_reserves(self.prop)
        ledger.post_entry(self.prop, date(2026, 10, 27), [{'account': ledger.account(self.prop, '6900'), 'debit': D('4770.17')}, {'account': ledger.account(self.prop, '3834'), 'credit': D('4770.17')}], memo='by hand')
        self.assertEqual(scheduled.status(self.prop, self.rule, date(2026, 10, 1), date(2026, 11, 10))['state'], 'posted')
        self.assertEqual(scheduled.post_due(self.prop, date(2026, 11, 10)), [])        # October is done; November's day has not come

    def test_no_budget_means_nothing_posts(self):
        self.assertEqual(scheduled.status(self.prop, self.rule, date(2026, 10, 1), date(2026, 10, 30))['state'], 'no_budget')
        with self.assertRaises(ledger.LedgerError):
            scheduled.post_contribution(self.prop, self.rule, date(2026, 10, 1))

    def test_the_schedule_posts_on_the_day_and_not_before(self):
        budget_with_reserves(self.prop)
        first = scheduled.post_due(self.prop, date(2026, 10, 26))
        self.assertEqual([e.date for e in first], [date(2026, 9, 27)])             # last month's, which was never posted; this month's day has not come
        made = scheduled.post_due(self.prop, date(2026, 10, 27))
        self.assertEqual([e.date for e in made], [date(2026, 10, 27)])
        self.assertEqual(scheduled.post_due(self.prop, date(2026, 10, 28)), [])      # already there

    def test_reserve_tie_out(self):
        BankAccount.objects.create(association=self.prop, name='Reserve', kind='reserve', account=ledger.account(self.prop, '1120'))
        cash = ledger.post_entry(self.prop, date(2026, 10, 5), [{'account': ledger.account(self.prop, '1120'), 'debit': D('1000')}, {'account': ledger.account(self.prop, '3834'), 'credit': D('1000')}])
        self.assertEqual(scheduled.reserve_tie(self.prop, date(2026, 10, 31))['diff'], D('0.00'))
        ledger.post_entry(self.prop, date(2026, 10, 6), [{'account': ledger.account(self.prop, '1120'), 'debit': D('250')}, {'account': ledger.account(self.prop, '1110'), 'credit': D('250')}])
        tie = scheduled.reserve_tie(self.prop, date(2026, 10, 31))
        self.assertEqual((tie['cash'], tie['funds'], tie['diff']), (D('1250.00'), D('1000.00'), D('250.00')))


class BillsInArrearsTests(TestCase):
    def setUp(self):
        self.prop = make_assoc()
        from core.models import Contact
        self.vendor = Contact.objects.create(name='Proper Realty Co')
        self.rec = RecurringBill.objects.create(association=self.prop, vendor=self.vendor, description='Management fee', account=ledger.account(self.prop, '6820'), amount=D('1400'),
                                                next_date=date(2026, 10, 31), post_on_month_end=True)

    def test_posts_on_the_last_day_dated_that_day_and_moves_to_the_next_month_end(self):
        self.assertEqual(payables.post_recurring(self.prop, date(2026, 10, 30)), [])
        made = payables.post_recurring(self.prop, date(2026, 10, 31))
        self.assertEqual([b.date for b in made], [date(2026, 10, 31)])
        self.rec.refresh_from_db()
        self.assertEqual(self.rec.next_date, date(2026, 11, 30))
        payables.post_recurring(self.prop, date(2026, 11, 30))
        self.rec.refresh_from_db()
        self.assertEqual(self.rec.next_date, date(2026, 12, 31))              # not December 30

    def test_a_normal_bill_keeps_its_day(self):
        self.rec.post_on_month_end, self.rec.next_date = False, date(2026, 10, 9)
        self.rec.save()
        payables.post_recurring(self.prop, date(2026, 10, 9))
        self.rec.refresh_from_db()
        self.assertEqual(self.rec.next_date, date(2026, 11, 9))


class ChargesAheadTests(TestCase):
    def test_charges_post_ten_days_before_they_are_due(self):
        prop = make_assoc()
        rule = ChargeRule.objects.create(association=prop, name='Monthly assessment', frequency='monthly', first_due=date(2026, 12, 1), amount=D('615'), income_account=ledger.account(prop, '4500'))
        self.assertEqual(rule.post_days_ahead, 10)
        posted = []
        with mock.patch.object(charges, 'post_rule', side_effect=lambda r, due, user=None: posted.append(due) or 1):
            charges.post_due(prop, date(2026, 11, 20))
            self.assertEqual(posted, [])                                    # 11 days before: too early
            charges.post_due(prop, date(2026, 11, 21))
            self.assertEqual(posted, [date(2026, 12, 1)])                   # dated the due date, posted early


@override_settings(STORAGES={'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                             'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class RecurringScreenAndCloseSheetTests(TestCase):
    def setUp(self):
        self.prop = make_assoc()
        from core.models import Contact
        vendor = Contact.objects.create(name='FPL')
        self.fpl = RecurringBill.objects.create(association=self.prop, vendor=vendor, description='Electricity', account=ledger.account(self.prop, '6410'), amount=None, next_date=date(2026, 10, 24))
        self.bank = bank_for(self.prop)
        BankAccount.objects.create(association=self.prop, name='Reserve', kind='reserve', account=ledger.account(self.prop, '1120'))
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.url = reverse('accounting:recurring', args=[self.prop.pk])

    def test_three_tabs_open(self):
        for tab in ('bills', 'charges', 'entries'):
            r = self.client.get(self.url + f'?tab={tab}&month=2026-10')
            self.assertEqual(r.status_code, 200, tab)
        html = self.client.get(self.url + '?tab=bills&month=2026-10').content.decode()
        self.assertIn('Charges to owners', html)
        self.assertIn('Journal entries', html)
        self.assertIn('Waiting on the statement', html)                       # the variable bill has no value yet

    def test_scheduling_and_posting_the_reserve_contribution_from_the_screen(self):
        budget_with_reserves(self.prop)
        self.client.post(self.url, {'action': 'jr_add', 'day_of_month': '27'})
        rule = JournalRule.objects.get()
        self.assertEqual(rule.day_of_month, 27)
        r = self.client.post(self.url, {'action': 'jr_post', 'rule': rule.pk, 'month': '2026-10'}, follow=True)
        self.assertContains(r, 'Posted J-')
        self.assertEqual(scheduled.status(self.prop, rule, date(2026, 10, 1), date(2026, 11, 1))['state'], 'posted')

    def test_close_sheet_puts_the_bank_reconciliation_last_and_does_not_block_on_recurring_items(self):
        budget_with_reserves(self.prop)
        JournalRule.objects.create(association=self.prop, day_of_month=27)
        sheet = closing.close_sheet(self.prop, date(2026, 10, 1), today=date(2026, 11, 10))
        keys = [i['key'] for i in sheet['items']]
        self.assertEqual(keys[-1], 'lock')
        self.assertTrue(keys[-2].startswith('bank'))
        by = {i['key']: i for i in sheet['items']}
        self.assertFalse(by['recurring']['blocks'])
        self.assertFalse(by['charges']['blocks'])
        self.assertEqual(by[f'journal{JournalRule.objects.get().pk}']['status'], 'attention')       # due, not posted yet
        self.assertIn('Electricity', by['recurring']['detail'])
        self.assertIn('reserve_tie', by)
        self.assertTrue(by['bank%d' % self.bank.pk]['blocks'])


from core.models import Contact as _Contact
from .models import Charge, Ownership


def owner_of(prop, unit, name='Pat Owner'):
    person = _Contact.objects.create(name=name, contact_type=_Contact.ContactType.OWNER)
    person.units.add(unit)
    person.properties.add(prop)
    Ownership.objects.create(unit=unit, person=person, start_date=date(2020, 1, 1), is_billed=True)
    return person


class LateFeesAndInterestTests(TestCase):
    def setUp(self):
        self.prop = make_assoc()
        self.bank = bank_for(self.prop)
        prof = ledger.profile_for(self.prop)
        prof.late_fee_kind, prof.late_fee_flat, prof.late_fee_percent, prof.late_fee_grace_days, prof.late_fee_confirmed = 'greater', D('25'), D('5'), 10, True
        prof.interest_rate, prof.interest_grace_days, prof.interest_confirmed = D('18'), 0, True
        prof.save()
        self.unit = Unit.objects.filter(property=self.prop).first()
        self.owner = owner_of(self.prop, self.unit)
        self.charge = charges.post_charge(self.prop, self.unit, self.owner, Charge.Kind.ASSESSMENT, 'Monthly assessment', date(2026, 10, 1), D('615'))

    def test_greater_of_25_or_5_percent(self):
        prof = ledger.profile_for(self.prop)

        class C:
            amount = D('615')
        self.assertEqual(charges.late_fee_amount(prof, C), D('30.75'))
        C.amount = D('400')
        self.assertEqual(charges.late_fee_amount(prof, C), D('25.00'))

    def test_the_plain_english_summary(self):
        lines = charges.terms_sentences(ledger.profile_for(self.prop), D('615'))
        self.assertIn('the greater of $25.00 or 5% of the installment', lines[0])
        self.assertIn('10 days after it is due', lines[0])
        self.assertIn('$30.75', lines[0])
        self.assertIn('Interest at 18% a year', lines[1])
        self.assertIn('from the due date', lines[1])
        self.assertIn('interest, then late fees', lines[2])

    def test_late_fee_waits_for_the_grace_days(self):
        self.assertEqual(charges.assess_late_fees(self.prop, date(2026, 10, 11)), [])
        made = charges.assess_late_fees(self.prop, date(2026, 10, 12))
        self.assertEqual([c.amount for c in made], [D('30.75')])
        self.assertEqual(charges.assess_late_fees(self.prop, date(2026, 10, 20)), [])       # once for each installment

    def test_interest_is_simple_daily_and_monthly(self):
        # 30 days late in October (Oct 2 to Oct 31): 615 x 18% / 365 x 30
        made = charges.assess_interest(self.prop, date(2026, 11, 10))
        self.assertEqual(len(made), 1)
        self.assertEqual(made[0].amount, D('9.10'))
        self.assertEqual(made[0].due_date, date(2026, 10, 31))
        self.assertEqual(made[0].kind, 'interest')
        self.assertEqual(charges.assess_interest(self.prop, date(2026, 11, 11)), [])           # not twice
        self.assertEqual(charges.assess_interest(self.prop, date(2026, 10, 20)), [])           # October is not over yet

    def test_a_payment_stops_the_interest(self):
        charges.receive_payment(self.prop, self.unit, self.owner, date(2026, 10, 11), D('615'), bank_account=self.bank)
        made = charges.assess_interest(self.prop, date(2026, 11, 10))
        self.assertEqual([c.amount for c in made], [D('3.03')])                              # Oct 2 to Oct 11: 10 days

    def test_a_payment_goes_to_interest_then_late_fee_then_the_assessment(self):
        charges.assess_late_fees(self.prop, date(2026, 10, 12))
        charges.assess_interest(self.prop, date(2026, 11, 10))
        rc = charges.receive_payment(self.prop, self.unit, self.owner, date(2026, 11, 12), D('40'), bank_account=self.bank)
        paid = {a.charge.kind: a.amount for a in rc.applications.all()}
        self.assertEqual(paid, {'interest': D('9.10'), 'late_fee': D('30.75'), 'assessment': D('0.15')})


from datetime import timedelta as _td

from django.utils import timezone as _tz

from .models import DocumentQuestion, DocumentQuestionRead


def claude_text(answer):
    """A stand-in client whose every reply is `answer` as plain text."""
    calls = []

    def create(**kw):
        calls.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type='text', text=answer)])
    return SimpleNamespace(messages=SimpleNamespace(create=create)), calls


class TopicIndexTests(TestCase):
    def setUp(self):
        self.prop = make_assoc()
        self.doc = AssociationDocument.objects.create(property=self.prop, name='Declaration', file='d.pdf', text_status='ready', page_count=3)
        words = 'assessments and installments not paid when due shall bear interest ' * 3
        for i, text in enumerate(('Cover page of the declaration of condominium and recording stamp information ' * 3, words, 'Pets and animals are limited to two per unit and must be leashed in common areas ' * 3), start=1):
            DocumentPage.objects.create(document=self.doc, number=i, text=text)

    def fake(self, rows):
        calls = []

        def create(**kw):
            calls.append(kw)
            return SimpleNamespace(content=[SimpleNamespace(type='tool_use', name='tag_pages', input={'pages': rows})])
        return SimpleNamespace(messages=SimpleNamespace(create=create)), calls

    def test_every_page_is_tagged_once_and_the_file_marked_indexed(self):
        client, calls = self.fake(_json.dumps([{'page': 1, 'tags': ['cover']}, {'page': 2, 'tags': ['interest on unpaid assessments', 'administrative late fee']}, {'page': 3, 'tags': ['pets', 'animals']}]))
        with mock.patch.object(documents, '_client', return_value=client):
            self.assertEqual(docreader.index_document(self.doc), 3)
            self.assertEqual(docreader.index_document(self.doc), 0)               # already done: no call
        self.assertEqual(len(calls), 1)
        self.doc.refresh_from_db()
        self.assertTrue(self.doc.indexed)
        self.assertEqual(DocumentPage.objects.get(document=self.doc, number=2).tags, 'interest on unpaid assessments; administrative late fee')

    def test_a_page_tagged_with_the_subject_beats_a_page_that_only_mentions_the_words(self):
        pages = list(DocumentPage.objects.filter(document=self.doc).order_by('number'))
        pages[0].text += ' late fees interest'                                    # mentions the words in passing
        pages[1].text = 'assessments and installments not paid when due ' * 3      # and the middle page does not mention them
        pages[2].tags = 'late fee; interest on unpaid assessments'
        numbers = [p.number for p in documents.rank_pages(pages, 'What is the late fee and interest?', 1)]
        self.assertIn(3, numbers)                      # the tagged page is the best one (with the page before it)
        self.assertNotIn(1, numbers)                   # not the page that only mentions the words


class AskTheDocumentsTests(TestCase):
    def setUp(self):
        self.prop = make_assoc()
        self.doc = AssociationDocument.objects.create(property=self.prop, name='Declaration', kind='declaration', file='d.pdf', text_status='ready', page_count=1)
        DocumentPage.objects.create(document=self.doc, number=1, text='Interest accrues from the due date at eighteen percent. The late fee is the greater of $25 or 5%.')

    def ask(self, question, answer='Interest is 18%.', **kw):
        client, calls = claude_text(answer)
        with mock.patch.object(documents, '_client', return_value=client):
            q = documents.ask(self.prop, question, **kw)
        return q, calls

    def test_a_new_question_uses_claude_once_and_a_repeat_is_free(self):
        q, calls = self.ask('What is the interest rate?')
        self.assertEqual((len(calls), q.from_memory, q.asks), (1, False, 1))
        self.assertIn('Declaration', q.sources)
        again, calls2 = self.ask('what is the INTEREST rate')
        self.assertEqual((len(calls2), again.from_memory, again.pk, again.asks), (0, True, q.pk, 2))

    def test_ask_again_refreshes_in_place_and_keeps_the_counts(self):
        q, _ = self.ask('What is the interest rate?')
        DocumentQuestion.objects.filter(pk=q.pk).update(reads=7)
        new, calls = self.ask('What is the interest rate?', answer='Eighteen percent a year.', force=True)
        self.assertEqual((len(calls), new.pk, new.answer, new.reads), (1, q.pk, 'Eighteen percent a year.', 7))

    def test_old_answers_and_changed_documents_are_flagged_for_asking_again(self):
        q, _ = self.ask('What is the interest rate?')
        sig = documents.significant_signature(self.prop)
        self.assertEqual(documents.staleness(q, sig), '')
        self.assertEqual(documents.staleness(q, sig, now=_tz.now() + _td(days=91)), 'age')
        AssociationDocument.objects.create(property=self.prop, name='First amendment', kind='amendment', file='a.pdf', text_status='ready')
        self.assertEqual(documents.staleness(q, documents.significant_signature(self.prop)), 'documents')

    def test_an_exhibit_or_budget_does_not_make_an_answer_stale(self):
        q, _ = self.ask('What is the interest rate?')
        AssociationDocument.objects.create(property=self.prop, name='Survey', kind='exhibit', file='s.pdf', text_status='ready')
        AssociationDocument.objects.create(property=self.prop, name='Budget', kind='budget', file='b.pdf', text_status='ready')
        self.assertEqual(documents.staleness(q, documents.significant_signature(self.prop)), '')

    def test_reading_counts_once_a_day_per_person(self):
        q, _ = self.ask('What is the interest rate?')
        user = get_user_model().objects.create_user('reader', password='pw12345678')
        self.assertTrue(documents.record_read(q, user))
        self.assertFalse(documents.record_read(q, user))
        other = get_user_model().objects.create_user('reader2', password='pw12345678')
        self.assertTrue(documents.record_read(q, other))
        q.refresh_from_db()
        self.assertEqual(q.reads, 2)
        self.assertEqual(DocumentQuestionRead.objects.count(), 2)

    def test_payload_is_ranked_by_how_much_it_is_asked_and_read(self):
        a, _ = self.ask('What is the interest rate?')
        b, _ = self.ask('Can I keep a dog?', answer='Two pets.')
        DocumentQuestion.objects.filter(pk=b.pk).update(reads=9)
        rows = documents.qa_payload(self.prop)
        self.assertEqual([r['q'] for r in rows], ['Can I keep a dog?', 'What is the interest rate?'])
        self.assertEqual(rows[0]['reads'], 9)


@override_settings(STORAGES={'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                             'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class AskScreenTests(TestCase):
    def setUp(self):
        self.prop = make_assoc()
        doc = AssociationDocument.objects.create(property=self.prop, name='Declaration', kind='declaration', file='d.pdf', text_status='ready', page_count=1)
        DocumentPage.objects.create(document=doc, number=1, text='Interest accrues from the due date at eighteen percent.')
        self.q = DocumentQuestion.objects.create(property=self.prop, question='What is the interest rate?', answer='18% a year.', answered_at=_tz.now())
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.url = reverse('accounting:settings', args=[self.prop.pk])

    def test_the_page_carries_the_questions_and_the_late_fee_card(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn('qa-data', html)
        self.assertIn('What is the interest rate?', html)
        self.assertIn('id="late"', html)
        self.assertIn('data-bubble-picker', html)

    def test_reading_is_counted_from_the_page(self):
        r = self.client.post(self.url, {'action': 'q_read', 'ids': [self.q.pk]})
        self.assertEqual(r.json(), {'counted': 1})
        self.assertEqual(self.client.post(self.url, {'action': 'q_read', 'ids': [self.q.pk]}).json(), {'counted': 0})
        self.q.refresh_from_db()
        self.assertEqual(self.q.reads, 1)

    def test_saving_the_late_fee_and_interest_terms(self):
        r = self.client.post(self.url, {'action': 'late_terms', 'late_fee_kind': 'greater', 'late_fee_flat': '25', 'late_fee_percent': '5', 'late_grace': '10', 'interest': '18', 'interest_grace': '0'}, follow=True)
        self.assertContains(r, 'the greater of $25.00 or 5% of the installment')
        prof = ledger.profile_for(self.prop)
        self.assertEqual((prof.late_fee_kind, prof.late_fee_grace_days, prof.interest_rate, prof.interest_grace_days), ('greater', 10, D('18'), 0))
        self.assertTrue(prof.late_fee_is_set() and prof.interest_is_set())
        bad = self.client.post(self.url, {'action': 'late_terms', 'late_fee_kind': 'greater', 'late_fee_flat': '', 'late_fee_percent': '5', 'late_grace': '10', 'interest': ''}, follow=True)
        self.assertContains(bad, 'flat amount')


class IncomeIsNotAnExpenseTests(TestCase):
    """The approved 2026 budget of St Andrews Grand counted its Assessments income line as an operating expense: $710,278, $1,138.27 a unit. Income is worked out, never added up."""
    def setUp(self):
        self.prop = make_assoc()
        for i in range(48):
            Unit.objects.create(property=self.prop, label=f'20{i}')            # 52 units with the four from make_assoc
        bud = Budget.objects.create(association=self.prop, year=2026)
        self.ver = BudgetVersion.objects.create(budget=bud, stage='approved')
        rows = [('4500', '326518'), ('6820', '16800'), ('6882', '200766'), ('6893', '43800'), ('3834', '26883'), ('3815', '30359')]       # reserves 57,242
        for code, amt in rows:
            BudgetVersionLine.objects.create(version=self.ver, account=ledger.account(self.prop, code), method='custom', custom=D(amt), proposed=D(amt))

    def test_the_income_line_is_not_a_line_of_the_budget(self):
        info = bg.info_for(self.ver)
        rows = bg.line_rows(self.ver, info)
        self.assertNotIn('4500', [r['code'] for r in rows])
        t = bg.totals(rows, 52)
        self.assertEqual(t['op']['proposed'], 261366.0)                       # 16,800 + 200,766 + 43,800
        self.assertEqual(t['total'], 261366.0 + 57242.0)
        self.assertEqual(t['income']['total']['proposed'], t['total'])
        self.assertEqual(t['income']['assessments']['proposed'], 261366.0)
        self.assertEqual(t['income']['reserve']['proposed'], 57242.0)

    def test_the_per_unit_amount_comes_from_the_total(self):
        from .views_budget import _sheet
        sheet = _sheet(self.ver, bg.info_for(self.ver), 52)
        self.assertEqual(sheet['total']['proposed'], 261366.0 + 57242.0)
        monthly = sheet['calc'][0]['rows'][0]['new']
        self.assertAlmostEqual(monthly, (261366 + 57242) / bg.unit_count(self.ver.budget.association) / 12, places=4)

    def test_approving_writes_the_income_budget_as_the_total(self):
        bud = self.ver.budget
        ver = bud.versions.get()
        bg.write_budget_lines(bud, ver)
        self.assertEqual(BudgetLine.objects.get(budget=bud, account__code='4500').annual_amount, D('318608.00'))      # expenses + reserves
        self.assertEqual(BudgetLine.objects.get(budget=bud, account__code='6820').annual_amount, D('16800.00'))


from django.core.management import call_command

from . import delinquency
from .models import CaseEvent, DelinquencyCase


def staff_admin(username='mgr'):
    from core.models import StaffProfile
    user = get_user_model().objects.create_user(username, password='pw12345678', first_name='Alexis')
    return StaffProfile.objects.create(user=user, role=StaffProfile.Role.ACCOUNTING, is_company_admin=True)


class DelinquencyBase(TestCase):
    def setUp(self):
        call_command('seed_legal_templates', verbosity=0)
        self.prop = make_assoc()
        self.bank = bank_for(self.prop)
        prof = ledger.profile_for(self.prop)
        prof.late_fee_kind, prof.late_fee_flat, prof.late_fee_percent, prof.late_fee_grace_days, prof.late_fee_confirmed = 'greater', D('25'), D('5'), 10, True
        prof.interest_rate, prof.interest_confirmed = D('18'), True
        prof.save()
        self.staff = staff_admin()
        units = list(Unit.objects.filter(property=self.prop).order_by('label'))
        self.late_unit, self.ok_unit = units[0], units[1]
        self.owner = owner_of(self.prop, self.late_unit, 'Late Owner')
        self.good = owner_of(self.prop, self.ok_unit, 'Good Owner')
        charges.post_charge(self.prop, self.late_unit, self.owner, Charge.Kind.ASSESSMENT, 'Monthly assessment', date(2026, 10, 1), D('615'))
        charges.post_charge(self.prop, self.ok_unit, self.good, Charge.Kind.ASSESSMENT, 'Monthly assessment', date(2026, 10, 1), D('615'))
        charges.receive_payment(self.prop, self.ok_unit, self.good, date(2026, 10, 5), D('615'), bank_account=self.bank)
        self.today = date(2026, 11, 12)


class DelinquentListTests(DelinquencyBase):
    def test_only_owners_over_the_threshold_are_listed(self):
        rows = delinquency.delinquent_rows(self.prop, self.today, D('100'))
        self.assertEqual([r['owner'].name for r in rows], ['Late Owner'])
        self.assertEqual(rows[0]['b']['total'], D('615.00'))
        self.assertEqual(rows[0]['days_late'], 42)
        self.assertTrue(rows[0]['needs_letter'])
        self.assertEqual(delinquency.delinquent_rows(self.prop, self.today, D('700')), [])

    def test_late_fee_and_interest_ready_show_on_the_row(self):
        row = delinquency.delinquent_rows(self.prop, self.today, D('100'))[0]
        self.assertEqual(row['fee_due'], D('30.75'))
        self.assertEqual(row['interest_due'], D('9.10'))            # October's interest, charged in November

    def test_fees_are_charged_only_to_the_units_ticked(self):
        self.assertEqual(charges.assess_late_fees(self.prop, self.today, units=[self.ok_unit.pk]), [])
        made = charges.assess_late_fees(self.prop, self.today, units=[self.late_unit.pk])
        self.assertEqual([c.amount for c in made], [D('30.75')])


class LateLetterOpensTheCaseTests(DelinquencyBase):
    def make_letter(self):
        from legalforms import services
        from legalforms.models import DocumentTemplate
        template = DocumentTemplate.objects.get(slug='nola')
        values = services.initial_values(template, self.prop, self.late_unit, self.staff.user)
        return services.save_document(template, self.prop, self.late_unit, self.staff.user, {k: '' if v is None else str(v) for k, v in values.items()})

    def test_the_letter_is_filled_from_the_books(self):
        from legalforms import services
        from legalforms.models import DocumentTemplate
        template = DocumentTemplate.objects.get(slug='nola')
        values = services.initial_values(template, self.prop, self.late_unit, self.staff.user)
        self.assertEqual(values['owner_name'], 'Late Owner')
        self.assertEqual(values['regular_assessments'], '615.00')
        self.assertEqual(values['late_fee_rate'], '5')
        self.assertEqual(values['interest_rate'], '18')

    def test_making_the_first_letter_opens_a_case_with_a_review_in_30_days(self):
        doc, errors = self.make_letter()
        self.assertFalse(errors)
        case = DelinquencyCase.objects.get()
        self.assertEqual((case.unit, case.owner, case.stage), (self.late_unit, self.owner, 'letter'))
        self.assertEqual(case.next_review, case.opened_on + _td(days=30))
        kinds = list(case.events.values_list('kind', flat=True))
        self.assertIn('letter', kinds)
        self.assertEqual(case.events.get(kind='letter').document_id, doc.pk)
        self.assertEqual(delinquency.case_icons(case)['letters'], 1)

    def test_a_second_letter_goes_on_the_same_case(self):
        self.make_letter()
        self.make_letter()
        self.assertEqual(DelinquencyCase.objects.count(), 1)
        self.assertEqual(DelinquencyCase.objects.get().events.filter(kind='letter').count(), 2)

    def test_the_review_is_a_line_on_the_compliance_screen(self):
        from worksessions.models import Session, SessionLine
        self.make_letter()
        case = DelinquencyCase.objects.get()
        line = case.review_line
        self.assertIsNotNone(line)
        self.assertEqual((line.session.due_at, line.session.template.name, line.state), (case.next_review, 'Delinquency case reviews', 'pending'))
        self.assertEqual(Session.objects.count(), 1)

    def test_reviewing_moves_it_on_30_days_and_makes_the_next_line(self):
        self.make_letter()
        case = DelinquencyCase.objects.get()
        first = case.review_line
        delinquency.review(case, self.staff.user, 'Called, no answer')
        case.refresh_from_db()
        first.refresh_from_db()
        self.assertEqual(first.state, 'done')
        self.assertNotEqual(case.review_line_id, first.pk)
        self.assertEqual(case.review_line.state, 'pending')
        self.assertTrue(case.events.filter(kind='review', summary='Called, no answer').exists())

    def test_ticking_the_line_on_the_compliance_screen_counts_as_a_review(self):
        self.make_letter()
        case = DelinquencyCase.objects.get()
        line = case.review_line
        line.state = 'done'
        line.save()
        self.assertEqual(delinquency.sync_reviews(self.today), 1)
        case.refresh_from_db()
        self.assertEqual(case.review_line.state, 'pending')
        self.assertTrue(case.events.filter(summary='Reviewed from the compliance screen.').exists())

    def test_the_attorney_step_shows_the_scales_and_resolving_ends_the_reminders(self):
        self.make_letter()
        case = DelinquencyCase.objects.get()
        delinquency.set_stage(case, 'attorney', self.staff.user, attorney='Smith & Jones')
        case.refresh_from_db()
        self.assertTrue(delinquency.case_icons(case)['attorney'])
        self.assertEqual(case.attorney_name, 'Smith & Jones')
        delinquency.resolve(case, self.staff.user, 'Paid in full')
        case.refresh_from_db()
        self.assertEqual((case.stage, case.next_review), ('resolved', None))
        self.assertEqual(case.review_line.state, 'done')
        self.assertIsNone(delinquency.open_case_for(self.prop, self.late_unit))


@override_settings(STORAGES={'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                             'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class DelinquencyScreenTests(DelinquencyBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.url = reverse('accounting:delinquencies', args=[self.prop.pk])

    def test_screen_lists_the_owner_with_the_icons_and_a_needs_a_letter_chip(self):
        with mock.patch('django.utils.timezone.localdate', return_value=self.today):
            html = self.client.get(self.url).content.decode()
        self.assertIn('Late Owner', html)
        self.assertNotIn('Good Owner', html)
        self.assertIn('Needs a letter', html)

    def test_making_letters_from_the_screen_opens_cases(self):
        r = self.client.post(self.url, {'action': 'make_letters', 'unit': [self.late_unit.pk]}, follow=True)
        self.assertContains(r, 'Made 1 late letter')
        self.assertEqual(DelinquencyCase.objects.count(), 1)
        case = DelinquencyCase.objects.get()
        page = self.client.get(reverse('accounting:case', args=[self.prop.pk, case.pk]))
        self.assertContains(page, 'Late Owner')
        self.assertContains(page, 'Mark reviewed')

    def test_case_actions(self):
        self.client.post(self.url, {'action': 'make_letters', 'unit': [self.late_unit.pk]})
        case = DelinquencyCase.objects.get()
        url = reverse('accounting:case', args=[self.prop.pk, case.pk])
        self.client.post(url, {'action': 'call', 'note': 'Spoke with the owner', 'date': '2026-11-12'})
        self.client.post(url, {'action': 'stage', 'stage': 'plan', 'note': 'Three payments'})
        case.refresh_from_db()
        self.assertEqual(case.stage, 'plan')
        self.assertTrue(case.events.filter(kind='call').exists())
        self.client.post(url, {'action': 'resolve', 'note': 'Paid'})
        case.refresh_from_db()
        self.assertIsNotNone(case.resolved_on)

    def test_the_close_sheet_has_a_delinquencies_step_that_links_here(self):
        sheet = closing.close_sheet(self.prop, date(2026, 10, 1), today=self.today)
        item = next(i for i in sheet['items'] if i['key'] == 'delinquency')
        self.assertEqual(item['url'], self.url)
        self.assertIn('need a late letter', item['detail'])
        self.assertFalse(item['blocks'])


from . import contracts
from .models import Contract


class ContractAlertTests(TestCase):
    def setUp(self):
        self.prop = make_assoc()
        staff_admin('contractmgr')
        self.c = Contract.objects.create(association=self.prop, name='Landscaping', vendor_name='RCH Lawn Maintenance', start_date=date(2025, 1, 1), end_date=date(2026, 12, 31),
                                         auto_renews=True, renewal_months=12, notice_days=60, amount=D('3500'), frequency='monthly', expense_account=ledger.account(self.prop, '6893'))

    def test_the_dates_that_matter(self):
        today = date(2026, 11, 10)
        self.assertEqual(contracts.current_end(self.c, today), date(2026, 12, 31))
        self.assertEqual(contracts.notice_deadline(self.c, today), date(2026, 11, 1))          # 60 days before the end
        keys = {k.split(':')[0]: d for k, d, _ in contracts.milestones(self.c, today)}
        self.assertEqual(keys['end30'], date(2026, 12, 1))
        self.assertEqual(keys['notice0'], date(2026, 11, 1))

    def test_a_contract_that_renews_by_itself_rolls_into_the_next_term(self):
        self.assertEqual(contracts.current_end(self.c, date(2027, 2, 1)), date(2027, 12, 31))
        self.c.auto_renews = False
        self.assertEqual(contracts.current_end(self.c, date(2027, 2, 1)), date(2026, 12, 31))

    def test_alerts_go_on_the_compliance_screen_two_weeks_ahead_and_once(self):
        from worksessions.models import SessionLine
        n = contracts.ensure_alerts(date(2026, 10, 20))
        # the 90-day warning (10/2), the 30-day-before-notice warning (10/2), the 60-day warning (11/1) and the notice itself (11/1); the 30-day warning (12/1) is too far off
        self.assertEqual(n, 4)
        lines = SessionLine.objects.filter(session__template__name='Contract alerts')
        self.assertEqual(lines.count(), 4)
        self.assertTrue(all('Landscaping' in l.label for l in lines))
        self.assertEqual(contracts.ensure_alerts(date(2026, 10, 21)), 0)           # nothing is made twice
        self.assertEqual(contracts.ensure_alerts(date(2026, 11, 20)), 1)           # the 30-day warning, 12/1, is now within two weeks

    def test_the_recurring_bill_follows_the_contract(self):
        bill = contracts.sync_bill(self.c, date(2026, 12, 1))
        self.assertEqual((bill.amount, bill.frequency, bill.description), (D('3500.00'), 'monthly', 'Landscaping'))
        self.c.amount = D('3650')
        self.c.save()
        contracts.sync_bill(self.c)
        bill.refresh_from_db()
        self.assertEqual(bill.amount, D('3650.00'))
        self.assertEqual(RecurringBill.objects.count(), 1)

    def test_upcoming_flags_a_notice_that_is_close(self):
        rows = contracts.upcoming(self.prop, date(2026, 10, 20))
        self.assertEqual(rows[0]['state'], 'notice')
        self.assertEqual(rows[0]['days'], 12)


@override_settings(STORAGES={'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                             'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class ContractScreenTests(TestCase):
    def setUp(self):
        self.prop = make_assoc()
        staff_admin('contractmgr2')
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.url = reverse('accounting:contracts', args=[self.prop.pk])

    def test_adding_a_contract_with_its_recurring_bill(self):
        acct = ledger.account(self.prop, '6906')
        self.client.post(self.url, {'action': 'save', 'name': 'Pest control', 'vendor_name': '4-Evergone', 'end_date': '2027-06-30', 'notice_days': '30', 'auto_renews': 'on', 'renewal_months': '12',
                                    'amount': '1175', 'frequency': 'quarterly', 'expense_account': acct.pk, 'visible_to_owners': 'on', 'make_bill': 'on', 'next_bill_date': '2026-12-28'})
        c = Contract.objects.get()
        self.assertEqual((c.name, c.amount, c.auto_renews, c.notice_days), ('Pest control', D('1175.00'), True, 30))
        self.assertIsNotNone(c.recurring_bill)
        self.assertEqual(c.recurring_bill.next_date, date(2026, 12, 28))
        page = self.client.get(self.url).content.decode()
        self.assertIn('Pest control', page)
        self.assertIn('4-Evergone', page)

    def test_the_official_records_list_shows_only_what_owners_may_see(self):
        Contract.objects.create(association=self.prop, name='Visible one', end_date=date(2027, 1, 1))
        Contract.objects.create(association=self.prop, name='Hidden one', end_date=date(2027, 1, 1), visible_to_owners=False)
        html = self.client.get(reverse('accounting:contracts_print', args=[self.prop.pk])).content.decode()
        self.assertIn('Visible one', html)
        self.assertNotIn('Hidden one', html)

    def test_reading_a_file_fills_the_form_and_asks_for_a_check(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        found = {'vendor_name': 'Acme', 'name': 'Elevators', 'description': 'Service', 'start_date': date(2026, 1, 1), 'end_date': date(2028, 12, 31), 'auto_renews': True, 'renewal_months': 12,
                 'notice_days': 90, 'amount': D('900'), 'frequency': 'monthly', 'notes': 'quote'}
        with mock.patch.object(contracts, 'read_contract', return_value=found):
            r = self.client.post(self.url, {'action': 'read', 'file': SimpleUploadedFile('c.pdf', b'%PDF-1.4 test')}, follow=True)
        c = Contract.objects.get()
        self.assertFalse(c.confirmed)
        self.assertContains(r, 'Check every field')
        self.assertEqual((c.name, c.notice_days), ('Elevators', 90))


from .models import JournalEntry, PriorYearToDate


@override_settings(STORAGES={'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                             'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class BudgetAfterATakeoverTests(TestCase):
    """We took over the books on Sep 30. January to September came across in one opening entry, and the budget must count them: insurance was $103,333 for the year, not October alone."""
    def setUp(self):
        self.prop = make_assoc('Took Over')
        prof = ledger.profile_for(self.prop)
        prof.books_start = date(2026, 9, 30)
        prof.save()
        a = lambda code: ledger.account(self.prop, code)
        ledger.post_entry(self.prop, date(2026, 9, 30), [
            {'account': a('6882'), 'debit': D('100000')}, {'account': a('6893'), 'debit': D('40000')},
            {'account': a('3834'), 'credit': D('20000')},                    # the fund's balance, not what went in this year
            {'account': a('1110'), 'credit': D('120000')}], memo='Opening balances', basis='accrual', source=JournalEntry.Source.OPENING, source_ref='yardi-sep30', allow_locked=True)
        spend(self.prop, date(2026, 10, 10), '6882', '8000')
        ledger.post_entry(self.prop, date(2026, 10, 20), [{'account': a('6900'), 'debit': D('2000')}, {'account': a('3834'), 'credit': D('2000')}], memo='Reserve contribution', basis='accrual')
        ledger.lock_month(self.prop, date(2026, 10, 1))
        bud = Budget.objects.create(association=self.prop, year=2026)
        BudgetLine.objects.create(budget=bud, account=a('6882'), annual_amount=D('200000'))
        BudgetLine.objects.create(budget=bud, account=a('3834'), annual_amount=D('24000'))
        PriorYearToDate.objects.create(association=self.prop, account=a('3834'), year=2026, amount=D('18000'), note='Yardi Jan-Sep')
        self.admin = get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678')
        self.client.force_login(self.admin)

    def test_the_months_before_our_books_count_as_actual(self):
        info = bg.base_info(self.prop, 2027)
        self.assertEqual((info['n'], info['n_closed'], info['carried'], info['remaining'], info['recent_n']), (10, 1, 9, 2, 1))
        self.assertEqual((info['actual_label'], info['rest_label']), ('Jan–Oct', 'Nov + Dec'))
        ver = bg.start_draft(self.prop, 2027, None)
        ins = ver.lines.get(account__code='6882')
        self.assertEqual(ins.actual, D('108000.00'))                      # the opening year to date plus October
        self.assertEqual(ins.recent, D('8000.00'))                        # the opening entry says nothing about any one month
        info = bg.info_for(ver)
        self.assertEqual(bg.estimate(ins, 'avg', info), D('21600.00'))    # 108,000 / 10 months x 2
        self.assertEqual(bg.estimate(ins, 'recent', info), D('16000.00'))
        res = ver.lines.get(account__code='3834')
        self.assertEqual(res.actual, D('20000.00'))                       # 18,000 before our books + October's 2,000: not the 20,000 balance as well
        self.assertEqual(bg.compute(res, info)['proposed'], D('24000.00'))

    def test_an_old_draft_is_brought_up_without_losing_what_was_typed(self):
        ver = bg.start_draft(self.prop, 2027, None)
        ins = ver.lines.get(account__code='6882')
        ins.method, ins.adjust, ins.note = 'rest', '-3%', 'rebid'
        ins.save()
        # make it look like one made when only the months we closed ourselves counted
        ver.months_actual = 1
        ver.save()
        ins.actual = D('8000')
        ins.save()
        have = bg.versions(ver.budget)
        self.assertEqual(bg.info_for(ver)['actual_label'], 'Oct')
        self.assertTrue(bg.heal(ver, have))
        ver.refresh_from_db()
        ins.refresh_from_db()
        self.assertEqual((ver.months_actual, ins.actual, ins.method, ins.adjust, ins.note), (10, D('108000.00'), 'rest', '-3%', 'rebid'))
        self.assertFalse(bg.is_stale(ver))
        self.assertFalse(bg.heal(ver, have))

    def test_the_board_sheet_and_its_downloads(self):
        url = reverse('accounting:budget_year', args=[self.prop.pk, 2027])
        self.client.post(url, {'action': 'start'})
        r = self.client.get(url)
        self.assertContains(r, 'Actual<br>Jan–Oct')
        self.assertContains(r, 'Board sheet')
        r = self.client.get(url + '?stage=draft&sheet=1')
        self.assertContains(r, '2026 projected')
        self.assertContains(r, 'Download PDF')
        self.assertNotContains(r, 'class="bd-i')
        pdf = self.client.get(url + '?stage=draft&export=pdf')
        self.assertEqual(pdf['Content-Type'], 'application/pdf')
        self.assertTrue(pdf.content.startswith(b'%PDF'))
        self.assertIn('attachment', pdf['Content-Disposition'])
        xl = self.client.get(url + '?stage=draft&export=xlsx')
        self.assertTrue(xl.content.startswith(b'PK'))
        import io
        import openpyxl
        ws = openpyxl.load_workbook(io.BytesIO(xl.content)).active
        texts = [c.value for row in ws.iter_rows() for c in row if c.value]
        self.assertIn('Total expenses with reserves', texts)
        self.assertIn('2026 projected', texts)

    def test_bring_the_actuals_up_to_date_is_offered_when_a_month_closes(self):
        url = reverse('accounting:budget_year', args=[self.prop.pk, 2027])
        self.client.post(url, {'action': 'start'})
        self.assertNotContains(self.client.get(url), 'The books have moved since this version was made')
        spend(self.prop, date(2026, 11, 5), '6882', '1000')
        ledger.lock_month(self.prop, date(2026, 11, 1))
        self.assertContains(self.client.get(url), 'The books have moved since this version was made')
        self.client.post(url, {'action': 'refresh', 'stage': 'draft'})
        ver = BudgetVersion.objects.get(budget__association=self.prop, stage='draft')
        self.assertEqual((ver.months_actual, ver.lines.get(account__code='6882').actual), (11, D('109000.00')))
        self.assertNotContains(self.client.get(url), 'The books have moved since this version was made')

    def test_the_change_is_against_last_years_approved_budget_not_its_projection(self):
        from .views_budget import _sheet
        ver = bg.start_draft(self.prop, 2027, None)
        sheet = _sheet(ver, bg.info_for(ver), 4)
        ins = next(l for sec in sheet['sections'] for l in sec['lines'] if l['name'].startswith('Insurance'))
        self.assertEqual((ins['prior'], ins['projected'], ins['proposed']), (200000.0, 129600.0, 129600.0))
        self.assertEqual(ins['d'], 129600.0 - 200000.0)                 # against the 2026 budget
        self.assertAlmostEqual(ins['p'], (129600 / 200000 - 1) * 100)
        self.assertEqual(sheet['total']['d'], sheet['total']['proposed'] - sheet['total']['prior'])
        url = reverse('accounting:budget_year', args=[self.prop.pk, 2027])
        self.client.post(url, {'action': 'start'})
        self.assertContains(self.client.get(url), 'vs 2026<br>budget')
        self.assertNotContains(self.client.get(url + '?stage=draft&sheet=1'), 'against the 2026 budget')                # the sheet carries no footnote

    def test_drilling_into_an_actual_shows_the_carried_figure_and_every_entry_and_ties_out(self):
        url = reverse('accounting:budget_year', args=[self.prop.pk, 2027])
        self.client.post(url, {'action': 'start'})
        ver = BudgetVersion.objects.get(budget__association=self.prop, stage='draft')
        detail = reverse('accounting:budget_detail', args=[self.prop.pk, 2027]) + '?stage=draft&line='
        ins = ver.lines.get(account__code='6882')
        d = self.client.get(detail + str(ins.pk)).json()
        self.assertEqual(d['carried'], 100000.0)
        self.assertEqual(d['carried_label'], 'Jan–Sep, carried from the old system')
        self.assertEqual([(r['date'], r['amount']) for r in d['rows']], [('2026-10-10', 8000.0)])
        self.assertEqual((d['total'], d['actual'], d['matches']), (108000.0, 108000.0, True))
        res = self.client.get(detail + str(ver.lines.get(account__code='3834').pk)).json()
        self.assertEqual(res['carried'], 18000.0)                       # the contributions before our books, not the fund's balance
        self.assertEqual([r['amount'] for r in res['rows']], [2000.0])
        self.assertEqual((res['total'], res['matches']), (20000.0, True))
        self.assertContains(self.client.get(url), 'bdDetail(')
        self.assertEqual(self.client.get(detail + '999999').status_code, 404)

    def test_the_old_systems_lines_replace_the_carried_figure_in_the_drill_down_and_still_tie_out(self):
        from .models import LedgerDetail
        ins = ledger.account(self.prop, '6882')
        mk = lambda book, day, who, remarks, ctrl, debit, credit='0': LedgerDetail.objects.create(
            association=self.prop, account=ins, book=book, date=day, period=day.replace(day=1), who=who, remarks=remarks, ctrl=ctrl, reference='Check 12', debit=D(debit), credit=D(credit))
        mk('cash', date(2026, 2, 3), 'Cash only Co', 'cash line', 'K-1', '5')
        mk('accrual', date(2026, 2, 3), 'Citizens', 'Windstorm premium', 'K-1529', '60000')
        mk('accrual', date(2026, 5, 9), 'Citizens', 'Flood premium', 'K-1700', '39000')
        mk('accrual', date(2026, 5, 9), 'Citizens', 'Refund', 'K-1701', '0', '1000')
        url = reverse('accounting:budget_year', args=[self.prop.pk, 2027])
        self.client.post(url, {'action': 'start'})
        ver = BudgetVersion.objects.get(budget__association=self.prop, stage='draft')
        detail = reverse('accounting:budget_detail', args=[self.prop.pk, 2027]) + '?stage=draft&line='
        d = self.client.get(detail + str(ver.lines.get(account__code='6882').pk)).json()
        self.assertEqual(d['old_book'], 'accrual')                                  # the accrual lines win once they are loaded
        self.assertEqual([(r['date'], r['amount'], r['ref']) for r in d['rows'] if r.get('old')], [('2026-02-03', 60000.0, 'K-1529'), ('2026-05-09', 39000.0, 'K-1700'), ('2026-05-09', -1000.0, 'K-1701')])
        self.assertEqual(d['rows'][0]['what'], 'Windstorm premium')
        self.assertEqual((d['carried'], d['old_total']), (2000.0, 98000.0))          # what the lines do not explain stays as one row
        self.assertIn('Not itemized', d['carried_label'])
        self.assertEqual((d['total'], d['actual'], d['matches']), (108000.0, 108000.0, True))
        self.assertEqual(d['rows'][-1]['date'], '2026-10-10')                       # our own entries follow the old system's lines
        LedgerDetail.objects.filter(book='accrual').delete()
        d = self.client.get(detail + str(ver.lines.get(account__code='6882').pk)).json()
        self.assertEqual((d['old_book'], d['old_total'], d['carried']), ('cash', 5.0, 99995.0))


@override_settings(STORAGES={'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                             'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class ChartOfAccountsTests(TestCase):
    """One chart of accounts for the whole company: every association uses it."""

    def setUp(self):
        self.a = make_assoc('Alpha Condos')
        self.b = make_assoc('Beta Condos')
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.url = reverse('accounting:master_chart')

    def post(self, **data):
        return self.client.post(self.url, data, follow=True)

    def names(self, code):
        return [ledger.account(p, code).name for p in (self.a, self.b)]

    def test_the_master_starts_as_the_standard_chart_and_is_linked_from_financials(self):
        from . import standard_chart as sc
        from .models import MasterAccount
        self.assertEqual(MasterAccount.objects.count(), len(sc.CHART))
        self.assertTrue(MasterAccount.objects.filter(code='3836', name='Reserve Perimeter Fence').exists())
        self.assertContains(self.client.get(reverse('ws_financials')), self.url)
        self.assertContains(self.client.get(self.url), 'Add an account')

    def test_there_is_no_chart_per_association_any_more(self):
        r = self.client.get(reverse('accounting:chart', args=[self.a.pk]))
        self.assertRedirects(r, self.url)
        self.assertNotContains(self.client.get(reverse('accounting:hub', args=[self.a.pk])), 'Chart of accounts')

    def test_reserve_funds_are_3810_to_3999_and_interest_is_not_a_fund(self):
        from . import standard_chart as sc
        mk = lambda code, kind='equity': Account(code=code, name='x', kind=kind)
        for code in ('3810', '3827', '3836', '3899', '3950', '3999'):
            self.assertTrue(bg.is_reserve(mk(code)), code)
        for code in ('3809', '3813', '3831', '4000'):
            self.assertFalse(bg.is_reserve(mk(code)), code)
        self.assertEqual([sc.section(c) for c in ('3801', '3810', '3999', '4500')], ['Equity', 'Reserve funds', 'Reserve funds', 'Income'])

    def test_an_added_account_appears_in_every_association_and_a_new_association_starts_with_it(self):
        self.assertContains(self.post(action='add', code='6930', name='  Pool   Maintenance ', kind='expense'), '6930 Pool Maintenance is added to every association')
        self.assertEqual(self.names('6930'), ['Pool Maintenance'] * 2)
        spend(self.a, date(2026, 3, 5), '6930', '500')                       # the books can post to it
        self.assertTrue(bg.is_budgetable(ledger.account(self.a, '6930')))
        self.assertContains(self.post(action='add', code='6930', name='Dup', kind='expense'), 'already on the chart')
        self.assertEqual(ledger.account(make_assoc('Gamma Condos'), '6930').name, 'Pool Maintenance')

    def test_bad_numbers_names_and_kinds_are_refused(self):
        for data, words in [({'code': '69', 'name': 'X', 'kind': 'expense'}, 'four digits'), ({'code': '6921', 'name': '', 'kind': 'expense'}, 'name'),
                            ({'code': '6820', 'name': 'Again', 'kind': 'expense'}, 'already'), ({'code': '6922', 'name': 'X', 'kind': 'nonsense'}, 'what kind')]:
            self.assertContains(self.post(action='add', **data), words)
        self.assertFalse(Account.objects.filter(code__in=['69', '6922']).exists())

    def test_a_number_that_does_not_match_its_kind_is_added_with_a_note(self):
        self.assertContains(self.post(action='add', code='1999', name='Odd', kind='expense'), 'usually asset')

    def test_a_rename_reaches_every_association(self):
        from .models import MasterAccount
        self.post(action='rename', account=MasterAccount.objects.get(code='6906').pk, name='Pest and Termite')
        self.assertEqual(self.names('6906'), ['Pest and Termite'] * 2)

    def test_an_unused_account_can_change_number_and_kind_everywhere_but_not_a_used_or_system_one(self):
        from .models import MasterAccount
        m = MasterAccount.objects.get(code='6906')
        self.assertContains(self.post(action='edit', account=m.pk, code='6950'), 'in every association')
        self.assertEqual(MasterAccount.objects.get(pk=m.pk).code, '6950')
        for prop in (self.a, self.b):
            self.assertEqual(ledger.account(prop, '6950').name, 'Pest Control')
            self.assertFalse(Account.objects.filter(association=prop, code='6906').exists())
        self.post(action='edit', account=m.pk, kind='income')
        self.assertEqual(ledger.account(self.b, '6950').kind, 'income')
        self.assertContains(self.post(action='edit', account=m.pk, code='6820'), 'already an account')
        self.assertContains(self.post(action='edit', account=m.pk, code='12'), 'four digits')
        spend(self.b, date(2026, 3, 5), '6898', '100')                       # an entry in one association fixes the number for all
        used = MasterAccount.objects.get(code='6898')
        self.assertContains(self.post(action='edit', account=used.pk, code='6951'), 'has entries in an association')
        self.assertEqual(ledger.account(self.a, '6898').code, '6898')
        mgmt = MasterAccount.objects.get(code='6820')
        self.assertContains(self.post(action='edit', account=mgmt.pk, code='6952'), 'posts to itself')

    def test_number_and_kind_fields_show_only_for_accounts_nothing_is_posted_to(self):
        spend(self.a, date(2026, 3, 5), '6898', '100')
        html = self.client.get(self.url).content.decode()
        self.assertIn('aria-label="Number of Pest Control"', html)
        self.assertNotIn('aria-label="Number of General Maintenance"', html)
        self.assertNotIn('aria-label="Number of Management Fees"', html)

    def test_turning_off_applies_everywhere_and_is_refused_for_system_accounts_and_balances(self):
        from .models import MasterAccount
        self.assertContains(self.post(action='toggle', account=MasterAccount.objects.get(code='6820').pk), 'stays on')
        spend(self.b, date.today(), '6906', '100')
        pest = MasterAccount.objects.get(code='6906')
        self.assertContains(self.post(action='toggle', account=pest.pk), 'still has a balance')
        mist = MasterAccount.objects.get(code='6905')
        self.post(action='toggle', account=mist.pk)
        self.assertEqual([ledger.account(p, '6905').is_active for p in (self.a, self.b)], [False, False])
        self.post(action='toggle', account=mist.pk)
        self.assertTrue(ledger.account(self.a, '6905').is_active)

    def test_an_association_that_drifted_is_brought_back_in_line(self):
        Account.objects.filter(association=self.a, code='6906').update(name='Something else', is_active=False)
        ledger.ensure_chart(self.a)
        acct = ledger.account(self.a, '6906')
        self.assertEqual((acct.name, acct.is_active), ('Pest Control', True))


@override_settings(STORAGES={'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                             'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class BoardSheetSmallLinesTests(TestCase):
    def test_a_line_of_one_dollar_or_less_in_both_budgets_is_left_off_even_with_actuals(self):
        from . import views_budget as vb
        rows = [{'section': 'Operating', 'name': 'Bank Fees', 'prior': 0, 'projected': 312, 'proposed': 0},
                {'section': 'Operating', 'name': 'Dust', 'prior': 1, 'projected': 40, 'proposed': 0.5},
                {'section': 'Operating', 'name': 'Insurance', 'prior': 200766, 'projected': 134064, 'proposed': 120000},
                {'section': 'Operating', 'name': 'New Thing', 'prior': 0, 'projected': 0, 'proposed': 900}]
        totals = {'sections': {s: dict(prior=0, projected=0, proposed=0) for s in bg.SECTIONS}, 'op': dict(prior=0, projected=0, proposed=0), 'rs': dict(prior=0, projected=0, proposed=0),
                  'total_prior': 0, 'total_projected': 0, 'total': 0, 'unit_month_prior': 0, 'unit_month': 0, 'unit_year': 0,
                  'oi': dict(prior=0, projected=0, proposed=0), 'needed': 0, 'needed_prior': 0, 'needed_projected': 0}
        import unittest.mock as mock
        groups = ([{'label': 'All 4 units, the same dues', 'units': '1, 2, 3, 4', 'count': 4, 'fraction': 0.25}], 'equal')
        with mock.patch.object(bg, 'line_rows', return_value=rows), mock.patch.object(bg, 'totals', return_value=totals), mock.patch.object(bg, 'unit_groups', return_value=groups), \
                mock.patch.object(bg, 'assessment_row', return_value={'prior': 0.0, 'actual': 0.0, 'nd': 0.0, 'projected': 0.0, 'proposed': 0.0}):
            sheet = vb._sheet(mock.Mock(budget=mock.Mock(year=2027)), {'remaining': 2}, 52)
        shown = [l['name'] for s in sheet['sections'] for l in s['lines']]
        self.assertEqual(shown, ['Regular assessments', 'Insurance', 'New Thing'])


@override_settings(STORAGES={'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                             'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class OldSystemLedgerLoadTests(TestCase):
    """The Yardi general ledger export becomes read-only detail behind the carried figures."""

    def setUp(self):
        self.prop = make_assoc('Alpha Condos')
        ledger.post_entry(self.prop, date(2026, 9, 30), [{'account': '1110', 'debit': '700'}, {'account': '6882', 'debit': '1000'}, {'account': '4500', 'credit': '1700'}],
                          memo='Opening balance', source=JournalEntry.Source.OPENING, basis='cash')

    def export(self, rows, book='Cash'):
        import tempfile
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(['General Ledger']); ws.append(['Active Properties']); ws.append(['Period = Jan 2026-Sep 2026']); ws.append([f'Book = {book}'])
        ws.append(['Property Name', 'Date', 'Period', 'Person/Description', 'CTRL Number', 'Reference', 'Debit', 'Credit', 'Balance', 'Remarks'])
        for code, name, lines in rows:
            ws.append([code, None, None, name, None, None, None, None, 0, '= Beginning Balance ='])
            for day, who, ctrl, ref, dr, cr, remarks in lines:
                ws.append(['Alpha Condos', day, day.replace(day=1), who, ctrl, ref, dr, cr, 0, remarks])
            ws.append([None, None, None, '  Net Change=0', None, None, None, None, None, '= Ending Balance ='])
            ws.append([None] * 10)
        f = tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False)
        f.close()
        wb.save(f.name)
        return f.name

    def good(self, **kw):
        from datetime import datetime
        d = lambda m, n: datetime(2026, m, n)
        return self.export([('1110', 'Operating Cash', [(d(3, 2), 'Owner', 'R-1', 'Check 5', 1700, 0, 'Assessment'), (d(4, 1), 'Citizens', 'K-2', '88', 0, 1000, 'Insurance')]),
                            ('4500', 'Fees - Residential', [(d(3, 2), 'Owner', 'R-1', 'Check 5', 0, 1700, 'Assessment')]),
                            ('6882', 'Insurance', [(d(4, 1), 'Citizens', 'K-2', '88', 1000, 0, 'Insurance')])], **kw)

    def test_a_file_that_ties_to_what_we_carried_is_stored_as_detail_and_changes_no_balance(self):
        from . import gl_detail
        from .models import JournalLine, LedgerDetail
        gl_detail.ALIASES['Alpha Condos'] = 'Alpha Condos'
        book, period, lines = gl_detail.read(self.good())
        self.assertEqual((book, len(lines)), ('cash', 4))
        res = gl_detail.check(book, lines)
        self.assertEqual((res['unbalanced'], res['missing_accounts'], res['differences']), ([], [], []))
        before = JournalLine.objects.count()
        self.assertEqual(gl_detail.load(book, period, lines), {'Alpha Condos': 4})
        self.assertEqual(JournalLine.objects.count(), before)
        self.assertEqual(LedgerDetail.objects.filter(book='cash').count(), 4)
        gl_detail.load(book, period, lines)                                          # again: replaced, not doubled
        self.assertEqual(LedgerDetail.objects.filter(book='cash').count(), 4)

    def test_a_file_that_does_not_add_up_is_reported_and_the_command_refuses_to_store_it(self):
        import io
        from django.core.management import call_command
        from django.core.management.base import CommandError
        from datetime import datetime
        from . import gl_detail
        from .models import LedgerDetail
        gl_detail.ALIASES['Alpha Condos'] = 'Alpha Condos'
        bad = self.export([('1110', 'Operating Cash', [(datetime(2026, 4, 1), 'Citizens', 'K-2', '88', 0, 900, 'Insurance')]), ('6882', 'Insurance', [(datetime(2026, 4, 1), 'Citizens', 'K-2', '88', 900, 0, 'Insurance')])])
        book, period, lines = gl_detail.read(bad)
        res = gl_detail.check(book, lines)
        self.assertEqual(sorted((d[1], d[3], d[4]) for d in res['differences']), [('4500', 0, -1700), ('6882', 900, 1000)])
        with self.assertRaises(CommandError):
            call_command('load_gl_detail', bad, '--apply', stdout=io.StringIO())
        self.assertFalse(LedgerDetail.objects.exists())
        call_command('load_gl_detail', self.good(), stdout=io.StringIO())             # a dry run keeps nothing
        self.assertFalse(LedgerDetail.objects.exists())
        call_command('load_gl_detail', self.good(), '--apply', stdout=io.StringIO())
        self.assertEqual(LedgerDetail.objects.count(), 4)

    def test_an_unknown_association_or_unlabelled_book_is_refused(self):
        from . import gl_detail
        with self.assertRaises(gl_detail.GLError):
            gl_detail.check('cash', [dict(prop='Nobody Condos', code='1110', ctrl='K-1', debit=D('1'), credit=D('0'))])
        with self.assertRaises(gl_detail.GLError):
            gl_detail.read(self.good(book='Both'))


@override_settings(STORAGES={'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                             'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class BudgetIncomeAndBoardSheetFormatTests(TestCase):
    """Other income is part of the budget: the assessments needed are the expenses and reserves less it. The board sheet shows changes in parentheses, green when good for owners, red when bad,
    and the new year's column is boxed."""

    def setUp(self):
        self.prop = make_assoc('Income Condos')
        prof = ledger.profile_for(self.prop)
        prof.books_start = date(2026, 9, 30)
        prof.save()
        a = lambda code: ledger.account(self.prop, code)
        ledger.post_entry(self.prop, date(2026, 9, 30), [{'account': a('6882'), 'debit': D('100000')}, {'account': a('4800'), 'credit': D('2000')}, {'account': a('1110'), 'credit': D('98000')}],
                          memo='Opening balances', basis='accrual', source=JournalEntry.Source.OPENING, source_ref='yardi-sep30', allow_locked=True)
        spend(self.prop, date(2026, 10, 10), '6882', '8000')
        ledger.post_entry(self.prop, date(2026, 10, 12), [{'account': a('1110'), 'debit': D('300')}, {'account': a('4511'), 'credit': D('300')}], memo='Interest', basis='accrual')
        ledger.lock_month(self.prop, date(2026, 10, 1))
        bud = Budget.objects.create(association=self.prop, year=2026)
        BudgetLine.objects.create(budget=bud, account=a('6882'), annual_amount=D('200000'))
        BudgetLine.objects.create(budget=bud, account=a('4500'), annual_amount=D('200000'))
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.url = reverse('accounting:budget_year', args=[self.prop.pk, 2027])

    def draft(self):
        return bg.start_draft(self.prop, 2027, None)

    def test_other_income_is_a_section_and_the_assessment_is_net_of_it(self):
        ver = self.draft()
        self.assertEqual(sorted(ver.lines.values_list('account__code', flat=True)), ['4511', '4800', '6882'])      # the assessments are never a line
        self.assertEqual(ver.lines.get(account__code='4800').actual, D('2000.00'))                                 # income is credits less debits
        rows = bg.line_rows(ver, bg.info_for(ver))
        self.assertEqual({r['code']: r['section'] for r in rows}, {'4511': 'Income', '4800': 'Income', '6882': 'Administrative'})
        t = bg.totals(rows, 4)
        self.assertEqual(round(t['oi']['proposed'], 2), 2760.0)                                                     # 2,000 + 300, each with two more months at its average
        self.assertEqual((round(t['total'], 2), round(t['needed'], 2)), (129600.0, 126840.0))
        self.assertEqual(round(t['unit_month'], 2), round(126840 / 4 / 12, 2))
        self.assertEqual(round(t['needed_prior'], 2), 200000.0)

    def test_the_drill_down_shows_income_as_a_positive_amount(self):
        ver = self.draft()
        d = bg.actual_detail(ver, ver.lines.get(account__code='4800'))
        self.assertEqual((d['carried'], d['total'], d['actual'], d['matches']), (2000.0, 2000.0, 2000.0, True))

    def test_a_draft_made_before_income_counted_gets_its_income_lines(self):
        ver = self.draft()
        ver.lines.filter(account__code__in=['4800', '4511']).delete()
        self.assertTrue(bg.heal(ver, bg.versions(ver.budget)))
        self.assertEqual(sorted(ver.lines.values_list('account__code', flat=True)), ['4511', '4800', '6882'])
        self.assertFalse(bg.heal(ver, bg.versions(ver.budget)))

    def test_approving_writes_the_income_lines_and_the_assessment_net_of_them(self):
        ver = self.draft()
        bg.write_budget_lines(ver.budget, ver)
        amounts = {l.account.code: l.annual_amount for l in BudgetLine.objects.filter(budget=ver.budget).select_related('account')}
        self.assertEqual((amounts['4800'], amounts['4511'], amounts['6882'], amounts['4500']), (D('2400.00'), D('360.00'), D('129600.00'), D('126840.00')))

    def test_changes_are_in_parentheses_and_green_when_good_red_when_bad(self):
        from .views_budget import _sheet
        from .templatetags.acc_extras import cparen, dparen, pparen
        self.assertEqual((dparen(-80766), dparen(1234), dparen(0.2), pparen(-40.24), pparen(3.14), pparen(None), pparen(0.01), cparen(-2.5)), ('($80,766)', '$1,234', '—', '(40.2%)', '3.1%', 'new', '—', '($2.50)'))
        ver = self.draft()
        sheet = _sheet(ver, bg.info_for(ver), 4)
        by = {l['name']: l for sec in sheet['sections'] for l in sec['lines']}
        self.assertEqual(by['Insurance - Hazard, Liability & Property']['tone'], 'good')                          # spending $70,400 less than the budget
        self.assertEqual(by['Late Fee']['tone'], 'good')                                                           # more income than the (nil) budget
        self.assertEqual(sheet['needed']['tone'], 'good')
        self.assertEqual(sheet['calc'][0]['rows'][0]['tone'], 'good')                                              # owners pay less each month
        self.assertEqual((by['Late Fee']['p'], by['Late Fee']['ptone']), (None, 'good'))                           # "new" is coloured like the dollar change
        self.assertEqual(sheet['sections'][0]['lines'][0]['name'], 'Regular assessments')
        from .views_budget import _tone
        self.assertEqual((_tone(500), _tone(-500), _tone(500, income=True), _tone(-500, income=True), _tone(0.2)), ('bad', 'good', 'good', 'bad', ''))

    def test_the_sheet_screen_shows_it_all_and_boxes_the_new_years_column(self):
        self.client.post(self.url, {'action': 'start'})
        html = self.client.get(self.url + '?stage=draft&sheet=1').content.decode()
        for word in ('Regular assessments', 'Total income', 'Total expenses with reserves', '($70,400)', 'class="t-good"', 'class="c27"', 'tr class="grand last"', '3px double'):
            self.assertIn(word, html)
        self.assertNotIn('Net income', html)
        self.assertNotIn('Assessments needed', html)
        self.assertNotIn('>-$70,400<', html)

    def test_the_downloads_carry_the_same_formatting(self):
        import io
        import openpyxl
        self.client.post(self.url, {'action': 'start'})
        pdf = self.client.get(self.url + '?stage=draft&export=pdf')
        self.assertEqual((pdf.status_code, pdf.content[:4]), (200, b'%PDF'))
        xl = self.client.get(self.url + '?stage=draft&export=xlsx')
        ws = openpyxl.load_workbook(io.BytesIO(xl.content)).active
        rows = {ws.cell(row=r, column=1).value: r for r in range(1, ws.max_row + 1) if ws.cell(row=r, column=1).value}
        need, ins = rows['Total expenses with reserves'], rows['Insurance - Hazard, Liability & Property']
        self.assertNotIn('Net income', rows)
        self.assertEqual(ws.cell(row=need, column=2).border.bottom.style, 'double')                              # totals: a line above, a double line below
        self.assertEqual(ws.cell(row=ins, column=5).value, 129600 - 200000)
        self.assertIn('(', ws.cell(row=ins, column=5).number_format)
        self.assertEqual(ws.cell(row=ins, column=5).font.color.rgb[-6:].upper(), '4C7F5E')                       # green: less than the budget
        for r in (4, ins, need):
            c = ws.cell(row=r, column=4)
            self.assertEqual((c.border.left.style, c.border.right.style, c.fill.fgColor.rgb[-6:].upper()), ('medium', 'medium', 'E4EDFB'))
        self.assertEqual((ws.cell(row=4, column=4).border.top.style, ws.cell(row=need, column=4).border.bottom.style), ('medium', 'double'))


    def test_by_unit_calculation_is_one_group_when_everyone_pays_the_same(self):
        self.client.post(self.url, {'action': 'start'})
        html = self.client.get(self.url + '?stage=draft&sheet=1').content.decode()
        self.assertIn('By unit calculation', html)
        self.assertNotIn('What it means per unit', html)
        self.assertNotIn('Of that', html)
        self.assertIn('All 4 units, the same dues', html)
        self.assertNotIn('100, 101, 102, 103', html)                                                              # no list of units when everyone pays the same
        self.assertNotIn('Every unit pays the same.', html)
        self.assertNotIn('left off; the totals', html)

    def _shares(self, percents):
        from .models import ShareKind, UnitShare
        kind = ShareKind.objects.create(association=self.prop, name='Common expenses', is_default=True)
        for unit, pc in zip(sorted(self.prop.units.all(), key=lambda u: u.label), percents):
            UnitShare.objects.create(unit=unit, kind=kind, percent=D(pc))

    def test_unequal_shares_make_one_group_per_share_with_its_units_listed(self):
        from .views_budget import _sheet
        self._shares(['30', '30', '20', '20'])
        ver = self.draft()
        sheet = _sheet(ver, bg.info_for(ver), 4)
        g1, g2 = sheet['calc']
        self.assertEqual((g1['header'], g1['units'], g2['header'], g2['units']), ('2 units with a 30% share', '100, 101', '2 units with a 20% share', '102, 103'))
        self.assertAlmostEqual(g1['rows'][0]['new'], 126840 * 0.30 / 12, places=2)
        self.assertAlmostEqual(g2['rows'][0]['new'], 126840 * 0.20 / 12, places=2)
        self.assertAlmostEqual(g1['rows'][0]['old'], 200000 * 0.30 / 12, places=2)                                # the 2026 budget divided the same way
        self.assertAlmostEqual(g1['rows'][1]['new'], g1['rows'][0]['new'] * 12, places=2)
        self.assertEqual(sheet['basis_note'], "Split by each unit's share of the common expenses.")
        self.client.post(self.url, {'action': 'start'})
        html = self.client.get(self.url + '?stage=draft&sheet=1').content.decode()
        self.assertIn('2 units with a 30% share', html)

    def test_a_flat_assessment_rule_beats_the_shares_and_a_share_rule_beats_equal_dues(self):
        from .models import ChargeRule
        self._shares(['30', '30', '20', '20'])
        rule = ChargeRule.objects.create(association=self.prop, name='Monthly assessment', frequency='monthly', first_due=date(2026, 1, 1), method='flat', amount=D('615'),
                                         income_account=ledger.account(self.prop, '4500'))
        groups, basis = bg.unit_groups(self.prop)
        self.assertEqual((basis, len(groups), groups[0]['count']), ('equal', 1, 4))
        rule.method = 'share'
        rule.save()
        self.assertEqual((bg.unit_groups(self.prop)[1], len(bg.unit_groups(self.prop)[0])), ('share', 2))

    def test_the_drill_down_dates_are_month_and_day_numbers(self):
        self.client.post(self.url, {'action': 'start'})
        page = self.client.get(self.url).content.decode()
        self.assertIn("var dt = r.date.slice(5);", page)
        self.assertNotIn("toLocaleDateString", page)

    def test_the_downloads_carry_the_by_unit_calculation(self):
        import io
        import openpyxl
        self.client.post(self.url, {'action': 'start'})
        ws = openpyxl.load_workbook(io.BytesIO(self.client.get(self.url + '?stage=draft&export=xlsx').content)).active
        col = [str(ws.cell(row=r, column=1).value) for r in range(1, ws.max_row + 1)]
        self.assertTrue(any(c == 'By unit calculation' for c in col))
        self.assertTrue(any(c == 'All 4 units, the same dues' for c in col))
        self.assertFalse(any('Of that' in c for c in col))
        self.assertEqual(self.client.get(self.url + '?stage=draft&export=pdf').content[:4], b'%PDF')

    def test_the_change_columns_say_they_are_against_last_years_budget(self):
        import io
        import openpyxl
        from pypdf import PdfReader
        self.client.post(self.url, {'action': 'start'})
        said = 'compare the 2027 budget with the 2026 budget'
        html = self.client.get(self.url + '?stage=draft&sheet=1').content.decode()
        self.assertEqual((html.count('vs 2026 budget</small>'), html.count('vs 2026</small>'), said in html), (2, 2, True))                       # $ and % change, in the sheet
        pdf = PdfReader(io.BytesIO(self.client.get(self.url + '?stage=draft&export=pdf').content)).pages[0].extract_text()
        self.assertIn(said, pdf)
        self.assertEqual(pdf.count('vs 2026 budget'), 2)
        ws = openpyxl.load_workbook(io.BytesIO(self.client.get(self.url + '?stage=draft&export=xlsx').content)).active
        self.assertIn(said, str(ws['A3'].value))
        heads = [str(ws.cell(row=4, column=c).value) for c in range(1, 7)]
        self.assertEqual(heads[4:], ['$ change\nvs 2026 budget', '% change\nvs 2026 budget'])


    def test_the_assessments_are_the_first_income_line_and_total_income_matches_the_expenses_next_year(self):
        from .views_budget import _sheet
        a = ledger.account(self.prop, '1110'), ledger.account(self.prop, '4500')
        ledger.post_entry(self.prop, date(2026, 10, 3), [{'account': a[0], 'debit': D('30000')}, {'account': a[1], 'credit': D('30000')}], memo='Assessments', basis='accrual', allow_locked=True)
        ver = self.draft()
        info = bg.info_for(ver)
        sheet = _sheet(ver, info, 4)
        inc = sheet['sections'][0]
        self.assertEqual((inc['key'], inc['name'], inc['lines'][0]['name']), ('Income', 'Income', 'Regular assessments'))
        reg = inc['lines'][0]
        self.assertEqual((reg['prior'], reg['projected'], round(reg['proposed'], 2)), (200000.0, 36000.0, 126840.0))      # 30,000 so far, and two more months at the same pace
        self.assertEqual(round(inc['proposed'], 2), round(sheet['total']['proposed'], 2))                              # next year the income covers the expenses exactly
        self.assertEqual([l['name'] for l in inc['lines']][1:], ['Interest Income', 'Late Fee'])
        self.assertEqual(bg.editor_state(ver, bg.versions(ver.budget))['assess']['projected'], 36000.0)

    def test_totals_have_a_line_above_and_a_double_line_below(self):
        self.client.post(self.url, {'action': 'start'})
        html = self.client.get(self.url + '?stage=draft&sheet=1').content.decode()
        self.assertIn('.bd-sheet .bd-table tr.tot td, .bd-sheet .bd-table tr.grand td { border-top: 1px solid #1c2430; border-bottom: 3px double #1c2430; }', html)
