from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import Property

NO_MANIFEST = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
               'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}


@override_settings(STORAGES=NO_MANIFEST)
class OwnerLegalNameTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.prop = Property.objects.create(name='800 Tropic', property_type=Property.Type.SHORT_TERM_RENTAL, financials_managed=True)

    def test_legal_name_is_saved_from_the_property_card(self):
        url = reverse('property_detail', args=[self.prop.pk])
        r = self.client.post(url, {'action': 'qb_set_owner_legal_name', 'owner_legal_name': '  4KC,   LLC '})
        self.assertEqual(r.status_code, 302)
        self.prop.refresh_from_db()
        self.assertEqual(self.prop.owner_legal_name, '4KC, LLC')
        self.assertContains(self.client.get(url), 'value="4KC, LLC"')

    def test_missing_name_shows_a_warning_not_a_block(self):
        self.assertContains(self.client.get(reverse('property_detail', args=[self.prop.pk])), 'Not set')
        self.assertEqual(self.client.get(reverse('close_pay_owners')).status_code, 200)


from decimal import Decimal as D

from django.template.loader import render_to_string

from . import ledger


def _totals(**kw):
    t = {'calc': True, 'prior_status': 'ok', 'owner_collects': False, 'expenses_reimbursable': D('0'), 'commission_due': D('150'), 'owner_due': D('3300'), 'reimbursement_trust': D('0'), 'commission': D('150'),
         'owner_payment': D('3300'), 'prior_reimbursable': D('0'), 'prior_commission': D('0'), 'prior_owner': D('0')}
    t.update(kw)
    return t


class SameMonthSettlementTests(TestCase):
    def test_paid_in_full_the_month_it_was_collected_has_no_difference(self):
        sm = ledger.same_month_settlement(_totals())
        self.assertEqual(sm['this'], {'reimb': D('0'), 'comm': D('150'), 'owner': D('3300')})
        self.assertEqual(sm['diff'], {'reimb': D('0'), 'comm': D('0'), 'owner': D('0')})

    def test_the_carry_from_last_month_and_this_months_earnings_are_both_counted(self):
        sm = ledger.same_month_settlement(_totals(prior_owner=D('900'), prior_commission=D('150'), owner_payment=D('3300'), commission=D('150')))
        self.assertEqual(sm['diff']['owner'], D('-900'))               # 900 from last month and 3,300 earned; 3,300 paid: 900 still owed
        self.assertEqual(sm['diff']['comm'], D('-150'))

    def test_paid_more_than_was_owed_is_a_positive_difference(self):
        self.assertEqual(ledger.same_month_settlement(_totals(owner_payment=D('3500')))['diff']['owner'], D('200'))

    def test_nothing_to_compare_with(self):
        self.assertIsNone(ledger.same_month_settlement(_totals(prior_status='not_in_books')))
        self.assertIsNone(ledger.same_month_settlement(_totals(owner_collects=True)))
        self.assertIsNone(ledger.same_month_settlement({'calc': False}))

    def test_the_table_shows_the_new_row_only_for_a_rental_paid_the_same_month(self):
        from datetime import date
        t = _totals(prior_month=date(2026, 9, 1), prior_closed=True, commission_rate=D('10'), commission_basis='gross', deposits=D('3600'), expenses_direct=D('0'), net_income=D('0'),
                    owner_baseline_expense=D('0'), commission_base=D('0'), owner_owes_us=D('0'), prior_opening=False)
        same = render_to_string('core/_close_summary.html', {'totals': t, 'month': date(2026, 10, 1), 'property': type('P', (), {'owner_paid_in': 'same_month'})()})
        later = render_to_string('core/_close_summary.html', {'totals': t, 'month': date(2026, 10, 1), 'property': type('P', (), {'owner_paid_in': 'next_month'})()})
        self.assertIn('Owed from October', same)
        self.assertNotIn('Owed from October', later)
        self.assertIn('settles both what was still owed from September and what October earned', same)


from datetime import date
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse

from . import close_ltr_tax, month_close_project, month_end_payments, workdays
from .models import LedgerLine, Property, RentCheck, SalesTaxFiling, SalesTaxReturn, StaffProfile


