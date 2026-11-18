"""The Properties screen: a newspaper of small boxes (one for each line of business, as tall as its own list), the Alerts button that holds every property reminder, and no placeholders."""
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from core import property_alerts
from core.models import Property, StaffProfile
from core.property_layout import box_height, pack_columns
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
        self.assertIn('pl-cols', html)
        self.assertNotIn('<table', html.split('pl-cols')[1])                       # no table, no type or address columns
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
        self.assertNotIn('btn-alerts-on', html.split('<button type="button" id="pl-alerts-btn"')[1].split('</button>')[0])
        self.assertIn('Nothing about the properties needs attention.', html)

    def test_an_association_missing_its_management_agreement_turns_the_button_on_with_a_count(self):
        prop('Missing Docs Condo')
        r = self.page(self.boss)
        html = r.content.decode()
        self.assertTrue(r.context['alerts'])
        button = html.split('<button type="button" id="pl-alerts-btn"')[1].split('</button>')[0]
        self.assertIn('btn-alerts-on', button)
        self.assertIn(f'<span class="alerts-n">{len(r.context["alerts"])}</span>', button)
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


def fake(title, n):
    return {'title': title, 'rows': [object()] * n}


class TheColumnsComeOutEvenTests(TestCase):
    """The boxes are dealt into three columns so the columns are about the same height, the extra length in the first: a justified newspaper."""

    def heights(self, cols):
        return [round(sum(box_height(b) for b in col), 1) for col in cols]

    def test_the_columns_come_out_about_the_same_height(self):
        boxes = [fake('Associations', 11), fake('Short-term rentals', 12), fake('Commercial', 5), fake('Long-term rentals', 11), fake('Snowbird oversight', 2), fake('Other', 1)]
        cols = pack_columns(boxes)
        h = self.heights(cols)
        self.assertLessEqual(max(h) - min(h), 3.5, h)                    # a plain three-in-a-row deal would be 11 / 12 / 16 rows apart
        self.assertEqual(sorted(b['title'] for c in cols for b in c), sorted(b['title'] for b in boxes))          # every box is placed once

    def test_extra_length_goes_in_the_first_column(self):
        for sizes in ([10, 10, 3], [7, 7, 7, 2], [12, 4, 4, 4, 1], [9, 9, 4, 3]):
            h = self.heights(pack_columns([fake(f'B{i}', n) for i, n in enumerate(sizes)]))
            self.assertGreaterEqual(h[0] + 1.0, h[1], (sizes, h))
            self.assertGreaterEqual(h[1] + 1.0, h[2], (sizes, h))

    def test_inside_a_column_the_boxes_keep_their_usual_order(self):
        boxes = [fake(f'B{i}', n) for i, n in enumerate([6, 5, 4, 3, 2, 1])]
        for col in pack_columns(boxes):
            self.assertEqual([b['title'] for b in col], sorted(b['title'] for b in col))

    def test_a_single_box_or_a_few_boxes_still_work(self):
        self.assertEqual([len(c) for c in pack_columns([fake('Only', 4)])], [1, 0, 0])
        self.assertEqual(pack_columns([]), [[], [], []])
        two = pack_columns([fake('A', 6), fake('B', 6)])
        self.assertEqual(sorted(len(c) for c in two), [0, 1, 1])

    def test_the_boxes_are_not_changed_or_reordered_by_title(self):
        boxes = [fake('Associations', 8), fake('Short-term rentals', 8), fake('Commercial', 8)]
        self.assertEqual([[b['title'] for b in c] for c in pack_columns(boxes)], [['Associations'], ['Short-term rentals'], ['Commercial']])

    def test_two_columns_for_a_narrower_deal(self):
        cols = pack_columns([fake('A', 10), fake('B', 5), fake('C', 5)], columns=2)
        h = self.heights(cols)
        self.assertLessEqual(abs(h[0] - h[1]), 2.5)


