"""The last step of the tourist tax: pay the county and record the amount and the confirmation number. Filing is not paying."""
from datetime import date
from decimal import Decimal as D
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import month_close_project, tourist_tax
from .close_views import _tourist_tax_step, close_steps
from .models import TouristTaxFiling, TouristTaxPayment
from .test_tax_and_calendar import NO_MANIFEST, _rep, _str
from .tests import make_staff

MONTH = date(2026, 10, 1)


@override_settings(STORAGES=NO_MANIFEST)
class PayTheCountyTests(TestCase):
    def setUp(self):
        make_staff()
        self.user = get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678')
        self.client.force_login(self.user)
        self.a = _str('800 Tropic', tdt_account_number='TDT-111')
        self.b = _str('702 SW 1st', tdt_account_number='TDT-222')
        self.taxable = [(self.a, D('1192.70')), (self.b, D('2500.00'))]
        self.url = reverse('tourist_tax') + '?month=2026-10'
        self.patch = mock.patch.object(tourist_tax, 'report', side_effect=lambda month: self.rep())
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def rep(self):
        return tourist_tax.attach_payment(_rep(self.taxable))

    def due(self):
        return self.rep()['due']

    def file_all(self):
        for row in self.rep()['rows']:
            TouristTaxFiling.objects.update_or_create(month=MONTH, property=row['property'], defaults={'amount': row['due'], 'filed_on': date(2026, 11, 3)})

    def pay(self, **kw):
        data = {'action': 'record_payment', 'amount_paid': str(self.due()), 'paid_on': '2026-11-05', 'confirmation': 'TDT-998877', 'reason': ''}
        data.update(kw)
        return self.client.post(self.url, data, follow=True)

    # ------------------------------------------------------------ where the month stands
    def test_nothing_filed_yet_is_not_started(self):
        self.assertEqual(self.rep()['pay_state'], 'none')
        self.assertEqual(_tourist_tax_step(MONTH)[0], 'not_started')

    def test_some_returns_filed_is_a_count_and_payment_cannot_be_recorded_yet(self):
        TouristTaxFiling.objects.create(month=MONTH, property=self.a, amount=self.rep()['rows'][0]['due'], filed_on=date(2026, 11, 3))
        self.assertEqual(self.rep()['pay_state'], 'partial')
        self.assertEqual(_tourist_tax_step(MONTH), (None, '1 of 2 filed.'))
        r = self.pay()
        self.assertContains(r, 'Mark every return as filed first')
        self.assertFalse(TouristTaxPayment.objects.exists())

    def test_filed_but_not_paid_keeps_the_step_open_and_says_so(self):
        self.file_all()
        self.assertEqual(self.rep()['pay_state'], 'unpaid')
        status, detail = _tourist_tax_step(MONTH)
        self.assertEqual(status, 'attention')
        self.assertIn('payment not recorded', detail)
        page = self.client.get(self.url).content.decode()
        self.assertIn('Filed but not paid.', page)
        self.assertIn('Filed, not paid', page)
        self.assertIn('id="pay-county"', page)
        self.assertIn('Record the payment', page)

    def test_the_close_project_item_stays_open_until_the_payment_is_recorded(self):
        from worksessions.models import Session
        month_close_project.ensure_items(MONTH, date(2026, 11, 3))
        self.file_all()
        step = next(s for s in close_steps(MONTH, date(2026, 11, 3)) if s['title'] == 'Tourist tax')
        self.assertEqual(step['status'], 'attention')
        month_close_project.finish_step(9, MONTH, step['status'])
        item = Session.objects.get(template__name='Month-end close: 9. Tourist tax', notes='close-month:2026-10')
        self.assertNotEqual(item.status, Session.Status.SUBMITTED)
        self.pay()
        step = next(s for s in close_steps(MONTH, date(2026, 11, 3)) if s['title'] == 'Tourist tax')
        self.assertEqual(step['status'], 'done')
        month_close_project.finish_step(9, MONTH, step['status'])
        item.refresh_from_db()
        self.assertEqual(item.status, Session.Status.SUBMITTED)

    # ------------------------------------------------------------ recording it
    def test_recording_the_payment_completes_the_step(self):
        self.file_all()
        r = self.pay()
        self.assertContains(r, 'Payment recorded')
        pay = TouristTaxPayment.objects.get()
        self.assertEqual((pay.month, pay.amount_paid, pay.amount_due, pay.confirmation, pay.paid_on, pay.recorded_by), (MONTH, self.due(), self.due(), 'TDT-998877', date(2026, 11, 5), self.user))
        self.assertEqual(self.rep()['pay_state'], 'paid')
        status, detail = _tourist_tax_step(MONTH)
        self.assertEqual(status, 'done')
        self.assertIn('TDT-998877', detail)
        page = self.client.get(self.url).content.decode()
        self.assertIn('confirmation <strong>TDT-998877</strong>', page)
        self.assertNotIn('Filed but not paid.', page)

    def test_one_payment_covers_every_return_of_the_month(self):
        self.file_all()
        self.pay()
        self.assertEqual(TouristTaxPayment.objects.count(), 1)
        self.assertEqual(TouristTaxPayment.objects.get().amount_paid, sum((r['due'] for r in self.rep()['rows']), D('0')))

    def test_the_confirmation_number_is_required(self):
        self.file_all()
        r = self.pay(confirmation='  ')
        self.assertContains(r, 'Enter the payment confirmation number')
        self.assertFalse(TouristTaxPayment.objects.exists())
        self.assertEqual(self.rep()['pay_state'], 'unpaid')

    def test_an_amount_that_differs_needs_a_reason(self):
        self.file_all()
        r = self.pay(amount_paid=str(self.due() + D('5.00')))
        self.assertContains(r, 'Say in a few words why they differ')
        self.assertFalse(TouristTaxPayment.objects.exists())
        self.pay(amount_paid=str(self.due() + D('5.00')), reason='County added a $5 late charge')
        pay = TouristTaxPayment.objects.get()
        self.assertEqual(pay.amount_paid, self.due() + D('5.00'))
        self.assertEqual(pay.reason, 'County added a $5 late charge')
        self.assertEqual(self.rep()['pay_state'], 'paid')                       # paid is paid; the amount due is unchanged

    def test_a_reason_is_dropped_when_the_amount_matches(self):
        self.file_all()
        self.pay(reason='left over text')
        self.assertEqual(TouristTaxPayment.objects.get().reason, '')

    def test_a_bad_or_zero_amount_is_refused(self):
        self.file_all()
        for bad in ('', 'abc', '0', '-5'):
            self.assertContains(self.pay(amount_paid=bad), 'Enter the amount you paid the county')
        self.assertFalse(TouristTaxPayment.objects.exists())

    def test_the_date_defaults_to_today_when_it_is_not_a_date(self):
        self.file_all()
        self.pay(paid_on='soon')
        self.assertEqual(TouristTaxPayment.objects.get().paid_on, timezone.localdate())

    # ------------------------------------------------------------ correcting and clearing
    def test_a_correction_keeps_who_and_when(self):
        self.file_all()
        self.pay()
        self.pay(confirmation='TDT-111222', paid_on='2026-11-06')
        pay = TouristTaxPayment.objects.get()
        self.assertEqual((pay.confirmation, pay.paid_on), ('TDT-111222', date(2026, 11, 6)))
        self.assertEqual([h['action'] for h in pay.history], ['recorded', 'corrected'])
        self.assertEqual(pay.history[0]['confirmation'], 'TDT-998877')
        self.assertTrue(all(h['by'] for h in pay.history))
        self.assertEqual(self.rep()['pay_state'], 'paid')

    def test_clearing_the_record_opens_the_step_again_and_keeps_the_row(self):
        self.file_all()
        self.pay()
        r = self.client.post(self.url, {'action': 'clear_payment'}, follow=True)
        self.assertContains(r, 'payment record was cleared')
        pay = TouristTaxPayment.objects.get()
        self.assertIsNotNone(pay.cleared_at)
        self.assertEqual(pay.cleared_by, self.user)
        self.assertEqual([h['action'] for h in pay.history], ['recorded', 'cleared'])
        self.assertEqual(self.rep()['pay_state'], 'unpaid')
        self.assertEqual(_tourist_tax_step(MONTH)[0], 'attention')
        self.pay(confirmation='TDT-333444')                                     # recording again reuses the same row and keeps its history
        pay.refresh_from_db()
        self.assertEqual((TouristTaxPayment.objects.count(), pay.confirmation, pay.cleared_at), (1, 'TDT-333444', None))
        self.assertEqual([h['action'] for h in pay.history], ['recorded', 'cleared', 'recorded'])

    def test_clearing_when_nothing_is_recorded_says_so(self):
        self.file_all()
        self.assertContains(self.client.post(self.url, {'action': 'clear_payment'}, follow=True), 'No payment is recorded')

    # ------------------------------------------------------------ when the figures move
    def test_a_change_in_the_amount_due_after_paying_reopens_the_step(self):
        self.file_all()
        self.pay()
        self.taxable = [(self.a, D('1192.70')), (self.b, D('2600.00'))]         # a late deposit was matched
        self.assertEqual(self.rep()['pay_state'], 'changed')                    # the return was filed for the old amount
        self.file_all()                                                         # filed again for the new one
        self.assertEqual(self.rep()['pay_state'], 'paid_changed')
        status, detail = _tourist_tax_step(MONTH)
        self.assertEqual(status, 'attention')
        self.assertIn('amount due is now', detail)
        page = self.client.get(self.url).content.decode()
        self.assertIn('The amount due changed after the payment was recorded.', page)
        self.pay()
        self.assertEqual(self.rep()['pay_state'], 'paid')
        self.assertEqual(TouristTaxPayment.objects.get().amount_due, self.due())

    def test_nothing_due_needs_no_payment(self):
        self.taxable = [(self.a, D('0.00'))]
        self.file_all()
        self.assertEqual(self.rep()['pay_state'], 'nothing')
        self.assertEqual(_tourist_tax_step(MONTH)[0], 'done')
        self.assertContains(self.client.get(self.url), 'nothing is due, so there is no payment to make')

    def test_only_an_admin_can_record_a_payment(self):
        self.file_all()
        self.client.force_login(get_user_model().objects.create_user('helper', 'h@example.com', 'pw12345678'))
        self.pay()
        self.assertFalse(TouristTaxPayment.objects.exists())