class WorkdayTests(TestCase):
    def test_working_days_skip_weekends_and_us_holidays(self):
        self.assertEqual(workdays.workday_of_month(2026, 11, 1), date(2026, 11, 2))       # November 1 is a Sunday
        self.assertEqual(workdays.workday_of_month(2026, 11, 4), date(2026, 11, 5))
        self.assertEqual(workdays.workday_of_month(2026, 1, 1), date(2026, 1, 2))         # New Year's Day
        self.assertEqual(workdays.workday_of_month(2026, 7, 3), date(2026, 7, 6))         # July 4 is a Saturday: observed Friday the 3rd
        self.assertFalse(workdays.is_workday(date(2026, 11, 26)))                         # Thanksgiving
        self.assertFalse(workdays.is_workday(date(2026, 5, 25)))                          # Memorial Day
        self.assertTrue(workdays.is_workday(date(2026, 11, 27)))


def make_staff(username='closer'):
    user = get_user_model().objects.create_user(username, password='pw12345678')
    return StaffProfile.objects.create(user=user, role=StaffProfile.Role.ACCOUNTING, is_company_admin=True, can_financial=True, can_time_admin=True)


def make_ltr(name='1 Ross', rent='3600', same_month=False):
    return Property.objects.create(name=name, property_type=Property.Type.LONG_TERM_RENTAL, financials_managed=True, monthly_rent=D(rent) if rent else None,
                                   owner_paid_in=Property.OwnerPaidIn.SAME_MONTH if same_month else Property.OwnerPaidIn.NEXT_MONTH)


def deposit(prop, month, amount, n=1):
    return LedgerLine.objects.create(property=prop, role='trust', txn_type='Deposit', txn_id=f'{prop.pk}{month:%m}{n}', txn_date=month.replace(day=3), month=month, flow=D(amount), category='deposit',
                                     category_source='user', reviewed=True)


class RentCollectionTests(TestCase):
    def setUp(self):
        self.prop = make_ltr()
        self.nov = date(2026, 11, 1)
        self.oct = date(2026, 10, 1)

    def row(self):
        return close_ltr_tax.rent_rows(self.oct)[0]

    def test_the_rent_is_for_the_month_after_the_close(self):
        self.assertEqual(close_ltr_tax.rent_month(self.oct), self.nov)

    def test_what_quickbooks_shows_is_compared_with_the_rent(self):
        self.assertEqual(self.row()['kind'], 'missing')
        deposit(self.prop, self.nov, '3600')
        r = self.row()
        self.assertEqual((r['seen'], r['diff'], r['kind']), (D('3600'), D('0'), 'matches'))
        deposit(self.prop, self.nov, '100', n=2)
        self.assertEqual(self.row()['kind'], 'over')

    def test_a_difference_needs_a_reason_to_confirm_as_received(self):
        deposit(self.prop, self.nov, '3000')
        with self.assertRaises(ValueError):
            close_ltr_tax.confirm_rent(self.prop, self.oct, 'received', '', None)
        check = close_ltr_tax.confirm_rent(self.prop, self.oct, 'received', 'Tenant paid the rest on the 5th', None)
        self.assertTrue(check.counts_as_collected)
        self.assertEqual((check.seen, check.expected), (D('3000'), D('3600')))
        self.assertEqual(close_ltr_tax.rent_summary(self.oct)['status'], 'done')

    def test_not_received_is_a_confirmation_that_does_not_count_as_collected(self):
        close_ltr_tax.confirm_rent(self.prop, self.oct, 'not_received', '', None)
        self.assertFalse(RentCheck.objects.get().counts_as_collected)


class PayLongTermOwnersTests(TestCase):
    def setUp(self):
        self.arrears = make_ltr('Arrears Rental', same_month=False)
        self.same = make_ltr('Same Month Rental', same_month=True)
        self.oct, self.nov = date(2026, 10, 1), date(2026, 11, 1)
        for p in (self.arrears, self.same):
            deposit(p, self.oct, '3600')
            deposit(p, self.nov, '3600', n=2)

    def rows(self):
        return {r['property'].name: r for r in month_end_payments.rows(self.oct, month_end_payments.Kind.OWNER, 'ltr')}

    def test_arrears_owners_are_paid_for_the_closed_month_and_same_month_owners_for_this_month(self):
        rows = self.rows()
        self.assertEqual(rows['Arrears Rental']['pay_month'], self.oct)
        self.assertEqual(rows['Same Month Rental']['pay_month'], self.nov)
        self.assertEqual(rows['Arrears Rental']['timing'], 'In arrears')
        self.assertEqual(rows['Same Month Rental']['timing'], 'Same month')

    def test_a_same_month_owner_waits_for_the_rent_to_be_confirmed(self):
        self.assertEqual(self.rows()['Same Month Rental']['state'], 'waiting_rent')
        close_ltr_tax.confirm_rent(self.same, self.oct, 'received', 'ok', None)
        self.assertEqual(self.rows()['Same Month Rental']['state'], 'payable')
        self.assertGreater(self.rows()['Same Month Rental']['amount'], 0)

    def test_an_arrears_owner_is_not_held_up_by_this_months_rent(self):
        # last month is not closed, so it is not payable yet, but it is never "waiting for rent"
        self.assertNotEqual(self.rows()['Arrears Rental']['state'], 'waiting_rent')

    def test_the_short_term_owner_page_does_not_list_long_term_rentals(self):
        names = [r['property'].name for r in month_end_payments.rows(self.oct, month_end_payments.Kind.OWNER)]
        self.assertNotIn('Same Month Rental', names)


