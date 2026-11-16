import io
import urllib.error
from datetime import date, timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import closing, ledger, sunbiz
from .tests import make_assoc

STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}
LEGAL = 'Test Condos Association, Inc.'


class HandCheckTests(TestCase):
    def setUp(self):
        self.prop = make_assoc('Test Condos')
        self.profile = ledger.profile_for(self.prop)
        self.profile.legal_name = LEGAL
        self.profile.save()

    def test_a_hand_check_records_today_and_clears_old_alerts(self):
        self.profile.sunbiz_alerts, self.profile.sunbiz_checked_on, self.profile.sunbiz_checked_by = ['The state lists this association as Inactive.'], date(2026, 1, 5), 'auto'
        self.profile.save()
        alerts = sunbiz.hand_check(self.profile, today=date(2026, 11, 16))
        self.profile.refresh_from_db()
        self.assertEqual((self.profile.sunbiz_checked_on, self.profile.sunbiz_checked_by, self.profile.sunbiz_alerts, alerts), (date(2026, 11, 16), 'hand', [], []))

    def test_what_the_person_saw_raises_the_right_alerts(self):
        alerts = sunbiz.hand_check(self.profile, today=date(2026, 11, 16), status='Inactive', agent_ok=False, annual_report_filed=date(2025, 4, 1))
        self.assertEqual(len(alerts), 3)
        self.assertTrue(any('registered agent' in a for a in alerts))
        self.assertTrue(any('Inactive' in a for a in alerts))
        self.assertTrue(any('annual report' in a for a in alerts))
        again = sunbiz.hand_check(self.profile, today=date(2026, 11, 17), status='Active', agent_ok=True, annual_report_filed=date(2026, 4, 1))
        self.assertEqual(again, [])
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.registered_agent_is_us())

    def test_leaving_things_out_never_raises_an_alert(self):
        self.assertEqual(sunbiz.hand_check(self.profile, today=date(2026, 11, 16)), [])

    def test_needs_check_after_ninety_days_or_never(self):
        today = date(2026, 11, 16)
        self.assertTrue(sunbiz.needs_check(self.profile, today))
        self.profile.sunbiz_checked_on = today - timedelta(days=90)
        self.assertFalse(sunbiz.needs_check(self.profile, today))
        self.profile.sunbiz_checked_on = today - timedelta(days=91)
        self.assertTrue(sunbiz.needs_check(self.profile, today))


class RefusalTests(TestCase):
    def setUp(self):
        self.prop = make_assoc('Test Condos')
        self.profile = ledger.profile_for(self.prop)
        self.profile.legal_name, self.profile.state_doc_number = LEGAL, 'N12345'
        self.profile.sunbiz_checked_on, self.profile.sunbiz_checked_by, self.profile.sunbiz_alerts = date(2026, 11, 1), 'hand', ['Look at the agent.']
        self.profile.save()

    def test_403_and_429_are_a_refusal_other_errors_are_not(self):
        for code, refused in ((403, True), (429, True), (500, False)):
            err = urllib.error.HTTPError('https://search.sunbiz.org', code, 'x', {}, None)
            with mock.patch('urllib.request.urlopen', side_effect=err):
                with self.assertRaises(sunbiz.SunbizError) as cm:
                    sunbiz.fetch('https://search.sunbiz.org/x')
            self.assertEqual(isinstance(cm.exception, sunbiz.SunbizRefused), refused, code)

    def test_a_refused_lookup_records_nothing_and_a_hand_check_stays(self):
        def refuse(doc, name):
            raise sunbiz.SunbizRefused('SunBiz refused the lookup (HTTP 403).')
        ok, message = sunbiz.check(self.profile, fetcher=refuse)
        self.assertEqual((ok, message), (False, sunbiz.REFUSED_NOTE))
        self.profile.refresh_from_db()
        self.assertEqual((self.profile.sunbiz_checked_on, self.profile.sunbiz_checked_by, self.profile.sunbiz_alerts), (date(2026, 11, 1), 'hand', ['Look at the agent.']))

    def test_the_weekly_command_says_it_once_and_stops_asking(self):
        other = make_assoc('Second Condos')
        p2 = ledger.profile_for(other)
        p2.legal_name, p2.state_doc_number = 'Second Condos Association, Inc.', 'N999'
        p2.save()
        out = io.StringIO()
        with mock.patch('accounting.sunbiz.lookup', side_effect=sunbiz.SunbizRefused('403')) as lookup, mock.patch('time.sleep'):
            call_command('check_sunbiz', stdout=out)
        self.assertEqual(lookup.call_count, 1)
        self.assertEqual(out.getvalue().strip(), sunbiz.REFUSED_NOTE)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.sunbiz_checked_by, 'hand')

    def test_a_working_lookup_still_updates_the_record(self):
        rec = {'name': LEGAL, 'doc': 'N12345', 'ein': '', 'status': 'Active', 'filed': None, 'principal': '', 'mailing': '', 'agent': 'Proper Realty Co., 1 Main St', 'agent_name': 'Proper Realty Co.',
               'officers': [], 'annual': timezone.localdate()}
        ok, message = sunbiz.check(self.profile, fetcher=lambda doc, name: rec)
        self.assertTrue(ok)
        self.profile.refresh_from_db()
        self.assertEqual((self.profile.sunbiz_checked_by, self.profile.sunbiz_alerts), ('auto', []))


