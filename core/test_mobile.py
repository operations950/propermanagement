"""The phone: the six-tile menu at /m/, the top bar every page carries, and Home's phone layout."""
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import resolve, reverse
from django.utils import timezone

from timeclock.models import Punch, TimeProfile

from . import mobile
from .models import StaffProfile

STATIC = override_settings(STORAGES={'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                                     'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})


def person(username, role='', new_face=False, menu_role='', first='Pat'):
    user = get_user_model().objects.create_user(username, f'{username}@example.com', 'pw12345678', first_name=first)
    return user, StaffProfile.objects.create(user=user, role=role, new_face=new_face, menu_role=menu_role)


@STATIC
class TheSixTilesTests(TestCase):
    def labels(self, user):
        return [t['label'] for t in mobile.tiles(user)]

    def test_login_is_required(self):
        response = self.client.get(reverse('mobile_menu'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response['Location'])

    def test_a_staffer_sees_six_tiles_in_the_usual_order_and_the_sixth_is_new_ticket(self):
        user, _ = person('pm')
        self.assertEqual(self.labels(user), ['Home', 'Tickets', 'On-Site', 'Properties', 'Contacts', 'New ticket'])
        self.client.force_login(user)
        html = self.client.get(reverse('mobile_menu')).content.decode()
        self.assertEqual(html.count('class="m-tile '), 6)
        self.assertIn('Hi, Pat', html)

    def test_every_tile_links_to_a_real_screen(self):
        for new_face in (False, True):
            user, _ = person(f'u{int(new_face)}', new_face=new_face)
            for tile in mobile.tiles(user):
                match = resolve(tile['url'].split('?')[0])
                self.assertTrue(match.url_name, tile['key'])

    def test_home_goes_to_the_new_home_or_the_original_dashboard(self):
        old, _ = person('old')
        new, _ = person('new', new_face=True)
        self.assertEqual(mobile.tiles(old)[0]['url'], reverse('dashboard'))
        self.assertEqual(mobile.tiles(new)[0]['url'], reverse('ws_home'))

    def test_the_new_layout_goes_to_the_plain_ticket_list_and_the_original_to_the_reactive_one(self):
        old, _ = person('old')
        new, _ = person('new', new_face=True)
        self.assertEqual(mobile.tiles(new)[1]['url'], reverse('ticket_list'))
        self.assertEqual(mobile.tiles(old)[1]['url'], reverse('ticket_list') + '?source=reactive')

    def test_a_cleaner_gets_on_site_first_on_either_layout_and_still_has_six(self):
        old, _ = person('cl_old', role='cleaner')
        new, _ = person('cl_new', new_face=True, menu_role='cleaner')
        for user in (old, new):
            tiles = self.labels(user)
            self.assertEqual(tiles[0], 'On-Site')
            self.assertEqual(len(tiles), 6)
            self.assertEqual(sorted(tiles), sorted(['Home', 'Tickets', 'On-Site', 'Properties', 'Contacts', 'New ticket']))

    def test_everyone_gets_properties_and_contacts_whatever_the_menu_role(self):
        user, _ = person('maint', new_face=True, menu_role='maintenance')
        self.assertIn('Properties', self.labels(user))
        self.assertIn('Contacts', self.labels(user))

    def test_the_time_clock_takes_the_sixth_place_for_someone_on_it_and_says_where_they_stand(self):
        user, staff = person('clock')
        profile = TimeProfile.objects.create(staff=staff, applies=True)
        sixth = mobile.tiles(user)[5]
        self.assertEqual((sixth['label'], sixth['hint'], sixth['url']), ('Time clock', 'Not clocked in', reverse('timeclock_home')))
        today = timezone.localdate()
        started = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time())) + timedelta(hours=8, minutes=5)
        punch = Punch.objects.create(profile=profile, day=today, clock_in=started)
        self.assertEqual(mobile.tiles(user)[5]['hint'], 'Clocked in 8:05 AM')
        punch.clock_out = started + timedelta(hours=8)
        punch.lunch_minutes = 30
        punch.save()
        self.assertEqual(mobile.tiles(user)[5]['hint'], 'Done for today')
        self.assertNotIn('New ticket', self.labels(user))

    def test_someone_not_on_the_clock_does_not_see_it(self):
        user, staff = person('off')
        TimeProfile.objects.create(staff=staff, applies=False)
        self.assertNotIn('Time clock', self.labels(user))

    def test_the_menu_offers_quick_add_the_full_site_the_install_tip_and_log_out(self):
        user, _ = person('pm2', new_face=True)
        self.client.force_login(user)
        html = self.client.get(reverse('mobile_menu')).content.decode()
        for needle in (reverse('ticket_create'), reverse('property_create'), reverse('contact_create'), 'data-m-full', 'data-pwa-install', reverse('logout')):
            self.assertIn(needle, html)
        self.assertIn(f'href="{reverse("ws_home")}" data-m-full', html)

    def test_the_hints_never_take_the_menu_down(self):
        user, _ = person('pm3')
        self.client.force_login(user)
        with mock.patch('workspace.services.home', side_effect=RuntimeError('boom')):
            response = self.client.get(reverse('mobile_menu'))
        self.assertEqual(response.status_code, 200)


