"""The review-and-close step: it opens the month's financial package, approving it closes the month and records who; the manual steps are marked done from the top right of their own screens."""
from datetime import date
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from . import closing, ledger
from .models import CloseStepDone, MonthLock
from .test_collections_close import STORAGES, CollectionsBase
from .tests import make_assoc

MONTH = date(2026, 10, 1)


@override_settings(STORAGES=STORAGES)
class ReviewAndCloseTests(TestCase):
    def setUp(self):
        self.prop = make_assoc()
        self.user = get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678', first_name='Justin')
        self.client.force_login(self.user)
        self.url = reverse('accounting:close_review', args=[self.prop.pk, '2026-10'])

    def sheet(self):
        return closing.close_sheet(self.prop, MONTH, today=date(2026, 11, 10))

    def items(self):
        return {i['key']: i for i in self.sheet()['items']}

    def test_the_step_opens_the_review_screen_not_the_reports_list(self):
        self.assertEqual(self.items()['lock']['url'], self.url)

    def test_the_screen_shows_the_package_and_only_the_package(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn(reverse('accounting:report_package', args=[self.prop.pk]) + '?month=2026-10', html)
        self.assertIn('Approve and close the month', html)
        self.assertNotIn('Trial balance', html)
        self.assertNotIn('Budget vs actual', html)

    def test_the_package_may_be_shown_inside_the_page(self):
        r = self.client.get(reverse('accounting:report_package', args=[self.prop.pk]) + '?month=2026-10')
        self.assertEqual(r.headers.get('X-Frame-Options'), 'SAMEORIGIN')

    def test_approving_closes_the_month_and_records_who_and_when(self):
        with mock.patch.object(closing, 'close_sheet', return_value={'items': [], 'locked': False, 'sent': False, 'can_lock': True, 'blocking': 0}):
            r = self.client.post(self.url, {'action': 'approve'}, follow=True)
        lock = MonthLock.objects.get(association=self.prop, month=MONTH)
        self.assertEqual(lock.locked_by, self.user)
        self.assertContains(r, 'approved and closed')
        self.assertContains(r, 'Open the month again')
        self.assertContains(r, 'Justin')

    def test_it_cannot_be_approved_while_something_blocking_is_open(self):
        with mock.patch.object(closing, 'close_sheet', return_value={'items': [{'key': 'bank', 'title': 'Bank reconciliation', 'blocks': True, 'status': 'attention'}], 'locked': False, 'sent': False, 'can_lock': False, 'blocking': 1}):
            html = self.client.get(self.url).content.decode()
            self.assertIn('disabled', html.split('Approve and close the month')[0].rsplit('<button', 1)[1])
            r = self.client.post(self.url, {'action': 'approve'}, follow=True)
        self.assertContains(r, 'Finish the items on the close sheet first')
        self.assertFalse(MonthLock.objects.exists())

    def test_when_every_step_before_it_is_done_nothing_is_said_to_be_waiting_and_approve_is_open(self):
        done = {'items': [{'key': 'bank', 'title': 'Bank reconciliation', 'blocks': True, 'status': 'done'},
                          {'key': 'lock', 'title': 'Review and close the month', 'blocks': True, 'status': 'attention'}], 'locked': False, 'sent': False, 'can_lock': True, 'blocking': 0}
        with mock.patch.object(closing, 'close_sheet', return_value=done):
            html = self.client.get(self.url).content.decode()
        self.assertNotIn('still need', html)                                          # the last step is never "waiting" on itself
        self.assertNotIn('disabled', html.split('Approve and close the month')[0].rsplit('<button', 1)[1])

    def test_the_package_opens_at_its_first_page_and_a_little_larger_than_the_width(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn('page=1', html)
        self.assertIn('Date.now()', html)                                              # a fresh address each time: the browser does not reopen it where it was last scrolled
        self.assertIn('* 1.12', html)

    def test_opening_the_month_again_removes_the_approval(self):
        ledger.lock_month(self.prop, MONTH, self.user)
        self.client.post(self.url, {'action': 'reopen'})
        self.assertFalse(MonthLock.objects.exists())

    def test_the_lock_step_says_who_approved_it(self):
        ledger.lock_month(self.prop, MONTH, self.user)
        item = self.items()['lock']
        self.assertEqual(item['status'], 'done')
        self.assertIn('Approved by Justin', item['detail'])

    def test_a_closed_month_still_needs_attention_on_the_list_until_the_financials_are_sent(self):
        ledger.lock_month(self.prop, MONTH, self.user)
        status, detail = closing.association_status(self.prop, MONTH)
        self.assertEqual(status, 'attention')
        self.assertIn('not sent', detail)

    def test_the_close_sheet_has_no_separate_close_button_any_more(self):
        html = self.client.get(reverse('accounting:close', args=[self.prop.pk, '2026-10'])).content.decode()
        self.assertNotIn('value="lock"', html)
        self.assertIn('Review and close the month', html)


@override_settings(STORAGES=STORAGES)
class MarkDoneFromTheTopRightTests(CollectionsBase):
    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678', first_name='Justin')
        self.client.force_login(self.user)
        self.coll = reverse('accounting:collections', args=[self.prop.pk])

    def test_owner_collections_can_be_marked_done_for_the_month_and_undone(self):
        with mock.patch('django.utils.timezone.localdate', return_value=self.today):
            html = self.client.get(self.coll + '?month=2026-10').content.decode()
            self.assertIn('Mark owner collections done for October 2026', html)
            r = self.client.post(self.coll, {'action': 'mark_done', 'month': '2026-10'}, follow=True)
            self.assertContains(r, 'marked done for October 2026')
            done = CloseStepDone.objects.get(association=self.prop, month=date(2026, 10, 1), key='collections')
            self.assertEqual(done.marked_by, self.user)
            item = {i['key']: i for i in closing.close_sheet(self.prop, date(2026, 10, 1), today=self.today)['items']}['collections']
            self.assertEqual(item['status'], 'done')
            self.assertIn('Marked done', item['detail'])
            self.assertIn('?month=2026-10', item['url'])
            self.client.post(self.coll, {'action': 'undo_done', 'month': '2026-10'})
        self.assertFalse(CloseStepDone.objects.filter(key='collections').exists())

    def test_marking_it_done_for_a_month_does_not_mark_another_month(self):
        with mock.patch('django.utils.timezone.localdate', return_value=self.today):
            self.client.post(self.coll, {'action': 'mark_done', 'month': '2026-10'})
            item = {i['key']: i for i in closing.close_sheet(self.prop, date(2026, 9, 1), today=self.today)['items']}['collections']
        self.assertNotEqual(item['detail'][:11], 'Marked done')

    def test_late_fees_has_the_same_button_in_the_same_place(self):
        url = reverse('accounting:late_fees', args=[self.prop.pk])
        with mock.patch('django.utils.timezone.localdate', return_value=self.today):
            html = self.client.get(url + '?month=2026-10').content.decode()
            self.assertIn('Mark late fees and interest done for October 2026', html)
            self.assertNotIn('I have charged late fees and interest to everyone I wish to charge', html)       # the old tick box at the bottom is gone
            r = self.client.post(url, {'action': 'mark_done', 'confirm': '1', 'month': '2026-10', 'as_of': '2026-10-31'}, follow=True)
        self.assertContains(r, 'marked done for October 2026')
        self.assertTrue(CloseStepDone.objects.filter(key='late_fees', month=date(2026, 10, 1)).exists())


@override_settings(STORAGES=STORAGES)
class CloseListOthersTests(TestCase):
    def test_properties_we_keep_the_books_for_are_not_listed_as_ones_we_do_not(self):
        from core.models import Property
        decoy = Property.objects.create(name='Decoy Villas', property_type=Property.Type.ASSOCIATION)      # no books: made first so property ids and profile ids differ
        prop = make_assoc()
        user = get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678')
        self.client.force_login(user)
        r = self.client.get(reverse('accounting:home'))
        others = [p.name for p in r.context['others']]
        self.assertIn('Decoy Villas', others)
        self.assertNotIn(prop.name, others)
        self.assertEqual([x['assoc'].name for x in r.context['rows']], [prop.name])
