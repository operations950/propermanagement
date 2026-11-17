"""The late fees screen after the charges: owners stay listed (charged), the step is closed by a person's 'mark done', Up goes to the close sheet when opened from it, and a general
placeholder property never appears on the close lists."""
from datetime import date
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import Property

from . import closing, ledger
from .models import Charge, CloseStepDone
from .test_late_fees_asof import OCT1, OCT31, STORAGES, AsOfBase
from .tests import D, make_assoc
from .views_collections import late_fee_data

TODAY = date(2026, 11, 12)


@override_settings(STORAGES=STORAGES)
class LateFeesAfterTheChargesTests(AsOfBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.url = reverse('accounting:late_fees', args=[self.prop.pk])

    def get(self, qs=''):
        with mock.patch('django.utils.timezone.localdate', return_value=TODAY):
            return self.client.get(self.url + qs)

    def post(self, data):
        with mock.patch('django.utils.timezone.localdate', return_value=TODAY):
            return self.client.post(self.url, data, follow=True)

    def charge_everyone(self):
        self.post({'action': 'charge_late', 'as_of': '2026-10-31', 'post_on': '2026-10-31'})
        self.post({'action': 'charge_interest', 'as_of': '2026-10-31', 'post_on': '2026-10-31'})

    def test_a_charged_owner_stays_on_the_screen_with_a_charged_chip_and_no_tick_box(self):
        self.charge_everyone()
        r = self.get('?as_of=2026-10-31&month=2026-10')
        self.assertEqual(sorted(x['owner'].name for x in r.context['rows']), ['Anna', 'Cora', 'Dan'])
        html = r.content.decode()
        self.assertIn('Charged', html)
        self.assertIn('Late fee $30.75 on Oct 31', html)
        self.assertIn('Interest $9.10 on Oct 31', html)
        self.assertNotIn('class="lf-pick"', html)                                      # nothing ready, so nothing to tick
        self.assertIn('Also owners who are not late', html)

    def test_the_total_owed_and_last_charged_still_show_for_a_charged_owner(self):
        self.charge_everyone()
        row = {x['owner'].name: x for x in late_fee_data(self.prop, OCT31)}['Anna']
        self.assertEqual((row['principal'], row['late_fees'], row['interest']), (D('615.00'), D('30.75'), D('9.10')))
        self.assertEqual(row['total'], D('654.85'))
        self.assertEqual(row['last_charged'], OCT31)

    def test_pressing_charge_again_with_nobody_ticked_charges_nobody_a_second_time(self):
        self.charge_everyone()
        before = Charge.objects.filter(kind__in=['late_fee', 'interest']).count()
        r = self.post({'action': 'charge_late', 'as_of': '2026-10-31', 'post_on': '2026-10-31'})
        self.assertContains(r, 'nothing was charged')
        self.assertEqual(Charge.objects.filter(kind__in=['late_fee', 'interest']).count(), before)

    def test_with_none_ticked_only_owners_with_something_ready_are_charged(self):
        self.post({'action': 'charge_late', 'as_of': '2026-10-31', 'post_on': '2026-10-31', 'unit': [self.anna_u.pk]})
        r = self.post({'action': 'charge_late', 'as_of': '2026-10-31', 'post_on': '2026-10-31'})
        self.assertContains(r, 'Charged 2 late fees')                                  # Cora and Dan: Anna already had hers
        self.assertEqual(Charge.objects.filter(kind='late_fee').count(), 3)

    def test_the_checkbox_lists_an_owner_who_is_not_late_but_owes_something(self):
        r = self.get('?as_of=2026-10-05&all=1')
        rows = {x['owner'].name: x for x in r.context['rows']}
        self.assertEqual(sorted(rows), ['Anna', 'Ben', 'Cora', 'Dan'])                 # nobody is late on the 5th (grace to the 11th), but they owe
        self.assertFalse(any(x['late'] for x in rows.values()))
        self.assertContains(r, 'Not late on')
        self.assertEqual(late_fee_data(self.prop, date(2026, 10, 5)), [])


@override_settings(STORAGES=STORAGES)
class MarkTheStepDoneTests(AsOfBase):
    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678', first_name='Pat')
        self.client.force_login(self.user)
        self.url = reverse('accounting:late_fees', args=[self.prop.pk])

    def post(self, data):
        with mock.patch('django.utils.timezone.localdate', return_value=TODAY):
            return self.client.post(self.url, data, follow=True)

    def step(self):
        sheet = closing.close_sheet(self.prop, OCT1, today=TODAY)
        return {i['key']: i for i in sheet['items']}['late_fees']

    def test_the_step_stays_open_until_a_person_marks_it_done(self):
        self.assertEqual(self.step()['status'], 'attention')
        self.post({'action': 'charge_late', 'as_of': '2026-10-31', 'post_on': '2026-10-31'})
        self.post({'action': 'charge_interest', 'as_of': '2026-10-31', 'post_on': '2026-10-31'})
        step = self.step()
        self.assertEqual(step['status'], 'attention')                                   # everyone charged, but a person has not said it is finished
        self.assertIn('all charged', step['detail'])

    def test_marking_it_done_needs_the_box_ticked_and_charges_nothing(self):
        r = self.post({'action': 'mark_done', 'as_of': '2026-10-31', 'month': '2026-10'})
        self.assertContains(r, 'Tick the box')
        self.assertFalse(CloseStepDone.objects.exists())
        r = self.post({'action': 'mark_done', 'confirm': '1', 'as_of': '2026-10-31', 'month': '2026-10'})
        self.assertContains(r, 'marked done for October 2026')
        done = CloseStepDone.objects.get()
        self.assertEqual((done.association, done.month, done.key, done.marked_by), (self.prop, OCT1, 'late_fees', self.user))
        self.assertFalse(Charge.objects.filter(kind__in=['late_fee', 'interest']).exists())          # owners left uncharged on purpose
        step = self.step()
        self.assertEqual(step['status'], 'done')
        self.assertIn('Marked done', step['detail'])

    def test_it_can_be_done_with_owners_still_uncharged_and_undone(self):
        self.post({'action': 'mark_done', 'confirm': '1', 'as_of': '2026-10-31', 'month': '2026-10'})
        self.assertEqual(self.step()['status'], 'done')
        r = self.post({'action': 'undo_done', 'as_of': '2026-10-31', 'month': '2026-10'})
        self.assertContains(r, 'open again')
        self.assertFalse(CloseStepDone.objects.exists())
        self.assertEqual(self.step()['status'], 'attention')

    def test_the_month_falls_back_to_the_month_of_the_as_of_day(self):
        self.post({'action': 'mark_done', 'confirm': '1', 'as_of': '2026-10-31'})
        self.assertEqual(CloseStepDone.objects.get().month, OCT1)

    def test_marking_it_twice_keeps_one_record(self):
        for _ in range(2):
            self.post({'action': 'mark_done', 'confirm': '1', 'as_of': '2026-10-31', 'month': '2026-10'})
        self.assertEqual(CloseStepDone.objects.count(), 1)

    def test_a_month_where_nobody_is_late_needs_no_marking(self):
        sheet = closing.close_sheet(self.prop, date(2026, 9, 1), today=TODAY)           # nothing was due yet on September 30
        step = {i['key']: i for i in sheet['items']}['late_fees']
        self.assertEqual(step['status'], 'done')
        self.assertIn('Nobody is late', step['detail'])

    def test_the_marked_done_panel_shows_who_and_when(self):
        self.post({'action': 'mark_done', 'confirm': '1', 'as_of': '2026-10-31', 'month': '2026-10'})
        with mock.patch('django.utils.timezone.localdate', return_value=TODAY):
            html = self.client.get(self.url + '?as_of=2026-10-31&month=2026-10').content.decode()
        self.assertIn('October 2026, marked', html)
        self.assertIn('Pat', html)
        self.assertIn('Undo', html)


@override_settings(STORAGES=STORAGES)
class UpFromTheCloseSheetTests(AsOfBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.url = reverse('accounting:late_fees', args=[self.prop.pk])
        self.close = reverse('accounting:close', args=[self.prop.pk, '2026-10'])
        self.hub = reverse('accounting:hub', args=[self.prop.pk])

    def get(self, url):
        with mock.patch('django.utils.timezone.localdate', return_value=TODAY):
            return self.client.get(url, HTTP_ACCEPT='text/html')

    def test_a_fresh_session_opened_from_the_close_sheet_link_still_goes_up_to_the_close_sheet(self):
        r = self.get(self.url + '?as_of=2026-10-31&month=2026-10')                     # no trail at all (the first page after a deploy, a stale tab)
        self.assertEqual((r.context['acc_back'], r.context['acc_back_label']), (self.close, 'Month-end close'))

    def test_without_a_month_the_default_is_accounting(self):
        r = self.get(self.url)
        self.assertEqual((r.context['acc_back'], r.context['acc_back_label']), (self.hub, 'Accounting'))

    def test_the_trail_still_wins_over_the_default(self):
        self.get(self.hub)
        r = self.get(self.url + '?as_of=2026-10-31&month=2026-10')
        self.assertEqual((r.context['acc_back'], r.context['acc_back_label']), (self.hub, 'Accounting'))

    def test_a_bad_month_value_falls_back_to_accounting(self):
        r = self.get(self.url + '?month=2026-13')
        self.assertEqual(r.context['acc_back'], self.hub)


@override_settings(STORAGES=STORAGES)
class GeneralPlaceholdersAreNotOnTheCloseListsTests(TestCase):
    def setUp(self):
        self.prop = make_assoc()
        self.general = Property.objects.create(name='Commercial (general)', property_type=Property.Type.COMMERCIAL)
        flagged = Property.objects.create(name='Odds and ends', property_type=Property.Type.COMMERCIAL, is_general=True)
        for p in (self.general, flagged):
            prof = ledger.profile_for(p)
            prof.we_do_financials = True
            prof.save()

    def test_the_placeholders_are_not_in_the_list_of_properties_whose_books_we_keep(self):
        self.assertEqual([p.property.name for p in closing.associations()], ['Test Condos'])
        self.assertEqual([p.property.name for p in closing.associations(commercial=False)], ['Test Condos'])

    def test_they_do_not_count_in_n_of_m_closed(self):
        status, detail = closing.checklist_step(date(2026, 10, 1))
        self.assertEqual(detail, '0 of 1 closed')                                     # the two placeholders are not counted

    def test_the_close_home_page_does_not_list_them(self):
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        with mock.patch('django.utils.timezone.localdate', return_value=TODAY):
            html = self.client.get(reverse('accounting:home') + '?month=2026-10').content.decode()
        self.assertIn('Test Condos', html)
        self.assertNotIn('Commercial (general)', html)
        self.assertNotIn('Odds and ends', html)
