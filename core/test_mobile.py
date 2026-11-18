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
class TheTilesTests(TestCase):
    """A manager or admin: a big Home tile and six medium ones (Tickets, Compliance, Projects, On-Site, Properties, Contacts). A cleaner keeps her own six."""

    def labels(self, user):
        return [t['label'] for t in mobile.tiles(user)]

    def test_login_is_required(self):
        response = self.client.get(reverse('mobile_menu'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response['Location'])

    def test_a_staffer_sees_a_big_home_tile_then_the_six_in_the_order_of_importance(self):
        user, _ = person('pm')
        self.assertEqual(self.labels(user), ['Home', 'Tickets', 'Compliance', 'Projects', 'On-Site', 'Properties', 'Contacts'])
        self.assertEqual([t['key'] for t in mobile.tiles(user) if t.get('big')], ['home'])
        self.client.force_login(user)
        html = self.client.get(reverse('mobile_menu')).content.decode()
        self.assertEqual(html.count('class="m-tile '), 7)
        self.assertEqual(html.count('m-tile-big'), 1)
        self.assertLess(html.index('m-tile-home'), html.index('m-tile-tickets'))
        order = [html.index(f'm-tile-{k}') for k in ('home', 'tickets', 'compliance', 'projects', 'onsite', 'properties', 'contacts')]
        self.assertEqual(order, sorted(order))
        self.assertIn('Hi, Pat', html)

    def test_there_is_no_new_ticket_tile_and_no_financials_or_time_tile_for_a_manager(self):
        user, staff = person('pm1', new_face=True)
        staff.can_financial = True
        staff.save()
        TimeProfile.objects.create(staff=staff, applies=True)
        self.client.force_login(user)
        html = self.client.get(reverse('mobile_menu')).content.decode()
        for gone in ('m-tile-new_ticket', 'm-tile-clock', 'm-tile-financials', 'm-tile-time'):
            self.assertNotIn(gone, html)
        self.assertNotIn('New ticket</span>', html.split('data-m-quick')[0])             # the only New ticket on this screen is in Quick add

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

    def test_compliance_and_projects_open_their_screens(self):
        new, _ = person('newc', new_face=True)
        old, _ = person('oldc')
        by = {t['key']: t['url'] for t in mobile.tiles(new)}
        self.assertEqual((by['compliance'], by['projects']), (reverse('ws_compliance'), reverse('ws_projects')))
        self.assertEqual({t['key']: t['url'] for t in mobile.tiles(old)}['compliance'], reverse('my_sessions'))

    def test_a_cleaner_gets_on_site_first_on_either_layout_and_still_has_six(self):
        old, _ = person('cl_old', role='cleaner')
        new, _ = person('cl_new', new_face=True, role='cleaner')
        for user in (old, new):
            tiles = self.labels(user)
            self.assertEqual(tiles[0], 'On-Site')
            self.assertEqual(len(tiles), 6)
            self.assertEqual(sorted(tiles), sorted(['Home', 'Tickets', 'On-Site', 'Properties', 'Contacts', 'New ticket']))
            self.assertFalse([t for t in mobile.tiles(user) if t.get('big')])             # her menu is exactly as it was

    def test_everyone_gets_properties_and_contacts_whatever_the_menu_role(self):
        user, _ = person('maint', new_face=True, menu_role='maintenance')
        self.assertIn('Properties', self.labels(user))
        self.assertIn('Contacts', self.labels(user))

    def test_the_time_clock_rides_on_the_home_tile_for_someone_on_it_and_says_where_they_stand(self):
        user, staff = person('clock')
        profile = TimeProfile.objects.create(staff=staff, applies=True)
        home = mobile.tiles(user)[0]
        self.assertTrue(home['hint'].endswith('Not clocked in'), home['hint'])
        self.assertNotIn('Time clock', self.labels(user))                                 # no tile of its own: the clock card is on Home
        today = timezone.localdate()
        started = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time())) + timedelta(hours=8, minutes=5)
        punch = Punch.objects.create(profile=profile, day=today, clock_in=started)
        self.assertTrue(mobile.tiles(user)[0]['hint'].endswith('Clocked in 8:05 AM'))
        punch.clock_out = started + timedelta(hours=8)
        punch.lunch_minutes = 30
        punch.save()
        self.assertTrue(mobile.tiles(user)[0]['hint'].endswith('Done for today'))

    def test_a_cleaner_on_the_clock_still_gets_the_time_clock_tile_second(self):
        user, staff = person('clock_cl', role='cleaner')
        TimeProfile.objects.create(staff=staff, applies=True)
        sixth = mobile.tiles(user)[1]
        self.assertEqual((sixth['label'], sixth['hint'], sixth['url']), ('Time clock', 'Not clocked in', reverse('timeclock_home')))
        self.assertNotIn('New ticket', self.labels(user))

    def test_someone_not_on_the_clock_does_not_see_it(self):
        user, staff = person('off')
        TimeProfile.objects.create(staff=staff, applies=False)
        self.assertNotIn('Time clock', self.labels(user))
        self.assertNotIn('Clocked', mobile.tiles(user)[0]['hint'])

    def test_phone_quick_add_is_exactly_ticket_compliance_rule_on_site_visit_and_contact(self):
        user, _ = person('pm2', new_face=True)
        self.client.force_login(user)
        html = self.client.get(reverse('mobile_menu')).content.decode()
        quick = html.split('data-m-quick')[1].split('</div>')[0]
        for name, label in (('ticket_create', 'New ticket'), ('session_template_create', 'New compliance rule'), ('onsite_visit_create', 'New on-site visit'), ('contact_create', 'New contact')):
            self.assertIn(f'href="{reverse(name)}"', quick, name)
            self.assertIn(label, quick)
        self.assertEqual(quick.count('class="m-row"'), 4)
        phone_part = html.split('class="m-launch"')[1]                                    # the desktop menu (hidden on a phone) has the fuller Quick add
        for name in ('property_create', 'process_template_create'):
            self.assertNotIn(reverse(name), phone_part, name)                             # not on a phone: no new properties, no project templates
        self.assertNotIn('?start=1', phone_part)
        self.assertNotIn('New property', phone_part)
        self.assertEqual([q['label'] for q in mobile.quick_add()], ['New ticket', 'New compliance rule', 'New on-site visit', 'New contact'])

    def test_the_menu_offers_the_full_site_the_install_tip_and_log_out(self):
        user, _ = person('pm4', new_face=True)
        self.client.force_login(user)
        html = self.client.get(reverse('mobile_menu')).content.decode()
        for needle in ('data-m-full', 'data-pwa-install', reverse('logout')):
            self.assertIn(needle, html)
        self.assertIn(f'href="{reverse("ws_home")}" data-m-full', html)

    def test_the_hints_never_take_the_menu_down(self):
        user, _ = person('pm3')
        self.client.force_login(user)
        with mock.patch('workspace.services.home', side_effect=RuntimeError('boom')):
            response = self.client.get(reverse('mobile_menu'))
        self.assertEqual(response.status_code, 200)