class SalesTaxTests(TestCase):
    def test_which_returns_are_due_for_a_month(self):
        monthly = SalesTaxReturn.objects.create(name='FL sales tax A', entity='A LLC', frequency='monthly')
        quarterly = SalesTaxReturn.objects.create(name='FL sales tax B', entity='B LLC', frequency='quarterly')
        annual = SalesTaxReturn.objects.create(name='FL sales tax C', frequency='annual')
        oct_rows = [r['ret'].name for r in close_ltr_tax.tax_rows(date(2026, 10, 1))]
        self.assertEqual(oct_rows, ['FL sales tax A'])
        dec_rows = {r['ret'].name for r in close_ltr_tax.tax_rows(date(2026, 12, 1))}
        self.assertEqual(dec_rows, {'FL sales tax A', 'FL sales tax B', 'FL sales tax C'})
        self.assertEqual({r['ret'].name for r in close_ltr_tax.tax_rows(date(2026, 9, 1))}, {'FL sales tax A', 'FL sales tax B'})

    def test_ticking_them_off_completes_the_step(self):
        ret = SalesTaxReturn.objects.create(name='FL sales tax A', frequency='monthly')
        self.assertEqual(close_ltr_tax.tax_summary(date(2026, 10, 1))['status'], 'not_started')
        SalesTaxFiling.objects.create(tax_return=ret, month=date(2026, 10, 1), filed_on=date(2026, 11, 20))
        self.assertEqual(close_ltr_tax.tax_summary(date(2026, 10, 1))['status'], 'done')
        self.assertEqual(close_ltr_tax.tax_summary(date(2026, 11, 1))['status'], 'not_started')


