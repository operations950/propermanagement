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
    return StaffProfile.objects.create(user=user, role=StaffProfile.Role.ACCOUNTING, is_company_admin=True)


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

    def test_the_checklist_has_thirteen_steps_in_four_days(self):
        from .close_views import close_steps
        steps = close_steps(date(2026, 10, 1), date(2026, 11, 3))
        self.assertEqual([s['n'] for s in steps], list(range(1, 14)))
        self.assertEqual([s['day'] for s in steps], [1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 4])
        self.assertEqual([s['title'] for s in steps][6:], ['Pay owners', 'Pay Proper', 'Tourist tax', 'Pay sales tax', 'Long-term rent collection', 'Close associations', 'Pay long-term owners'])
        self.assertEqual(steps[0]['due'], date(2026, 11, 2))
        self.assertEqual(steps[6]['due'], date(2026, 11, 3))
        self.assertEqual(steps[12]['due'], date(2026, 11, 5))
        self.assertEqual(steps[6]['deadline'], date(2026, 11, 5))

    def test_each_step_becomes_its_own_compliance_item_on_its_day(self):
        from worksessions.models import Session
        with mock.patch.object(month_close_project, 'workdays', workdays):
            made = month_close_project.ensure_items(date(2026, 10, 1), date(2026, 11, 3))
        self.assertEqual(made, 13)
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

    def test_the_pages_open(self):
        for name in ('close_home', 'close_rent_collection', 'close_sales_tax', 'close_pay_ltr_owners', 'close_pay_owners'):
            self.assertEqual(self.client.get(reverse(name) + '?month=2026-10').status_code, 200, name)
        html = self.client.get(reverse('close_home') + '?month=2026-10').content.decode()
        self.assertIn('Day 1', html)
        self.assertIn('Day 4', html)
        self.assertIn('Pay long-term owners', html)

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
