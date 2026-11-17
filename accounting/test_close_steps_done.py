"""Owner charges, recurring bills and recurring entries on the close sheet: each is done only when you have marked it (after every bill and entry is posted or skipped), from the top right of its screen."""
from datetime import date
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import Contact

from . import closing, ledger, payables, scheduled
from .models import CloseStepDone, JournalRule, RecurringBill, RecurringBillSkip
from .test_collections_close import STORAGES
from .tests import D, make_assoc

MONTH = date(2026, 10, 1)
TODAY = date(2026, 11, 10)


@override_settings(STORAGES=STORAGES)
class StepsNeedYourWordTests(TestCase):
    def setUp(self):
        self.prop = make_assoc()
        self.user = get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678', first_name='Justin')
        self.client.force_login(self.user)
        self.vendor = Contact.objects.create(name='Pool Co')
        self.bill = RecurringBill.objects.create(association=self.prop, vendor=self.vendor, description='Pool service', account=ledger.account(self.prop, '6410'), amount=D('300'), next_date=date(2026, 10, 9))
        self.url = reverse('accounting:recurring', args=[self.prop.pk])

    def items(self):
        return {i['key']: i for i in closing.close_sheet(self.prop, MONTH, today=TODAY)['items']}

    # ---- recurring bills: post or skip, then mark done
    def test_an_unposted_fixed_bill_must_be_posted_or_skipped(self):
        item = self.items()['recurring']
        self.assertEqual(item['status'], 'attention')
        self.assertIn('Post or skip each fixed bill', item['detail'])

    def test_skipping_a_bill_remembers_it_and_does_not_post_it(self):
        with mock.patch('django.utils.timezone.localdate', return_value=TODAY):
            self.client.post(self.url, {'action': 'skip_bill', 'rec': self.bill.pk, 'month': '2026-10'})
        self.assertTrue(RecurringBillSkip.objects.filter(recurring=self.bill, month=MONTH).exists())
        self.bill.refresh_from_db()
        self.assertGreater(self.bill.next_date, date(2026, 10, 31))                    # the schedule moved on past October
        self.assertEqual(self.bill.bills.count() if hasattr(self.bill, 'bills') else 0, 0)
        row = [r for r in payables.recurring_status(self.prop, MONTH) if r['rec'].pk == self.bill.pk][0]
        self.assertEqual((row['posted'], row['skipped'], row['due']), (False, True, True))

    def test_a_skipped_bill_still_needs_the_step_marked_and_then_it_is_done(self):
        payables.skip_recurring(self.bill, MONTH, self.user)
        item = self.items()['recurring']
        self.assertEqual(item['status'], 'attention')
        self.assertIn('mark the step done', item['detail'])
        with mock.patch('django.utils.timezone.localdate', return_value=TODAY):
            r = self.client.post(self.url, {'action': 'mark_done', 'tab': 'bills', 'month': '2026-10'}, follow=True)
        self.assertContains(r, 'Marked done for October 2026')
        done = self.items()['recurring']
        self.assertEqual(done['status'], 'done')
        self.assertIn('Marked done', done['detail'])

    def test_it_cannot_be_marked_while_a_fixed_bill_is_still_open(self):
        with mock.patch('django.utils.timezone.localdate', return_value=TODAY):
            r = self.client.post(self.url, {'action': 'mark_done', 'tab': 'bills', 'month': '2026-10'}, follow=True)
        self.assertContains(r, 'Post or skip each fixed bill first: Pool service')
        self.assertFalse(CloseStepDone.objects.filter(key='recurring').exists())

    def test_a_month_with_no_fixed_bills_is_done_by_itself(self):
        self.bill.delete()
        self.assertEqual(self.items()['recurring']['status'], 'done')

    # ---- recurring journal entries: post or skip every one, then mark done
    def make_rule(self):
        rule = JournalRule.objects.create(association=self.prop, kind='entry', name='Loan payment', day_of_month=13)
        scheduled.save_entry_rule(self.prop, 'Loan payment', 13, [{'account': ledger.account(self.prop, '6410'), 'side': 'debit', 'amount': D('100')}, {'account': ledger.account(self.prop, '1110'), 'side': 'credit', 'amount': D('100')}], rule=rule)
        return rule

    def test_an_entry_not_posted_or_skipped_is_open_and_blocks_the_mark(self):
        self.make_rule()
        item = self.items()['entries']
        self.assertEqual(item['status'], 'attention')
        self.assertIn('post or skip it', item['detail'])
        with mock.patch('django.utils.timezone.localdate', return_value=TODAY):
            r = self.client.post(self.url, {'action': 'mark_done', 'tab': 'entries', 'month': '2026-10'}, follow=True)
        self.assertContains(r, 'Post or skip each recurring entry first')

    def test_skipping_the_entry_lets_you_mark_the_step_done(self):
        rule = self.make_rule()
        scheduled.skip_rule(self.prop, rule, MONTH, self.user)
        self.assertIn('Look them over', self.items()['entries']['detail'])
        with mock.patch('django.utils.timezone.localdate', return_value=TODAY):
            self.client.post(self.url, {'action': 'mark_done', 'tab': 'entries', 'month': '2026-10'})
        self.assertEqual(self.items()['entries']['status'], 'done')

    def test_with_no_entries_set_up_the_step_is_done(self):
        self.assertEqual(self.items()['entries']['status'], 'done')

    # ---- owner charges: verified, then marked
    def test_posted_charges_wait_for_you_to_say_you_checked_them(self):
        from .models import ChargeRule
        ChargeRule.objects.create(association=self.prop, name='Monthly assessment', frequency='monthly', first_due=date(2026, 10, 1), amount=D('250'), income_account=ledger.account(self.prop, '4500'))
        with mock.patch.object(closing.charges, 'rule_status', return_value={'pending': 0, 'posted': 5, 'units': 5, 'last_due': MONTH, 'next_due': date(2026, 11, 1)}):
            item = self.items()['charges']
            self.assertEqual(item['status'], 'attention')
            self.assertIn('mark the step done', item['detail'])
            with mock.patch('django.utils.timezone.localdate', return_value=TODAY):
                self.client.post(self.url, {'action': 'mark_done', 'tab': 'charges', 'month': '2026-10'})
            self.assertEqual(self.items()['charges']['status'], 'done')

    def test_the_late_fees_and_interest_card_is_gone_from_the_charges_tab(self):
        html = self.client.get(self.url + '?tab=charges&month=2026-10').content.decode()
        self.assertNotIn('Late fees and interest', html)
        self.assertIn('Mark owner charges done for October 2026', html)

    def test_each_tab_has_its_button_in_the_top_right_and_undo_reopens_it(self):
        for tab, word in (('bills', 'recurring bills'), ('entries', 'recurring entries')):
            html = self.client.get(self.url + f'?tab={tab}&month=2026-10').content.decode()
            self.assertIn(f'Mark {word} done for October 2026', html)
        CloseStepDone.objects.create(association=self.prop, month=MONTH, key='entries', marked_by=self.user)
        self.assertIn('Undo', self.client.get(self.url + '?tab=entries&month=2026-10').content.decode())
        self.client.post(self.url, {'action': 'undo_done', 'tab': 'entries', 'month': '2026-10'})
        self.assertFalse(CloseStepDone.objects.filter(key='entries').exists())