@override_settings(STORAGES={'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                             'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class MonthCloseProjectTests(TestCase):
    def setUp(self):
        make_staff()
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))

    def test_the_checklist_has_fifteen_steps_in_four_days(self):
        from .close_views import close_steps
        steps = close_steps(date(2026, 10, 1), date(2026, 11, 3))
        self.assertEqual([s['n'] for s in steps], list(range(1, 16)))
        self.assertEqual([s['day'] for s in steps], [1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 4, 4, 4])
        self.assertEqual([s['title'] for s in steps][6:], ['Pay owners', 'Pay Proper', 'Tourist tax', 'Pay sales tax', 'Long-term rent collection', 'Close associations', 'Pay long-term owners', 'Close commercial properties', 'Lease renewals'])
        self.assertEqual(steps[0]['due'], date(2026, 11, 2))
        self.assertEqual(steps[6]['due'], date(2026, 11, 3))
        self.assertEqual(steps[12]['due'], date(2026, 11, 5))
        self.assertEqual(steps[6]['deadline'], date(2026, 11, 5))

    def test_each_step_becomes_its_own_compliance_item_on_its_day(self):
        from worksessions.models import Session
        with mock.patch.object(month_close_project, 'workdays', workdays):
            made = month_close_project.ensure_items(date(2026, 10, 1), date(2026, 11, 3))
        self.assertEqual(made, 15)
        by_name = {s.template.name: s for s in Session.objects.select_related('template')}
        self.assertEqual(by_name['Month-end close: 1. Bank tie-out'].due_at, date(2026, 11, 2))
        self.assertEqual(by_name['Month-end close: 7. Pay owners'].due_at, date(2026, 11, 3))
        self.assertEqual(by_name['Month-end close: 13. Pay long-term owners'].due_at, date(2026, 11, 5))
        self.assertIn('owners must be paid by Nov 5', by_name['Month-end close: 13. Pay long-term owners'].lines.get().label)
        self.assertEqual(month_close_project.ensure_items(date(2026, 10, 1), date(2026, 11, 3)), 0)       # once

    def test_a_step_that_is_done_finishes_its_item(self):
        from worksessions.models import Session
        month_close_project.ensure_items(date(2026, 10, 1), date(2026, 11, 3))
        from . import close_views
        real = close_views.close_steps

        def fake(month, today=None):
            steps = real(month, today)
            for s in steps:
                s['status'] = 'done' if s['n'] in (1, 2) else 'not_started'
            return steps
        with mock.patch.object(close_views, 'close_steps', fake):
            made, finished = month_close_project.sync(date(2026, 11, 3))
        self.assertEqual(finished, 2)
        done = set(Session.objects.filter(status='submitted').values_list('template__name', flat=True))
        self.assertEqual(done, {'Month-end close: 1. Bank tie-out', 'Month-end close: 2. Misc trust account'})

    def open_items(self):
        from worksessions.models import Session
        return set(Session.objects.filter(status='open', template__name__startswith=month_close_project.PREFIX).values_list('template__name', flat=True))

    def test_opening_the_checklist_closes_the_items_of_the_steps_it_shows_done(self):
        from . import close_views
        month_close_project.ensure_items(date(2026, 10, 1), date(2026, 11, 3))
        real = close_views.close_steps

        def fake(month, today=None):
            steps = real(month, today)
            for s in steps:
                s['status'] = 'done' if s['n'] == 13 else 'not_started'
            return steps
        with mock.patch.object(close_views, 'close_steps', fake):
            self.assertEqual(self.client.get(reverse('close_home') + '?month=2026-10').status_code, 200)
        self.assertNotIn('Month-end close: 13. Pay long-term owners', self.open_items())
        self.assertEqual(len(self.open_items()), 14)

    def test_a_payment_step_that_is_done_closes_its_item_the_moment_its_page_is_opened(self):
        from . import close_views
        month_close_project.ensure_items(date(2026, 10, 1), date(2026, 11, 3))
        with mock.patch.object(close_views.month_end_payments, 'summary', return_value={'status': 'done', 'detail': '3 of 3 paid or held.', 'open': 0}):
            self.client.get(reverse('close_pay_ltr_owners') + '?month=2026-10')
        self.assertNotIn('Month-end close: 13. Pay long-term owners', self.open_items())
        self.assertIn('Month-end close: 7. Pay owners', self.open_items())                       # only its own step

    def test_a_step_that_is_not_done_leaves_its_item_open_and_other_months_are_left_alone(self):
        month_close_project.ensure_items(date(2026, 10, 1), date(2026, 11, 3))
        self.assertEqual(month_close_project.finish_step(13, date(2026, 10, 1), 'attention'), 0)
        self.assertEqual(month_close_project.finish_step(13, date(2026, 9, 1), 'done'), 0)
        self.assertIn('Month-end close: 13. Pay long-term owners', self.open_items())
        self.assertEqual(month_close_project.finish_step(13, date(2026, 10, 1), 'done'), 1)
        self.assertEqual(month_close_project.finish_step(13, date(2026, 10, 1), 'done'), 0)       # once

    def test_the_pages_open(self):
        for name in ('close_home', 'close_rent_collection', 'close_sales_tax', 'close_pay_ltr_owners', 'close_pay_owners'):
            self.assertEqual(self.client.get(reverse(name) + '?month=2026-10').status_code, 200, name)
        html = self.client.get(reverse('close_home') + '?month=2026-10').content.decode()
        self.assertIn('Day 1', html)
        self.assertIn('Day 4', html)
        self.assertIn('Pay long-term owners', html)

    def test_opening_a_close_item_goes_straight_to_the_steps_own_screen(self):
        from worksessions.models import Session
        from .close_views import close_steps
        month_close_project.ensure_items(date(2026, 10, 1), date(2026, 11, 3))
        sessions = {s.template.name: s for s in Session.objects.select_related('template')}
        steps = close_steps(date(2026, 10, 1), date(2026, 11, 3))
        self.assertEqual(len(steps), 15)
        for step in steps:
            session = sessions[f'{month_close_project.PREFIX}{step["n"]}. {step["title"]}']
            response = self.client.get(reverse('session_detail', args=[session.pk]))
            self.assertEqual(response.status_code, 302, step['title'])
            self.assertEqual(response['Location'], step['url'], step['title'])
        tie = sessions['Month-end close: 1. Bank tie-out']
        self.assertEqual(self.client.get(reverse('session_detail', args=[tie.pk]))['Location'], reverse('close_bank_tieout') + '?month=2026-10')
        payouts = sessions['Month-end close: 4. Platform payouts']
        self.assertEqual(self.client.get(reverse('session_detail', args=[payouts.pk]))['Location'], reverse('close_payouts'))         # that screen has no month
        assoc = sessions['Month-end close: 12. Close associations']
        self.assertEqual(self.client.get(reverse('session_detail', args=[assoc.pk]))['Location'], reverse('accounting:home') + '?month=2026-10')

    def test_a_finished_close_item_opens_the_screen_too_and_a_post_is_not_redirected_there(self):
        from worksessions.models import Session
        month_close_project.ensure_items(date(2026, 10, 1), date(2026, 11, 3))
        session = Session.objects.get(template__name='Month-end close: 9. Tourist tax')
        session.status = Session.Status.SUBMITTED
        session.save()
        url = reverse('session_detail', args=[session.pk])
        self.assertEqual(self.client.get(url)['Location'], reverse('tourist_tax') + '?month=2026-10')
        response = self.client.post(url, {'action': 'nothing'})
        self.assertNotIn('/close/', response.get('Location', ''))

    def test_other_items_keep_their_own_screen(self):
        from worksessions.models import Frequency, Session, SessionLine, SessionTemplate
        staff = StaffProfile.objects.get()
        rule = SessionTemplate.objects.create(name='Daily Admin', owner=staff, department=StaffProfile.Role.ACCOUNTING, frequency=Frequency.DAILY, next_open_date=date(2026, 11, 3))
        session = Session.objects.create(template=rule, owner=staff, department=staff.role, period_label='Nov 3', period_key=date(2026, 11, 3), opens_at=date(2026, 11, 3), due_at=date(2026, 11, 3))
        SessionLine.objects.create(session=session, label='Check the mail')
        self.assertEqual(self.client.get(reverse('session_detail', args=[session.pk])).status_code, 200)
        # a close item whose month can no longer be read (its notes were changed) also stays put rather than breaking
        month_close_project.ensure_items(date(2026, 10, 1), date(2026, 11, 3))
        odd = Session.objects.get(template__name='Month-end close: 2. Misc trust account')
        odd.notes = 'close-month:soon'
        odd.save()
        self.assertEqual(self.client.get(reverse('session_detail', args=[odd.pk])).status_code, 200)
        self.assertIsNone(month_close_project.screen_url(odd))

    def test_the_rent_and_sales_tax_screens_work(self):
        prop = make_ltr()
        url = reverse('close_rent_collection') + '?month=2026-10'
        self.client.post(url, {'action': 'set_rent', 'property': prop.pk, 'monthly_rent': '2750'})
        prop.refresh_from_db()
        self.assertEqual(prop.monthly_rent, D('2750.00'))
        deposit(prop, date(2026, 11, 1), '2750')
        self.client.post(url, {'action': 'confirm', 'property': prop.pk, 'status': 'received', 'note': ''})
        self.assertTrue(RentCheck.objects.get().counts_as_collected)
        tax = reverse('close_sales_tax') + '?month=2026-10'
        self.client.post(tax, {'action': 'add', 'name': 'FL sales tax, 12 Main', 'entity': 'Main LLC', 'frequency': 'monthly', 'due_day': '20'})
        ret = SalesTaxReturn.objects.get()
        self.client.post(tax, {'action': 'file', 'ret': ret.pk, 'filed_on': '2026-11-18', 'confirmation': 'C-123'})
        self.assertEqual(SalesTaxFiling.objects.get().confirmation, 'C-123')


