"""The Properties screen: a newspaper of small boxes (one for each line of business, as tall as its own list), the Alerts button that holds every property reminder, and no placeholders."""
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from core import property_alerts
from core.models import Property, StaffProfile
from core.views import property_boxes

STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}
T = Property.Type


def prop(name, kind=T.ASSOCIATION, **kw):
    kw.setdefault('street', '1 Main St')
    return Property.objects.create(name=name, property_type=kind, city='Boca Raton', state='FL', zip_code='33432', **kw)


@override_settings(STORAGES=STORAGES)
class TheBoxesTests(TestCase):
    def setUp(self):
        self.boss = get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678')
        StaffProfile.objects.create(user=self.boss, role='admin', is_company_admin=True)
        self.client.force_login(self.boss)
        for name in ('St Andrews Grand', 'La Pensee'):
            prop(name)
        prop('Bethesda Park', is_general=True)                     # a real association that was flagged general by mistake: it is listed like any other
        prop('800 Tropic', T.SHORT_TERM_RENTAL)
        prop('324 Lofts', T.COMMERCIAL)
        prop('1038 E Heritage', T.SNOWBIRD)
        prop('225 NE 1st St', T.LONG_TERM_RENTAL)
        prop('Associations (general)', T.ASSOCIATION, is_general=True)
        prop('Commercial (general)', T.COMMERCIAL, is_general=True)
        prop('Snowbird Oversight (general)', T.SNOWBIRD, is_general=True)
        prop('No specific property', T.GENERAL, is_general=True)
        prop('Proper Realty Co Office 309', T.GENERAL)
        prop('Old Duplex', T.LONG_TERM_RENTAL, is_active=False)

    def boxes(self, query=''):
        return {b['title']: [p.name for p in b['rows']] for b in self.client.get(reverse('property_list') + query).context['boxes']}

    def test_each_line_of_business_is_its_own_box_by_name(self):
        got = self.boxes()
        self.assertEqual(got['Associations'], ['Bethesda Park', 'La Pensee', 'St Andrews Grand'])
        self.assertEqual(got['Short-term rentals'], ['800 Tropic'])
        self.assertEqual(got['Commercial'], ['324 Lofts'])
        self.assertEqual(got['Long-term rentals'], ['225 NE 1st St'])
        self.assertEqual(got['Snowbird oversight'], ['1038 E Heritage'])
        self.assertEqual(got['Other'], ['Proper Realty Co Office 309'])

    def test_the_general_placeholders_are_not_listed_anywhere(self):
        html = self.client.get(reverse('property_list')).content.decode()
        for name in ('Associations (general)', 'Commercial (general)', 'Snowbird Oversight (general)', 'No specific property'):
            self.assertNotIn(name, html)

    def test_a_real_association_flagged_general_is_still_listed(self):
        self.assertIn('Bethesda Park', self.boxes()['Associations'])

    def test_the_boxes_run_in_the_order_associations_str_commercial_ltr_snowbird_other(self):
        self.assertEqual(list(self.boxes()), ['Associations', 'Short-term rentals', 'Commercial', 'Long-term rentals', 'Snowbird oversight', 'Other'])

    def test_an_empty_line_of_business_has_no_box(self):
        Property.objects.filter(property_type=T.SNOWBIRD).delete()
        self.assertNotIn('Snowbird oversight', self.boxes())

    def test_a_property_is_a_compact_row_that_opens_it(self):
        grand = Property.objects.get(name='St Andrews Grand')
        html = self.client.get(reverse('property_list')).content.decode()
        self.assertIn(f'href="{reverse("property_detail", args=[grand.pk])}"', html)
        self.assertEqual(html.count('class="pl-box"'), 6)
        self.assertIn('pl-paper', html)
        self.assertNotIn('<table', html.split('pl-paper')[1])                       # no table, no type or address columns
        self.assertNotIn('All types', html)                                         # the type chips are gone: the box says the type

    def test_inactive_properties_show_only_when_asked_and_in_grey_inside_their_own_box(self):
        self.assertNotIn('Old Duplex', self.boxes()['Long-term rentals'])
        got = self.boxes('?show_inactive=1')
        self.assertIn('Old Duplex', got['Long-term rentals'])
        html = self.client.get(reverse('property_list') + '?show_inactive=1').content.decode()
        self.assertIn('pl-row pl-off', html)

    def test_the_search_filters_the_boxes_and_answers_a_script_with_just_the_boxes(self):
        j = self.client.get(reverse('property_list') + '?q=tropic', HTTP_X_REQUESTED_WITH='XMLHttpRequest').json()
        self.assertIn('800 Tropic', j['paper'])
        self.assertNotIn('St Andrews Grand', j['paper'])
        self.assertNotIn('Associations</span>', j['paper'])

    def test_only_the_rentals_missing_size_details_can_be_asked_for(self):
        self.assertEqual(list(self.boxes('?needs=details')), ['Short-term rentals'])