@override_settings(STORAGES=STORAGES)
class ScreenTests(TestCase):
    def setUp(self):
        self.prop = make_assoc('Test Condos')
        self.profile = ledger.profile_for(self.prop)
        self.user = get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678')
        self.client.force_login(self.user)
        self.url = reverse('accounting:settings', args=[self.prop.pk])

    def test_the_settings_card_has_the_open_button_in_a_new_tab_and_the_i_checked_it_button(self):
        self.profile.legal_name = LEGAL
        self.profile.save()
        html = self.client.get(self.url).content.decode()
        self.assertIn('id="official"', html)
        self.assertIn('Open on SunBiz', html)
        self.assertIn('target="_blank" rel="noopener"', html)
        self.assertIn('I checked it', html)
        self.assertIn('Never checked', html)
        self.assertIn('SunBiz does not allow automatic lookups', html)
        self.assertNotIn('Check now', html)

    def test_without_a_legal_name_the_card_asks_for_it_and_has_no_link(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn('Fill in the legal name first', html)
        self.assertNotIn('search.sunbiz.org', html)

    def test_pressing_i_checked_it_records_the_check_and_says_so(self):
        self.profile.legal_name = LEGAL
        self.profile.save()
        resp = self.client.post(self.url, {'action': 'sunbiz_hand'}, follow=True)
        self.profile.refresh_from_db()
        self.assertEqual((self.profile.sunbiz_checked_on, self.profile.sunbiz_checked_by), (timezone.localdate(), 'hand'))
        self.assertContains(resp, 'Marked as checked by hand')
        self.assertContains(resp, 'Checked by hand on')

    def test_what_was_seen_is_kept_and_raises_alerts(self):
        self.client.post(self.url, {'action': 'sunbiz_hand', 'hand_status': 'Inactive', 'hand_agent': 'other', 'hand_annual': '2025-04-01'})
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.state_status, 'Inactive')
        self.assertEqual(self.profile.annual_report_filed, date(2025, 4, 1))
        self.assertTrue(self.profile.sunbiz_alerts)

    def test_saving_the_typed_record_is_not_a_check(self):
        self.client.post(self.url, {'action': 'official', 'legal_name': LEGAL, 'state_doc_number': 'N1'})
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.legal_name, LEGAL)
        self.assertIsNone(self.profile.sunbiz_checked_on)

    def test_the_old_automatic_button_says_it_quietly_instead_of_failing(self):
        self.profile.legal_name, self.profile.state_doc_number = LEGAL, 'N1'
        self.profile.save()
        with mock.patch('accounting.sunbiz.lookup', side_effect=sunbiz.SunbizRefused('403')):
            resp = self.client.post(self.url, {'action': 'sunbiz_check'}, follow=True)
        self.assertContains(resp, sunbiz.REFUSED_NOTE)
        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.sunbiz_checked_on)

    def test_only_an_address_on_this_site_is_followed_after_a_post(self):
        resp = self.client.post(self.url, {'action': 'sunbiz_hand', 'next': '//evil.example/x'})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp['Location'], self.url)
        resp = self.client.post(self.url, {'action': 'sunbiz_hand', 'next': '/properties/'})
        self.assertEqual(resp['Location'], '/properties/')

    def test_the_association_page_card_has_the_buttons_and_no_check_now(self):
        self.profile.legal_name, self.profile.state_doc_number = LEGAL, 'N1'
        self.profile.save()
        html = self.client.get(reverse('property_detail', args=[self.prop.pk])).content.decode()
        self.assertIn('Open on SunBiz', html)
        self.assertIn('I checked it', html)
        self.assertNotIn('Check now', html)


class CloseSheetItemTests(TestCase):
    def setUp(self):
        self.prop = make_assoc('Test Condos')
        self.profile = ledger.profile_for(self.prop)
        self.today = date(2026, 11, 16)

    def item(self):
        sheet = closing.close_sheet(self.prop, date(2026, 10, 1), today=self.today)
        return next((i for i in sheet['items'] if i['key'] == 'state'), None)

    def test_no_legal_name_and_no_alerts_means_no_item(self):
        self.assertIsNone(self.item())

    def test_never_checked_says_check_it_by_hand(self):
        self.profile.legal_name = LEGAL
        self.profile.save()
        item = self.item()
        self.assertEqual(item['status'], 'attention')
        self.assertIn('Check it by hand', item['detail'])
        self.assertTrue(item['url'].endswith('#official'))
        self.assertFalse(item['blocks'])

    def test_checked_recently_is_done_and_ninety_one_days_ago_is_not(self):
        self.profile.legal_name = LEGAL
        self.profile.sunbiz_checked_on = timezone.localdate() - timedelta(days=10)
        self.profile.save()
        self.assertEqual(self.item()['status'], 'done')
        self.profile.sunbiz_checked_on = timezone.localdate() - timedelta(days=100)
        self.profile.save()
        item = self.item()
        self.assertEqual(item['status'], 'attention')
        self.assertIn('Check it by hand', item['detail'])

    def test_alerts_still_show_first(self):
        self.profile.legal_name, self.profile.sunbiz_alerts = LEGAL, ['The registered agent is Someone, not Proper Realty Co.']
        self.profile.save()
        item = self.item()
        self.assertEqual(item['status'], 'attention')
        self.assertIn('registered agent', item['detail'])