def ledger_line(prop, month, flow, role='trust', category='expense', type_='Journal Entry', n='x', day=4, status='active', **kw):
    return LedgerLine.objects.create(property=prop, role=role, txn_type=type_, txn_id=f'{prop.pk}{month:%m}{n}', txn_date=month.replace(day=day), month=month, flow=D(flow), category=category,
                                     status=status, **kw)


@override_settings(STORAGES=NO_MANIFEST)
class RentCollectionShowsEverythingTests(TestCase):
    """The screen lists every transaction of the month for a rental, so a reversed deposit is seen, not only the one line that counts as rent."""

    def setUp(self):
        make_staff()
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.prop = make_ltr()
        self.nov, self.oct = date(2026, 11, 1), date(2026, 10, 1)
        deposit(self.prop, self.nov, '3600')
        ledger_line(self.prop, self.nov, '-3600', n='rev', day=6, payee='Tenant Tom', memo='Returned: NSF')
        ledger_line(self.prop, self.nov, '500', category='deposit', type_='Deposit', n='gone', day=7, status='removed', memo='Deposited twice')
        ledger_line(self.prop, self.nov, '80', role='expense', n='bill', day=8, type_='Bill', payee='Plumber')
        ledger_line(self.prop, self.oct, '999', n='oct', day=9, memo='An October line')                                          # another month
        other = make_ltr('2 Ross')
        ledger_line(other, self.nov, '123', n='other', memo='Another rental')

    def test_every_line_of_the_month_is_there_reversal_and_removed_ones_included(self):
        t = close_ltr_tax.transactions(self.prop, self.oct)
        self.assertEqual(t['count'], 4)
        self.assertEqual([l['amount'] for l in t['lines'] if l['trust']], [D('3600'), D('-3600'), D('500')])                      # trust account first, by date
        self.assertFalse(t['lines'][3]['trust'])                                                                                  # then the expense account
        self.assertEqual((t['money_in'], t['money_out'], t['net'], t['removed']), (D('3600'), D('3600'), D('0'), 1))              # the removed line is not counted in the totals
        self.assertEqual([l['counts'] for l in t['lines']], [True, False, False, False])                                          # only the live deposit is counted as rent
        self.assertNotIn('An October line', ' '.join(l['what'] for l in t['lines']))
        self.assertNotIn('Another rental', ' '.join(l['what'] for l in t['lines']))
        self.assertEqual(close_ltr_tax.rent_rows(self.oct)[0]['seen'], D('3600'))                                                  # what QuickBooks shows is unchanged

    def test_a_rental_with_nothing_in_the_month_says_so(self):
        quiet = make_ltr('Quiet Rental')
        t = close_ltr_tax.transactions(quiet, self.oct)
        self.assertEqual((t['count'], t['net']), (0, D('0')))

    def test_the_screen_shows_them_and_links_to_where_the_rent_is_changed(self):
        html = self.client.get(reverse('close_rent_collection') + '?month=2026-10').content.decode()
        self.assertIn('All transactions in November (4)', html)
        self.assertIn('Returned: NSF', html)
        self.assertIn('Deposited twice', html)
        self.assertIn('gone from QuickBooks', html)
        self.assertIn('counted as rent', html)
        self.assertIn('Plumber', html)
        self.assertNotIn('An October line', html)
        page = reverse('property_detail', args=[self.prop.pk])
        self.assertIn(f'href="{page}#monthly-rent"', html)                                                                       # Change, beside the rent
        self.assertIn(f'href="{page}"', html)                                                                                    # the rental's name opens its page