@STATIC
class TheTopBarTests(TestCase):
    def test_every_signed_in_page_carries_the_phone_bar_with_a_menu_button_back_to_the_menu(self):
        user, _ = person('bar', new_face=True)
        self.client.force_login(user)
        html = self.client.get(reverse('property_list')).content.decode()
        self.assertIn('id="mBar"', html)
        self.assertIn('data-m-menu', html)
        self.assertIn(f'href="{reverse("mobile_menu")}"', html)
        self.assertIn('data-m-back', html)
        self.assertIn('css/mobile.css', html)
        self.assertIn('js/mobile.js', html)

    def test_the_menu_itself_has_no_menu_button(self):
        user, _ = person('bar2')
        self.client.force_login(user)
        html = self.client.get(reverse('mobile_menu')).content.decode()
        self.assertIn('id="mBar"', html)
        self.assertNotIn('data-m-menu', html)
        self.assertNotIn('data-m-back', html)

    def test_a_page_can_name_where_back_goes(self):
        user, _ = person('bar3')
        self.client.force_login(user)
        html = self.client.get(reverse('contact_list')).content.decode()
        self.assertIn('id="mBackUrl"', html)                              # the hook a page fills in with {% block mobile_back_url %}

    def test_the_desktop_menu_is_still_there(self):
        user, _ = person('bar4')
        self.client.force_login(user)
        html = self.client.get(reverse('property_list')).content.decode()
        self.assertIn('brand-bar', html)
        self.assertIn('navbar-toggler', html)

    def test_the_phone_rules_only_touch_phone_widths(self):
        from pathlib import Path

        from django.conf import settings
        css = (Path(settings.BASE_DIR) / 'static' / 'css' / 'mobile.css').read_text(encoding='utf-8')
        # everything that restyles an existing page sits inside a max-width: 768px query
        outside = css.split('@media (max-width: 768px)')[0]
        for selector in ('.brand-bar', '.form-control', '.btn:', '.list-group-item', '.page-content', '.ws-row'):
            self.assertNotIn(selector, outside, selector)
        self.assertIn('--m-bar-h', css)
        self.assertIn('safe-area-inset', css)

    def test_the_login_page_has_no_bar(self):
        html = self.client.get(reverse('login')).content.decode()
        self.assertNotIn('id="mBar"', html)


@STATIC
class HomeOnAPhoneTests(TestCase):
    def setUp(self):
        self.user, self.staff = person('home', new_face=True)
        self.client.force_login(self.user)

    def test_home_has_the_phone_wrapper_and_keeps_the_day_before_the_calendar(self):
        html = self.client.get(reverse('ws_home')).content.decode()
        self.assertIn('class="m-home"', html)
        self.assertIn('class="ws-tabs"', html)

    def _days(self):
        from tickets.views import _format_calendar_events
        today = timezone.localdate()
        raw = [{'summary': 'Walk the roof', 'start': {'dateTime': timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time()) + timedelta(hours=10)).isoformat()},
                'end': {'dateTime': timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time()) + timedelta(hours=11)).isoformat()}, '_calendar_id': 'primary', 'id': 'e1'},
               {'summary': 'Board holiday', 'start': {'date': (today + timedelta(days=2)).isoformat()}, 'end': {'date': (today + timedelta(days=3)).isoformat()}, '_calendar_id': 'primary', 'id': 'e2'}]
        return raw, _format_calendar_events(raw, days_ahead=13)

    def _with_calendar(self, horizon='today'):
        from core.models import GoogleCalendarToken
        raw, _ = self._days()
        GoogleCalendarToken.objects.create(staff=self.staff, access_token='x', refresh_token='y', google_email='pat@example.com')
        with mock.patch('core.google_calendar.is_configured', return_value=True), mock.patch('core.google_calendar.get_upcoming_events', return_value=(raw, [])) as read:
            response = self.client.get(reverse('ws_home'), {'h': horizon})
        return response, read

    def test_the_phone_agenda_carries_today_and_the_next_two_weeks_from_one_read_of_google(self):
        response, read = self._with_calendar()
        self.assertEqual(read.call_count, 1)
        self.assertEqual(len(response.context['calendar_days_all']), 14)
        html = response.content.decode()
        self.assertIn('data-m-agenda', html)
        self.assertEqual(html.count('data-day '), 14)
        self.assertIn('Walk the roof', html)
        self.assertIn('Board holiday', html)
        self.assertIn('data-prev', html)
        self.assertIn('data-next', html)

    def test_the_two_week_list_skips_days_with_nothing_on_them(self):
        response, _ = self._with_calendar()
        html = response.content.decode()
        listing = html.split('data-all', 1)[1].split('m-agenda-foot', 1)[0]
        self.assertEqual(listing.count('<h6>'), 2)                         # today's event and the all-day event two days on; the other twelve days are not listed
        self.assertIn('Days that are not listed have nothing on them.', listing)

    def test_the_desktop_calendar_still_gets_only_the_days_the_range_asks_for(self):
        for horizon, expected in (('today', 1), ('week', 7), ('month', 14)):
            response, _ = self._with_calendar(horizon)
            self.assertEqual(len(response.context['calendar_days']), expected, horizon)
            from core.models import GoogleCalendarToken
            GoogleCalendarToken.objects.all().delete()

    def test_without_a_connected_calendar_the_phone_shows_a_big_connect_button(self):
        with mock.patch('core.google_calendar.is_configured', return_value=True):
            html = self.client.get(reverse('ws_home')).content.decode()
        self.assertIn('m-btn-primary', html)
        self.assertIn('Connect Google Calendar', html)