@override_settings(STORAGES=STORAGES)
class ThePageUsesTheColumnsTests(TestCase):
    def test_the_page_draws_three_columns_and_every_box_once(self):
        boss = get_user_model().objects.create_superuser('boss3', 'b3@example.com', 'pw12345678')
        StaffProfile.objects.create(user=boss, role='admin', is_company_admin=True)
        self.client.force_login(boss)
        for i in range(8):
            prop(f'Assoc {i}')
        for i in range(3):
            prop(f'Rental {i}', T.SHORT_TERM_RENTAL)
        prop('Lofts', T.COMMERCIAL)
        r = self.client.get(reverse('property_list'))
        html = r.content.decode()
        self.assertEqual(html.count('class="pl-col"'), 3)
        self.assertEqual(html.count('class="pl-box"'), 3)
        self.assertEqual(sum(len(c) for c in r.context['columns']), 3)
        j = self.client.get(reverse('property_list') + '?q=lofts', HTTP_X_REQUESTED_WITH='XMLHttpRequest').json()
        self.assertIn('pl-cols', j['paper'])
        self.assertNotIn('Assoc 1', j['paper'])


@override_settings(STORAGES=STORAGES)
class ThePhoneLayoutTests(TestCase):
    """On a phone: no title, no alerts, no inactive switch, just search and a small +; every property on its own thumb-sized line; names that end in ... ; the boxes in their usual order."""

    def setUp(self):
        boss = get_user_model().objects.create_superuser('boss4', 'b4@example.com', 'pw12345678')
        StaffProfile.objects.create(user=boss, role='admin', is_company_admin=True)
        self.client.force_login(boss)
        prop('St Andrews Grand')
        prop('800 Tropic', T.SHORT_TERM_RENTAL)
        prop('324 Lofts', T.COMMERCIAL)
        self.html = self.client.get(reverse('property_list')).content.decode()

    def test_the_phone_rules_hide_the_title_alerts_and_inactive_switch(self):
        css = self.html.split('@media (max-width: 575px)')[-1].split('</style>')[0]
        for hidden in ('.pl-top .page-title', '.pl-inactive', '#pl-alerts-btn', '#pl-alerts-panel'):
            self.assertIn(hidden, css)
        self.assertIn('display: none !important', css)

    def test_every_property_is_on_its_own_line_on_a_phone(self):
        css = self.html.split('@media (max-width: 575px)')[-1].split('</style>')[0]
        self.assertNotIn('grid-template-columns: 1fr 1fr', css)                       # no two-column names any more
        self.assertNotIn('nth-of-type(odd)', css)

    def test_the_rows_are_thumb_sized_buttons_from_the_shared_stylesheet(self):
        from pathlib import Path

        from django.conf import settings
        self.assertIn('css/thumb-rows', self.html)                                    # base.html loads it for every page
        css = (Path(settings.BASE_DIR) / 'static' / 'css' / 'thumb-rows.css').read_text(encoding='utf-8')
        self.assertIn('@media (max-width: 575px)', css)
        self.assertIn('.pl-box .pl-row.pl-row', css)
        self.assertIn('.np-box .np-row.np-row', css)                                  # the other newspaper screens use the same rows
        self.assertIn('min-height: 3rem', css)                                        # about 48px: easy for a thumb
        self.assertIn('border-radius: 12px', css)                                     # each row is its own soft button
        self.assertIn(':active', css)                                                 # pressed state answers the tap
        self.assertIn(':focus-visible', css)
        self.assertIn('transition: none', css)                                        # no delay on the pressed tint
        self.assertIn('var(--brand-primary)', css)                                    # theme values only: dark mode themes it
        self.assertNotIn('#fff', css)

    def test_a_name_that_does_not_fit_ends_in_dots(self):
        self.assertIn('.pl-name { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }', self.html)

    def test_the_new_property_button_stays_as_a_small_plus(self):
        self.assertIn('class="btn-action pl-new"', self.html)
        self.assertIn('.pl-new .pl-new-text { display: none; }', self.html)

    def test_the_boxes_carry_their_usual_order_for_the_phone_column(self):
        orders = [int(x) for x in __import__('re').findall(r'class="pl-box" style="order: (\d+);"', self.html)]
        self.assertEqual(sorted(orders), list(range(len(orders))))
        self.assertEqual(orders[0], 0)