@override_settings(STORAGES=NO_MANIFEST)
class MonthlyRentEditTests(TestCase):
    """The expected rent of a long-term rental is changed on its property page (it could only be set once, on the collection screen)."""

    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.prop = make_ltr(rent='3600')
        self.url = reverse('property_detail', args=[self.prop.pk])

    def rent(self):
        self.prop.refresh_from_db()
        return self.prop.monthly_rent

    def test_the_rent_is_changed_from_the_property_page(self):
        self.assertContains(self.client.get(self.url), 'id="monthly-rent"')
        self.assertContains(self.client.get(self.url), 'value="3600.00"')
        response = self.client.post(self.url, {'action': 'qb_set_monthly_rent', 'monthly_rent': ' $3,950.5 '})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response['Location'].endswith('#quickbooks'))
        self.assertEqual(self.rent(), D('3950.50'))
        self.assertEqual(close_ltr_tax.rent_rows(date(2026, 10, 1))[0]['expected'], D('3950.50'))                                 # the collection screen compares with it

    def test_a_blank_clears_it_and_something_that_is_not_an_amount_is_refused(self):
        self.client.post(self.url, {'action': 'qb_set_monthly_rent', 'monthly_rent': 'lots'})
        self.assertEqual(self.rent(), D('3600.00'))
        self.client.post(self.url, {'action': 'qb_set_monthly_rent', 'monthly_rent': '-5'})
        self.assertEqual(self.rent(), D('3600.00'))
        self.client.post(self.url, {'action': 'qb_set_monthly_rent', 'monthly_rent': ''})
        self.assertIsNone(self.rent())
        self.assertContains(self.client.get(self.url), 'Not set')
        self.assertEqual(close_ltr_tax.rent_rows(date(2026, 10, 1))[0]['kind'], 'no_rent')

    def test_only_a_long_term_rental_has_one_and_only_an_administrator_changes_it(self):
        short = Property.objects.create(name='800 Tropic', property_type=Property.Type.SHORT_TERM_RENTAL, financials_managed=True)
        self.client.post(reverse('property_detail', args=[short.pk]), {'action': 'qb_set_monthly_rent', 'monthly_rent': '1000'})
        short.refresh_from_db()
        self.assertIsNone(short.monthly_rent)
        self.assertNotContains(self.client.get(reverse('property_detail', args=[short.pk])), 'id="monthly-rent"')
        self.client.logout()
        self.client.force_login(get_user_model().objects.create_user('staffer', password='pw12345678'))
        self.client.post(self.url, {'action': 'qb_set_monthly_rent', 'monthly_rent': '1'})
        self.assertEqual(self.rent(), D('3600.00'))