@STATIC
class ThePhoneAppearanceControlTests(TestCase):
    """Light / Dark / Automatic on the phone menu for people allowed dark mode (Admin for now): one stored choice that applies on the phone and on the web version."""

    def admin(self, name='adm'):
        user, staff = person(name, new_face=True)
        staff.is_company_admin = True
        staff.save()
        return user, staff

    def test_an_admin_sees_a_prominent_three_way_control_and_a_plain_user_does_not(self):
        admin, _ = self.admin()
        self.client.force_login(admin)
        html = self.client.get(reverse('mobile_menu')).content.decode()
        self.assertIn('data-m-appearance', html)
        group = html.split('data-m-appearance')[1].split('</div>')[0]
        for choice in ('light', 'dark', 'auto'):
            self.assertIn(f'data-theme-choice="{choice}"', group)
        self.assertIn('class="m-seg"', html)                                              # a segmented control, not three tiny links
        plain, _ = person('plain', new_face=True)
        self.client.force_login(plain)
        html = self.client.get(reverse('mobile_menu')).content.decode()
        self.assertNotIn('data-m-appearance', html)
        self.assertNotIn('data-theme-choice="', html)                                     # (the bare attribute name is also in a CSS rule)

    def test_choosing_dark_saves_on_the_profile_and_both_the_phone_and_the_web_pages_carry_it(self):
        admin, staff = self.admin('adm2')
        self.client.force_login(admin)
        r = self.client.post(reverse('theme_set'), {'theme': 'dark'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(r.status_code, 200)
        staff.refresh_from_db()
        self.assertEqual(staff.theme, 'dark')
        for name in ('mobile_menu', 'ws_home', 'property_list'):                          # the phone menu and two web pages: one stored preference
            html = self.client.get(reverse(name)).content.decode()
            self.assertIn('data-theme-pref="dark"', html, name)

    def test_a_non_admin_cannot_set_it(self):
        plain, staff = person('plain2', new_face=True)
        self.client.force_login(plain)
        r = self.client.post(reverse('theme_set'), {'theme': 'dark'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(r.status_code, 403)
        staff.refresh_from_db()
        self.assertNotEqual(staff.theme, 'dark')


@STATIC
class TheMenuOrderTests(TestCase):
    """The order of importance on every menu: Home, Tickets, Compliance, Projects, On-Site, Properties, Contacts, Financials, then Time."""

    def keys(self, user, staff):
        from workspace import menu
        self.client.force_login(user)

        class R:
            pass
        r = R()
        r.user, r.path = user, '/w/home'
        return [i['key'] for i in menu.build(r, staff)]

    def test_financials_comes_after_contacts_and_before_time(self):
        user, staff = person('fin', new_face=True)
        staff.can_financial = True
        staff.save()
        keys = self.keys(user, staff)
        self.assertEqual(keys, ['home', 'tickets', 'compliance', 'projects', 'onsite', 'properties', 'contacts', 'financials', 'time'])

    def test_a_plain_user_has_the_same_order_without_financials(self):
        user, staff = person('plainm', new_face=True)
        self.assertEqual(self.keys(user, staff), ['home', 'tickets', 'compliance', 'projects', 'onsite', 'properties', 'contacts', 'time'])

    def test_an_admin_has_the_dashboard_after_home_and_admin_last(self):
        user, staff = person('admm', new_face=True)
        staff.is_company_admin = True
        staff.save()
        keys = self.keys(user, staff)
        self.assertEqual(keys[:3], ['home', 'dashboard', 'tickets'])
        self.assertEqual(keys[-1], 'admin')
        self.assertLess(keys.index('contacts'), keys.index('financials') if 'financials' in keys else 99)

    def test_the_original_layout_menu_has_the_same_importance_order(self):
        user, _ = person('oldlayout')
        self.client.force_login(user)
        html = self.client.get(reverse('property_list')).content.decode()
        nav = html.split('id="navMain"')[1].split('Quick add')[0]
        order = [nav.index(x) for x in ('> Dashboard', '> Tickets', '> Recurring', '> On-Site', '> Properties', '> Contacts')]
        self.assertEqual(order, sorted(order))


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
        with mock.patch('core.google_calendar.is_configured', return_value=True), mock.patch('core.google_calendar.get_upcoming_events', return_value=(raw, [])) as read, \
                mock.patch('core.google_calendar.get_events_between', return_value=(raw, [])):
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
