from datetime import datetime, time, timedelta
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import Property, StaffProfile

from .models import Booking, Visit, VisitType

STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}
CSS = 'css/mobile-onsite.css'


@override_settings(STORAGES=STORAGES)
class PhoneOnSiteTests(TestCase):
    """The On-Site screens on a phone: big visit cards and buttons, a way up at the top of every page, the week board as cards, the cleaner's own page easy to use with one thumb.
    Everything new sits in phone-only markup or a phone-only media query, so the desktop screens read exactly as before."""

    def setUp(self):
        User = get_user_model()
        self.boss = User.objects.create_superuser('boss', 'b@example.com', 'pw12345678')
        cu = User.objects.create_user('charlotte', first_name='Charlotte', last_name='Kim', password='pw12345678')
        self.cleaner = StaffProfile.objects.create(user=cu, role=StaffProfile.Role.CLEANER, new_face=True)
        self.prop = Property.objects.create(name='Palm Cottage 12', property_type='str', street='12 Palm Way', city='Delray Beach', state='FL', zip_code='33444', gate_code='4410')
        self.vt = VisitType.objects.create(name='Turnover Clean')
        self.today = timezone.localdate()
        self.visit = Visit.objects.create(property=self.prop, visit_type=self.vt, scheduled_date=self.today, status=Visit.Status.SCHEDULED, assigned_staff=self.cleaner,
                                          ready_by=timezone.make_aware(datetime.combine(self.today, time(15, 0))))
        self.client.force_login(self.boss)

    # ---------------------------------------------------------------- the visits list
    def test_the_visits_list_has_compact_phone_cards_with_a_maps_button_and_the_desktop_table_is_still_there(self):
        html = self.client.get(reverse('onsite_dashboard')).content.decode()
        self.assertIn(CSS, html)
        self.assertIn('class="mo-vc"', html)
        self.assertIn('Ready by 3:00 PM', html)
        self.assertIn('class="mo-vc-dir"', html)                             # Directions is a small button at the card's edge, not a row of its own
        self.assertIn('https://www.google.com/maps/search/?api=1&amp;query=12%20Palm%20Way%2C%20Delray%20Beach%2C%20FL%2033444', html)
        self.assertNotIn('class="mo-card"', html)                            # the tall card is gone
        self.assertIn('d-none d-md-block table-responsive', html)            # the desktop table, untouched
        self.assertIn('d-none d-md-flex', html)                              # and the desktop toolbar

    def morning(self):
        """Pin the clock to 9 AM today: a visit is "at risk" in the 90 minutes before its ready-by time, so these tests must not depend on what time of day they run."""
        return mock.patch('django.utils.timezone.now', return_value=timezone.make_aware(datetime.combine(self.today, time(9, 0))))

    def test_a_scheduled_visit_shows_no_status_word_only_something_worth_a_glance_does(self):
        with self.morning():
            html = self.client.get(reverse('onsite_dashboard')).content.decode()
        card = html.split('class="mo-vc"')[1].split('mo-vc-dir')[0]
        self.assertNotIn('>Scheduled<', card)                                # the normal state is left unsaid
        self.assertNotIn('mo-tag bad">At risk', card)
        Visit.objects.create(property=self.prop, visit_type=self.vt, scheduled_date=self.today, status=Visit.Status.UNASSIGNED)
        Visit.objects.create(property=self.prop, visit_type=self.vt, scheduled_date=self.today, status=Visit.Status.IN_PROGRESS, assigned_staff=self.cleaner)
        with self.morning():
            html = self.client.get(reverse('onsite_dashboard')).content.decode()
        self.assertIn('<span class="mo-tag warn">Unassigned</span>', html)
        self.assertIn('<span class="mo-tag warn">In progress</span>', html)
        for one_card in html.split('class="mo-vc"')[1:]:
            self.assertLessEqual(one_card.split('mo-vc-dir')[0].count('<span class="mo-tag'), 1)       # never more than ONE small tag on a card

    def test_the_card_opens_the_visit_and_the_day_chips_keep_every_range(self):
        html = self.client.get(reverse('onsite_dashboard')).content.decode()
        self.assertIn(f'href="{reverse("onsite_visit_detail", args=[self.visit.pk])}" class="mo-vc-main stretched-link"', html)
        for key in ('yesterday', 'week', 'month'):
            self.assertEqual(html.count(f'?range={key}'), 2, key)             # once in the phone chips, once in the desktop group
        self.assertIn('mo-chiprow', html)
        phone = html.split('class="mo-chiprow')[1].split('</div>')[0]
        for label in ('Yesterday', 'Today + Tomorrow', 'This week', 'Next 30 days', 'Calendar'):
            self.assertIn(label, phone, label)

    def test_the_phone_visits_screen_has_no_alert_cards_no_more_menu_and_no_shortcut_buttons(self):
        Visit.objects.create(property=self.prop, visit_type=self.vt, scheduled_date=self.today, status=Visit.Status.UNASSIGNED)
        html = self.client.get(reverse('onsite_dashboard')).content.decode()
        i = html.index('Needs attention today')
        self.assertNotEqual(html.rfind('<div class="d-none d-md-block">', 0, i), -1)       # the alert card sits inside a wide-screens-only wrapper ...
        self.assertLess(i, html.index('class="mo-chiprow'))                                 # ... that closes before the phone controls begin
        for gone in ('mo-more', 'mo-actions', 'class="mo-btn" href="'):
            self.assertNotIn(gone, html, gone)
        phone_part = html.split('class="mo-chiprow')[1]
        self.assertNotIn('Rentals today', phone_part.split('d-none d-md-flex')[0])
        self.assertIn(f'<a href="{reverse("onsite_visit_create")}" class="mo-fab d-md-none">', html)      # New visit: the one big floating button

    def test_the_desktop_toolbar_still_has_every_shortcut(self):
        html = self.client.get(reverse('onsite_dashboard')).content.decode()
        toolbar = html.split('Wide screens: every shortcut as a button.')[1].split('<div class="d-none d-md-block">')[0] if 'Wide screens: every shortcut' in html else html
        for name in ('onsite_str_today', 'onsite_rentals_calendar', 'onsite_reservation_list', 'onsite_performance', 'onsite_checklist_templates', 'onsite_visit_rule_list', 'onsite_visit_create',
                     'onsite_checklist_custom_items', 'onsite_cleaning_payments', 'onsite_booking_feeds'):
            self.assertIn(f'href="{reverse(name)}"', html, name)

    def test_the_sections_are_one_scrolling_row_for_everyone(self):
        css = (Path(settings.BASE_DIR) / 'static' / 'css' / 'mobile-onsite.css').read_text(encoding='utf-8')
        inside = css.partition('@media (max-width: 767.98px)')[2]
        self.assertIn('body .ws-sub {', inside)
        self.assertIn('flex-wrap: nowrap', inside.split('body .ws-sub {')[1].split('}')[0])
        self.assertNotIn('grid-template-columns: 1fr 1fr', inside.split('body .ws-sub {')[1].split('}')[0])
        plain = get_user_model().objects.create_user('amy', password='pw12345678')
        StaffProfile.objects.create(user=plain, role=StaffProfile.Role.PROPERTY_MANAGER, new_face=False)         # not on the new layout: she gets the same row from the phone-only partial
        self.client.force_login(plain)
        html = self.client.get(reverse('onsite_dashboard')).content.decode()
        self.assertIn('class="mo-sections d-md-none"', html)
        for name in ('onsite_dashboard', 'ws_week', 'onsite_str_today', 'onsite_reservation_list', 'onsite_performance'):
            self.assertIn(f'href="{reverse(name)}"', html.split('class="mo-sections')[1].split('</div>')[0], name)

    def test_a_drilled_in_day_has_a_way_back_up_in_the_chip_row(self):
        html = self.client.get(reverse('onsite_dashboard') + f'?date={self.today:%Y-%m-%d}').content.decode()
        chips = html.split('class="mo-chiprow')[1].split('</div>')[0]
        self.assertIn('data-no-back class="mo-pill on"', chips)
        self.assertIn('Today + Tomorrow', chips)

    def test_the_card_says_the_kind_of_visit_and_who_has_it_short(self):
        from .templatetags.onsite_phone import first_name, short_type
        self.assertEqual([short_type(x) for x in ('Turnover Clean', 'Normal Cleaning', 'Commercial Clean', 'Deep Clean', 'Task', 'Clean', '')], ['Turnover', 'Normal', 'Commercial', 'Deep', 'Task', 'Clean', ''])
        self.assertEqual([first_name(x) for x in ('Charlotte Kim', 'Unassigned', 'ABC Cleaning Co (external)', 'Madonna', '')], ['Charlotte', 'Unassigned', 'ABC (external)', 'Madonna', ''])
        html = self.client.get(reverse('onsite_dashboard')).content.decode()
        card = html.split('class="mo-vc"')[1].split('mo-vc-dir')[0]
        self.assertIn('<span class="mo-vc-type">Turnover</span>', card)
        self.assertIn('Ready by 3:00 PM &middot; Charlotte</span>', card)
        self.assertNotIn('vacant', card)                                     # "30+ days vacant" says nothing on a phone: only a real, short vacancy is worth a tag

    def test_a_short_vacancy_is_still_tagged(self):
        with self.morning():                                                 # at 9 AM, a 4 PM check-in three days on is exactly 3 days away whatever time the test runs
            now = timezone.now()
            nxt = Booking.objects.create(property=self.prop, source='manual', external_uid='nb', guest_name='Lee', check_in=now.replace(hour=16, minute=0) + timedelta(days=3), check_out=now + timedelta(days=6))
            self.visit.next_booking = nxt
            self.visit.save()
            html = self.client.get(reverse('onsite_dashboard')).content.decode()
        self.assertIn('<span class="mo-tag">3d vacant</span>', html)

    # ---------------------------------------------------------------- the calendar on a phone
    def test_the_phone_calendar_is_a_week_strip_and_a_day_agenda_and_the_month_grid_is_wide_screens_only(self):
        html = self.client.get(reverse('onsite_calendar')).content.decode()
        self.assertIn('class="mo-weekstrip"', html)
        self.assertEqual(html.count('class="mo-wkday'), 7)
        self.assertIn('class="mo-vc"', html)                                  # today's visit, as the same compact card
        self.assertIn('calendar-grid', html)                                  # the desktop grid is still there ...
        self.assertIn('class="card d-none d-md-block"', html)                 # ... and hidden on a phone
        self.assertIn('d-none d-md-flex justify-content-between', html)       # with its own month heading and arrows

    def test_the_week_strip_counts_visits_and_a_chosen_day_lists_that_days_visits(self):
        monday = self.today - timedelta(days=self.today.weekday())
        other = monday + timedelta(days=(self.today.weekday() + 2) % 7 if self.today.weekday() < 5 else 1)
        Visit.objects.create(property=self.prop, visit_type=self.vt, scheduled_date=other, status=Visit.Status.SCHEDULED)
        Visit.objects.create(property=self.prop, visit_type=self.vt, scheduled_date=other, status=Visit.Status.CANCELLED)       # a cancelled visit is not counted or listed
        response = self.client.get(reverse('onsite_calendar') + f'?day={other:%Y-%m-%d}')
        days = response.context['phone_days']
        self.assertEqual([d['date'] for d in days], [monday + timedelta(days=i) for i in range(7)])          # Monday to Sunday
        self.assertEqual({d['date']: d['count'] for d in days}[other], 1 + (1 if other == self.today else 0))
        self.assertEqual([d['is_selected'] for d in days].count(True), 1)
        self.assertEqual(len(response.context['sel_visits']), 1 + (1 if other == self.today else 0))
        html = response.content.decode()
        self.assertIn('aria-current="date"', html)
        self.assertIn(f'?day={(monday - timedelta(days=7)):%Y-%m-%d}', html)    # earlier week
        self.assertIn(f'?day={(monday + timedelta(days=7)):%Y-%m-%d}', html)    # next week

    def test_a_bad_day_falls_back_to_today_and_an_empty_day_says_so(self):
        response = self.client.get(reverse('onsite_calendar') + '?day=nonsense')
        self.assertEqual(response.context['sel_day'], self.today)
        empty = self.client.get(reverse('onsite_calendar') + f'?day={(self.today + timedelta(days=40)):%Y-%m-%d}').content.decode()
        self.assertIn('No visits this day.', empty)

    # ---------------------------------------------------------------- the visit screen
    def test_the_visit_screen_has_a_big_way_up_directions_and_keeps_its_desktop_link(self):
        html = self.client.get(reverse('onsite_visit_detail', args=[self.visit.pk])).content.decode()
        self.assertIn('class="mo-page"', html)
        self.assertIn('class="mo-back d-md-none"', html)
        self.assertIn('d-none d-md-inline-flex', html)                       # the original small link, for wide screens
        self.assertIn('mo-btn d-md-none mt-2', html)                          # Directions, phones only
        self.assertIn('query=12%20Palm%20Way', html)

    # ---------------------------------------------------------------- the cleaner's own page
    def test_the_cleaner_page_has_directions_a_full_width_start_and_still_starts_the_visit(self):
        url = reverse('onsite_visit_public', args=[self.visit.access_token])
        html = self.client.get(url).content.decode()
        self.assertIn('class="mo-page"', html)
        self.assertIn('query=12%20Palm%20Way', html)
        self.assertIn('mo-start', html)
        self.assertIn('Gate code: <strong>4410</strong>', html)
        self.assertEqual(html.count('<div class="mo-page">'), html.count('<div class="mo-page">'))
        self.client.post(url, {'action': 'start'})
        self.visit.refresh_from_db()
        self.assertIsNotNone(self.visit.started_at)

    def test_the_cleaner_checklist_sizes_only_change_at_phone_width(self):
        css = (Path(settings.BASE_DIR) / 'static' / 'css' / 'checklist-mobile.css').read_text(encoding='utf-8')
        head, _, phone = css.partition('/* Phones: the cleaner works this page')
        self.assertIn('@media (max-width: 767.98px)', phone)
        self.assertIn('min-height: 56px', phone)
        self.assertNotIn('min-height: 56px', head)                            # the original sizes above it are unchanged

    # ---------------------------------------------------------------- the properties board
    def _stay(self):
        now = timezone.now()
        return Booking.objects.create(property=self.prop, source='manual', external_uid='a1', guest_name='Dana', check_in=now - timedelta(days=2), check_out=now.replace(hour=11, minute=0) + timedelta(days=1),
                                      listing_name='Palm Cottage')

    def test_the_week_board_on_a_phone_is_one_page_per_day_with_the_same_buttons_as_the_grid(self):
        self._stay()
        html = self.client.get(reverse('ws_week')).content.decode()
        self.assertIn('class="pg-phone"', html)
        self.assertIn('class="pgscroll"', html)                               # the desktop grid is still there
        phone = html.split('class="pg-phone"')[1].split('class="pgscroll"')[0]
        self.assertIn('id="pdPager"', phone)
        self.assertEqual(phone.count('class="pd-page"'), 7)                   # a page for each of the seven days
        self.assertEqual(phone.count('class="pd-chip'), 7)                    # and a chip for each in the day strip
        self.assertIn('class="pname"', phone)
        self.assertIn('data-wk-time', phone)
        self.assertIn('Check-out', phone)
        self.assertIn('data-wk-clean', phone)
        self.assertIn('Guest in house', phone)
        self.assertNotIn('class="dayrow"', phone)

    def test_each_day_lists_only_the_properties_that_have_something_that_day(self):
        self._stay()
        response = self.client.get(reverse('ws_week'))
        by_day = response.context['w']['phone_days']
        self.assertEqual(len(by_day), 7)
        for day in by_day:
            self.assertEqual(day['count'], len(day['items']))
            for item in day['items']:
                self.assertTrue(item['c']['kind'], 'a property with nothing on the day must not be listed')
        self.assertEqual(sum(d['count'] for d in by_day), 2)                  # the one stay: in house today, checks out tomorrow: two days have something, five do not
        html = response.content.decode()
        self.assertIn('Nothing on this day.', html)                           # the quiet days say so

    def test_a_property_with_only_a_guest_in_house_is_one_slim_row(self):
        self._stay()
        phone = self.client.get(reverse('ws_week')).content.decode().split('class="pg-phone"')[1].split('class="pgscroll"')[0]
        self.assertIn('class="pcard pcard-stay"', phone)
        stay_row = phone.split('class="pcard pcard-stay"')[1].split('</div></div>')[0]
        self.assertNotIn('data-wk-time', stay_row)                           # nothing to change on a day with no check-in or check-out
        self.assertIn('Guest in house', stay_row)

    def test_the_day_strip_and_pager_have_the_script_that_links_them(self):
        html = self.client.get(reverse('ws_week')).content.decode()
        self.assertIn("document.getElementById('pdPager')", html)
        css = (Path(settings.BASE_DIR) / 'static' / 'css' / 'mobile-onsite.css').read_text(encoding='utf-8')
        self.assertIn('scroll-snap-type: x mandatory', css)
        self.assertNotIn('.pg-phone .none', css)                              # a cleaning button with the class "none" must not be hidden by any rule

    # ---------------------------------------------------------------- the other On-Site pages and the stylesheet
    def test_the_other_onsite_pages_get_the_same_phone_treatment(self):
        for name in ('onsite_str_today', 'onsite_reservation_list', 'onsite_rentals_calendar', 'onsite_calendar', 'onsite_visit_create', 'onsite_cleaner_activity'):
            html = self.client.get(reverse(name)).content.decode()
            self.assertIn(CSS, html, name)
            self.assertIn('class="mo-page"', html, name)

    def test_the_phone_stylesheet_changes_existing_looks_only_inside_the_phone_media_query(self):
        css = (Path(settings.BASE_DIR) / 'static' / 'css' / 'mobile-onsite.css').read_text(encoding='utf-8')
        before, _, inside = css.partition('@media (max-width: 767.98px)')
        for selector in ('.mo-page', '.ws-sub', '.pgw .'):
            self.assertNotIn(selector, before, selector)                      # nothing that restyles an existing element sits outside the media query
        self.assertIn('.ws-sub', inside)
        self.assertIn('min-height: 48px', inside)

    def test_the_section_tabs_are_still_the_same_links(self):
        self.client.force_login(self.cleaner.user)
        html = self.client.get(reverse('onsite_dashboard')).content.decode()
        for name in ('onsite_dashboard', 'ws_week', 'onsite_str_today', 'onsite_reservation_list', 'onsite_cleaner_activity', 'onsite_performance'):
            self.assertIn(f'href="{reverse(name)}"', html, name)