@override_settings(STORAGES=STORAGES)
class TheAlertsButtonTests(TestCase):
    def setUp(self):
        self.boss = get_user_model().objects.create_superuser('boss2', 'b2@example.com', 'pw12345678')
        StaffProfile.objects.create(user=self.boss, role='admin', is_company_admin=True)
        self.plain_user = get_user_model().objects.create_user('plain', 'p@example.com', 'pw12345678')
        StaffProfile.objects.create(user=self.plain_user, role='property_manager')

    def page(self, user):
        self.client.force_login(user)
        return self.client.get(reverse('property_list'))

    def test_with_nothing_to_look_at_the_button_is_plain_and_has_no_number(self):
        html = self.page(self.boss).content.decode()
        self.assertIn('pl-alerts-btn', html)
        self.assertNotIn('pl-alerts-on', html.split('<button type="button" id="pl-alerts-btn"')[1].split('</button>')[0])
        self.assertIn('Nothing about the properties needs attention.', html)

    def test_an_association_missing_its_management_agreement_turns_the_button_on_with_a_count(self):
        prop('Missing Docs Condo')
        r = self.page(self.boss)
        html = r.content.decode()
        self.assertTrue(r.context['alerts'])
        button = html.split('<button type="button" id="pl-alerts-btn"')[1].split('</button>')[0]
        self.assertIn('pl-alerts-on', button)
        self.assertIn(f'<span class="pl-n">{len(r.context["alerts"])}</span>', button)
        self.assertIn('missing a required document', html)
        self.assertIn(f'href="{reverse("missing_documents")}"', html)

    def test_the_old_banners_and_the_missing_documents_button_are_gone_from_the_top(self):
        prop('Missing Docs Condo')
        html = self.page(self.boss).content.decode()
        self.assertNotIn('ml-banner', html)
        self.assertNotIn('Review them', html)
        self.assertNotIn('bell-ring" class="icon"></i> 1 thing', html)

    def test_the_panel_is_closed_until_the_button_is_pressed(self):
        prop('Missing Docs Condo')
        html = self.page(self.boss).content.decode()
        self.assertIn('id="pl-alerts-panel" hidden', html)

    def test_the_new_property_button_is_still_there(self):
        self.assertIn(f'href="{reverse("property_create")}"', self.page(self.boss).content.decode())

    def test_everyone_sees_missing_documents_but_the_business_reminders_are_for_admins(self):
        prop('Missing Docs Condo')
        with mock.patch('core.nudges._properties', return_value=[{'section': 'properties', 'key': 'faq', 'count': 3, 'level': 'info', 'icon': 'x', 'url': '/f/', 'text': 'three answers', 'detail': ''}]):
            plain = [a['key'] for a in property_alerts.collect(self.plain_user)]
            boss = [a['key'] for a in property_alerts.collect(self.boss)]
        self.assertEqual(plain, ['documents'])
        self.assertIn('faq', boss)
        self.assertIn('documents', boss)

    def test_an_association_whose_late_fee_or_interest_terms_are_not_confirmed_is_an_alert(self):
        from accounting import ledger
        p = prop('Terms Condo')
        profile = ledger.profile_for(p)
        profile.we_do_financials, profile.late_fee_kind, profile.late_fee_confirmed, profile.interest_rate, profile.interest_confirmed = True, 'percent', True, 18, False
        profile.save()
        got = {a['key']: a for a in property_alerts.collect(self.boss)}
        self.assertIn('terms', got)
        self.assertIn('Terms Condo: interest', got['terms']['detail'])
        self.assertEqual(got['terms']['url'], reverse('accounting:settings', args=[p.pk]))

    def test_a_collector_that_fails_never_takes_the_page_down(self):
        with mock.patch('core.property_alerts._missing_documents', side_effect=RuntimeError('boom')):
            self.assertEqual(self.page(self.boss).status_code, 200)

    def test_the_missing_size_details_alert_filters_the_boxes(self):
        p = prop('800 Tropic', T.SHORT_TERM_RENTAL)
        keys = [a['key'] for a in property_alerts.collect(self.boss)]
        self.assertIn('specs', keys)
        item = next(a for a in property_alerts.collect(self.boss) if a['key'] == 'specs')
        self.assertTrue(item['url'].endswith('?needs=details'))
        self.assertTrue(p.pk)

    def test_contact_reminders_do_not_belong_on_the_properties_screen(self):
        self.assertNotContains(self.page(self.boss), 'waiting for a yes or no')