# ---------------------------------------------------------------- the owner dashboard and where the reminders went

from django.core.cache import cache

from . import listings, nudges
from .models import ContactImportCandidate, StaffProfile as _Staff, Unit


@override_settings(STORAGES=NO_MANIFEST)
class OwnerDashboardAndRemindersTests(TestCase):
    def setUp(self):
        cache.clear()
        user = get_user_model().objects.create_user('owner', password='pw12345678')
        self.profile = _Staff.objects.create(user=user, role=_Staff.Role.ADMIN, is_company_admin=True) if hasattr(_Staff.Role, 'ADMIN') else _Staff.objects.create(user=user, is_company_admin=True)
        self.client.force_login(user)
        self.user = user

    def test_the_dashboard_has_what_was_asked_for_and_nothing_that_was_dropped(self):
        r = self.client.get(reverse('dashboard') + '?stats=1')
        self.assertEqual(r.status_code, 200)
        for kept in ('Who opened and who closed', 'Today', 'Last 7 days', 'Last 30 days', 'Updated today', 'Closed today', 'Hover any dot'):
            self.assertContains(r, kept)
        for gone in ('Needs attention', 'Off track', 'Company Financials', 'Local weather', "Today's Calendar", 'On-Site next 48', 'Open-Meteo'):
            self.assertNotContains(r, gone, msg_prefix=gone)
        html = r.content.decode()
        self.assertIn('<details class="card mb-2 od-coll"', html)         # the two lists start closed
        self.assertNotIn('<details class="card mb-2 od-coll" open', html)

    def test_flow_windows_count_today_the_week_and_the_month(self):
        from datetime import timedelta
        from django.utils import timezone
        from tickets.models import Ticket
        p = Property.objects.create(name='9 Test St', property_type=Property.Type.SHORT_TERM_RENTAL)
        now = timezone.now()
        for ago in (0, 3, 20):
            t = Ticket.objects.create(title=f'T{ago}', property=p, created_by=self.user)
            Ticket.objects.filter(pk=t.pk).update(created_at=now - timedelta(days=ago, minutes=1))
        from tickets.services import owner_dashboard as od
        f = od.flow_windows(now)
        self.assertEqual([(w['key'], w['opened']) for w in f['windows']], [('today', 1), ('week', 2), ('month', 3)])
        self.assertEqual(len(f['series']), 30)
        self.assertEqual(f['series'][-1]['opened'], 1)
        self.assertIn('label', f['series'][-1])

    def test_a_pending_contact_is_a_number_on_contacts_and_a_strip_on_that_page_not_on_the_dashboard(self):
        ContactImportCandidate.objects.create(source='quo', name='Someone New', phone='5615550100')
        self.assertEqual(nudges.nav_counts(self.user), {'contacts': 1})
        page = self.client.get(reverse('contact_list'))
        self.assertContains(page, 'waiting for a yes or no')                    # in the Alerts panel (the strip at the top of the page is gone)
        self.assertContains(page, '<span class="alerts-n">1</span>')
        self.assertNotContains(page, 'need attention in Contacts')
        self.assertContains(page, 'class="nav-pip"')
        self.assertNotContains(self.client.get(reverse('dashboard') + '?stats=1'), 'waiting for a yes or no')
        self.assertNotContains(self.client.get(reverse('property_list')), 'waiting for a yes or no')

    def test_short_term_rental_reminders_gather_on_the_onsite_number(self):
        prop = Property.objects.create(name='12 Palm Way', property_type=Property.Type.SHORT_TERM_RENTAL)
        Unit.objects.create(property=prop, label='A')
        keys = [i['key'] for i in nudges.section_items(self.user, 'onsite')]
        self.assertIn('listing_links', keys)
        self.assertIn('specs', keys)
        self.assertIn('trash', keys)
        self.assertEqual(nudges.nav_counts(self.user)['onsite'], len(keys))        # the number counts reminders, not units
        # these are about the properties, so they live behind the Alerts button on the Properties screen (the On-Site screen has its own Alerts for the day's work)
        page = self.client.get(reverse('property_list'))
        self.assertContains(page, '1 unit with no Airbnb or VRBO listing link')
        self.assertContains(page, '12 Palm Way')
        self.assertNotContains(self.client.get(reverse('onsite_dashboard')), 'need attention in On-Site')

    def test_marking_a_unit_not_on_airbnb_or_vrbo_stops_the_reminder_counting_it(self):
        prop = Property.objects.create(name='12 Palm Way', property_type=Property.Type.SHORT_TERM_RENTAL)
        a, b = Unit.objects.create(property=prop, label='A'), Unit.objects.create(property=prop, label='B')
        self.assertEqual(listings.attention()['places_without_links'], 2)
        url = reverse('property_detail', args=[prop.pk])
        self.client.post(url, {'action': 'save_listing_links', 'unit_id': a.pk, 'no_listing_field': '1', 'no_listing_link': 'on', 'airbnb_url': '', 'vrbo_url': ''})
        a.refresh_from_db()
        self.assertTrue(a.no_listing_link)
        self.assertEqual(listings.attention()['places_without_links'], 1)
        self.client.post(url, {'action': 'save_listing_links', 'unit_id': b.pk, 'no_listing_field': '1', 'no_listing_link': 'on', 'airbnb_url': '', 'vrbo_url': ''})
        self.assertEqual(listings.attention()['places_without_links'], 0)
        # unticking brings it back; a link on a platform means it is on one after all
        self.client.post(url, {'action': 'save_listing_links', 'unit_id': a.pk, 'no_listing_field': '1', 'airbnb_url': '', 'vrbo_url': ''})
        self.assertEqual(listings.attention()['places_without_links'], 1)
        self.assertContains(self.client.get(url), "Not on Airbnb or VRBO (or we won")

    def test_a_property_with_no_units_can_be_marked_too(self):
        prop = Property.objects.create(name='4 Solo Ct', property_type=Property.Type.SHORT_TERM_RENTAL)
        self.assertEqual(listings.attention()['places_without_links'], 1)
        self.client.post(reverse('property_detail', args=[prop.pk]), {'action': 'save_listing_links', 'no_listing_field': '1', 'no_listing_link': 'on'})
        prop.refresh_from_db()
        self.assertTrue(prop.no_listing_link)
        self.assertEqual(listings.attention()['places_without_links'], 0)

    def test_only_the_owner_sees_the_numbers_and_strips(self):
        ContactImportCandidate.objects.create(source='quo', name='Someone New')
        other = get_user_model().objects.create_user('staffer', password='pw12345678')
        _Staff.objects.create(user=other)
        self.assertEqual(nudges.nav_counts(other), {})
        self.client.force_login(other)
        self.assertNotContains(self.client.get(reverse('contact_list')), 'waiting for a yes or no')


@override_settings(STORAGES=NO_MANIFEST)
class WeekBoardNameColumnTests(TestCase):
    """On-Site > Turnovers: the first column shows the property name with its unit under it, not a squeezed one-line label."""
    def test_the_name_and_the_unit_are_both_there(self):
        user = get_user_model().objects.create_user('owner2', password='pw12345678')
        _Staff.objects.create(user=user, is_company_admin=True, new_face=True)
        self.client.force_login(user)
        prop = Property.objects.create(name="Joe's", property_type=Property.Type.SHORT_TERM_RENTAL, city='Delray Beach')
        Unit.objects.create(property=prop, label='Blue Ocean (710 NE 7th Ct)')
        r = self.client.get(reverse('onsite_turnovers') + '?all=1')           # a quiet unit is hidden unless asked for
        self.assertContains(r, 'grid-template-columns: 13.5rem')
        self.assertContains(r, 'Joe&#x27;s</a><span class="un">Blue Ocean (710 NE 7th Ct)</span>')
