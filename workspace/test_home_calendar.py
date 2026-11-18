"""The Home calendar card (square card, a + icon and one Settings button, the week with every day shown, the month grid) and the lines strip under the main bar."""
from datetime import date, datetime, timedelta, timezone as dt_timezone
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import GoogleCalendarToken, Property, StaffProfile
from tickets.views import MONTH_CHIPS, _month_grid, month_bounds
from workspace import menu

STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}
User = get_user_model()


def timed(title, day, hour, minute=0, minutes=60, calendar='primary', ident=None):
    start = timezone.make_aware(datetime(day.year, day.month, day.day, hour, minute))
    return {'id': ident or title, 'summary': title, 'start': {'dateTime': start.isoformat()}, 'end': {'dateTime': (start + timedelta(minutes=minutes)).isoformat()}, '_calendar_id': calendar}


def all_day(title, first, last_exclusive, calendar='primary'):
    return {'id': title, 'summary': title, 'start': {'date': first.isoformat()}, 'end': {'date': last_exclusive.isoformat()}, '_calendar_id': calendar}


class TheMonthGridTests(TestCase):
    def test_the_grid_runs_sunday_to_saturday_and_includes_the_days_either_side(self):
        self.assertEqual(month_bounds(date(2026, 11, 1)), (date(2026, 11, 1), date(2026, 12, 5)))               # Nov 1 is a Sunday; Nov 30 a Monday
        self.assertEqual(month_bounds(date(2026, 12, 1)), (date(2026, 11, 29), date(2027, 1, 2)))               # Dec 1 is a Tuesday; Dec 31 a Thursday
        self.assertEqual(month_bounds(date(2026, 2, 1)), (date(2026, 2, 1), date(2026, 2, 28)))                 # a February of exactly four weeks
        self.assertEqual(month_bounds(date(2027, 2, 1)), (date(2027, 1, 31), date(2027, 3, 6)))                 # Feb 28 is a Sunday: the grid runs on to the Saturday after

    def test_every_week_has_seven_cells_and_the_other_months_days_are_marked_out(self):
        grid = _month_grid([], date(2026, 12, 1), today=date(2026, 12, 15))
        self.assertTrue(all(len(w) == 7 for w in grid['weeks']))
        self.assertEqual(grid['label'], 'December 2026')
        flat = [c for w in grid['weeks'] for c in w]
        self.assertEqual(flat[0]['date'], date(2026, 11, 29))
        self.assertFalse(flat[0]['in_month'])
        self.assertEqual(sum(1 for c in flat if c['in_month']), 31)
        self.assertEqual([c['date'] for c in flat if c['is_today']], [date(2026, 12, 15)])

    def test_a_busy_day_shows_two_events_and_counts_the_rest_all_day_ones_first(self):
        day = date(2026, 11, 18)
        events = [timed('Owner call', day, 14), timed('Team standup', day, 9), timed('Dinner', day, 18), all_day('Board holiday', day, day + timedelta(days=1))]
        cell = next(c for w in _month_grid(events, date(2026, 11, 1), today=date(2026, 11, 17))['weeks'] for c in w if c['date'] == day)
        self.assertEqual(MONTH_CHIPS, 2)
        self.assertEqual([e['title'] for e in cell['events']], ['Board holiday', 'Team standup', 'Owner call', 'Dinner'])
        self.assertEqual([e['title'] for e in cell['shown']], ['Board holiday', 'Team standup'])
        self.assertEqual((cell['extra'], cell['total']), (2, 4))
        self.assertEqual([e['time'] for e in cell['events']], ['', '9:00a', '2:00p', '6:00p'])

    def test_a_multi_day_all_day_event_is_on_every_day_it_covers_and_not_after(self):
        events = [all_day('Conference', date(2026, 11, 10), date(2026, 11, 13))]
        cells = {c['date']: c for w in _month_grid(events, date(2026, 11, 1))['weeks'] for c in w}
        self.assertEqual([d.day for d, c in cells.items() if c['total']], [10, 11, 12])

    def test_the_cell_carries_what_the_edit_form_needs(self):
        day = date(2026, 11, 18)
        cell = next(c for w in _month_grid([timed('Walk the roof', day, 10, 30, ident='ev9', calendar='cal2')], date(2026, 11, 1))['weeks'] for c in w if c['date'] == day)
        e = cell['events'][0]
        self.assertEqual((e['event_id'], e['calendar_id'], e['date'], e['start_time'], e['end_time'], e['all_day']), ('ev9', 'cal2', day, '10:30', '11:30', False))
        self.assertEqual(cell['label'], 'Wednesday, November 18')

    def test_an_event_is_on_the_day_it_starts_in_the_persons_time_zone(self):
        # 03:30 UTC on the 18th is 10:30 pm on the 17th in New York
        utc = datetime(2026, 11, 18, 3, 30, tzinfo=dt_timezone.utc)
        event = {'id': 'late', 'summary': 'Late call', 'start': {'dateTime': utc.isoformat()}, 'end': {'dateTime': (utc + timedelta(hours=1)).isoformat()}, '_calendar_id': 'primary'}
        with timezone.override('America/New_York'):
            cells = {c['date']: c for w in _month_grid([event], date(2026, 11, 1), today=date(2026, 11, 17))['weeks'] for c in w}
        self.assertEqual(cells[date(2026, 11, 17)]['events'][0]['time'], '10:30p')
        self.assertEqual(cells[date(2026, 11, 18)]['total'], 0)


@override_settings(STORAGES=STORAGES)
class HomeCalendarCardTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('pat@example.com', 'pat@example.com', 'pw12345678')
        self.staff = StaffProfile.objects.create(user=self.user, role='', new_face=True)
        self.client.force_login(self.user)
        GoogleCalendarToken.objects.create(staff=self.staff, access_token='x', refresh_token='y', google_email='pat@example.com')
        self.today = timezone.localdate()

    def home(self, horizon, **params):
        raw = [timed('Walk the roof', self.today, 10), all_day('Board holiday', self.today + timedelta(days=2), self.today + timedelta(days=3))]
        with mock.patch('core.google_calendar.is_configured', return_value=True), mock.patch('core.google_calendar.get_upcoming_events', return_value=(raw, [])) as upcoming, \
                mock.patch('core.google_calendar.get_events_between', return_value=(raw, [])) as between:
            response = self.client.get(reverse('ws_home'), {'h': horizon, **params})
        return response, upcoming, between

    # ---------------------------------------------------------------- the card and its controls
    def test_the_card_is_square_with_a_hairline_border_and_no_sticky_or_inner_scroll(self):
        html = self.home('today')[0].content.decode()
        self.assertIn('class="ws-calendar"', html)
        self.assertIn('border: 1px solid var(--border-hairline); border-radius: 6px;', html)
        for gone in ('.ws-calendar { position: sticky', 'class="calendar-days-scroll"', 'max-height: calc(100vh'):
            self.assertNotIn(gone, html)
        self.assertIn('cal-head', html)

    def test_new_event_is_a_plus_icon_and_everything_else_is_behind_one_settings_button(self):
        html = self.home('today')[0].content.decode()
        self.assertIn('data-cal-pop="new"', html)
        self.assertIn('title="New event"', html)
        self.assertIn('data-lucide="plus"', html)
        self.assertEqual(html.count('data-cal-pop="settings"'), 1)
        self.assertIn('Settings</button>', html)
        for gone in ('>+ New event</button>', 'data-bs-target="#timezonePicker"', 'data-bs-target="#calendarPicker"'):
            self.assertNotIn(gone, html)

    def test_the_time_zone_calendars_and_disconnect_forms_live_inside_the_settings_popover(self):
        html = self.home('today')[0].content.decode()
        panel = html.split('data-cal-panel="settings"')[1].split('data-cal-panel="new"')[0]
        self.assertIn(reverse('timezone_select'), panel)
        self.assertIn(reverse('calendar_select'), panel)
        self.assertIn(reverse('calendar_disconnect'), panel)
        self.assertIn('Disconnect Google Calendar', panel)
        self.assertIn('pat@example.com', panel)                                             # the account, small, inside Settings
        header = html.split('class="cal-head"')[1].split('data-cal-panel="settings"')[0]
        self.assertNotIn(reverse('calendar_disconnect'), header)

    def test_the_new_and_edit_event_forms_still_post_to_their_endpoints(self):
        html = self.home('today')[0].content.decode()
        for name in ('calendar_event_create', 'calendar_event_update', 'calendar_event_delete'):
            self.assertIn(reverse(name), html, name)
        self.assertIn('calendar-event-clickable', html)

    def test_a_person_with_no_calendar_connected_sees_the_connect_button_and_no_plus(self):
        GoogleCalendarToken.objects.all().delete()
        html = self.home('today')[0].content.decode()
        self.assertIn(reverse('calendar_connect'), html)
        self.assertNotIn('data-cal-pop="new"', html)

    # ---------------------------------------------------------------- this week: every day, the card grows
    def test_this_week_shows_all_seven_days_in_the_card(self):
        response, upcoming, between = self.home('week')
        html = response.content.decode()
        self.assertEqual(len(response.context['calendar_days']), 7)
        self.assertEqual(html.split('class="cal-days"')[1].split('<script>')[0].count('class="cal-day'), 7)
        self.assertEqual(upcoming.call_count, 1)
        self.assertEqual(between.call_count, 0)

    # ---------------------------------------------------------------- this month: a real month grid
    def test_this_month_is_a_grid_from_one_read_that_covers_the_whole_grid_and_the_phone_agenda(self):
        response, upcoming, between = self.home('month')
        self.assertEqual(upcoming.call_count, 0)
        self.assertEqual(between.call_count, 1)
        _token, first, last = between.call_args.args
        grid_first, grid_last = month_bounds(self.today.replace(day=1))
        self.assertLessEqual(first, min(grid_first, self.today))
        self.assertGreaterEqual(last, max(grid_last, self.today + timedelta(days=13)))
        grid = response.context['calendar_month']
        self.assertEqual(grid['label'], f'{self.today:%B %Y}')
        self.assertTrue(grid['is_current'])
        html = response.content.decode()
        self.assertEqual(html.count('data-cal-day='), len(grid['weeks']) * 7)
        self.assertIn('Walk the roof', html)
        self.assertEqual(len(response.context['calendar_days_all']), 14)                    # the phone agenda still has its two weeks
        self.assertIn('data-m-agenda', html)

    def test_the_month_gets_more_width_than_the_list(self):
        html = self.home('month')[0].content.decode()
        self.assertIn('col-lg-7 ws-cal-col', html)
        self.assertIn('col-lg-5', html)
        today_html = self.home('today')[0].content.decode()
        self.assertIn('col-lg-4 ws-cal-col', today_html)

    def test_the_month_arrows_page_by_month_and_keep_the_rest_of_the_address(self):
        this = self.today.replace(day=1)
        plus = lambda n: date(this.year + (this.month - 1 + n) // 12, (this.month - 1 + n) % 12 + 1, 1)
        response = self.home('month', scope='all', m=f'{plus(2):%Y-%m}')[0]
        grid = response.context['calendar_month']
        self.assertEqual(grid['label'], f'{plus(2):%B %Y}')
        self.assertIn(f'm={plus(1):%Y-%m}', grid['prev_url'])
        self.assertIn(f'm={plus(3):%Y-%m}', grid['next_url'])
        self.assertIn('scope=all', grid['prev_url'])
        self.assertIn('h=month', grid['next_url'])
        self.assertNotIn('m=', grid['today_url'])
        self.assertFalse(grid['is_current'])
        back = self.home('month', m=f'{plus(1):%Y-%m}')[0].context['calendar_month']
        self.assertNotIn('m=', back['prev_url'])                                           # paging back to this month drops the parameter

    def test_a_bad_month_in_the_address_is_this_month(self):
        grid = self.home('month', m='nonsense')[0].context['calendar_month']
        self.assertEqual(grid['label'], f'{self.today:%B %Y}')

    def test_a_day_cell_carries_its_events_for_the_day_popover_and_a_new_event_button(self):
        html = self.home('month')[0].content.decode()
        self.assertIn('cal-dayev', html)
        self.assertIn('data-cal-newday', html)
        self.assertIn('cal-daypop', html)                                                    # the script that builds the popover
        self.assertIn('+ New event on this day', html)

    def test_on_a_phone_the_month_chips_fold_into_dots(self):
        html = self.home('month')[0].content.decode()
        self.assertIn('.cal-mg .cal-chip, .cal-mg .cal-more { display: none; }', html)
        self.assertIn('.cal-mg .cal-dots { display: flex; }', html)
        self.assertIn('.cal-mg .d { min-height: 52px; }', html)

    def test_the_phone_agenda_still_renders_in_every_horizon(self):
        for horizon in ('today', 'week', 'month'):
            html = self.home(horizon)[0].content.decode()
            self.assertIn('data-m-agenda', html, horizon)


@override_settings(STORAGES=STORAGES)
class TheLinesStripTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user('lines@example.com', 'lines@example.com', 'pw12345678')
        self.staff = StaffProfile.objects.create(user=self.user, role='', new_face=True)
        self.client.force_login(self.user)
        T = Property.Type
        for name, kind, extra in (('Grand', T.ASSOCIATION, {}), ('Glen', T.ASSOCIATION, {}), ('Bethesda Park', T.ASSOCIATION, {'is_general': True}), ('Associations (general)', T.ASSOCIATION, {'is_general': True}),
                                  ('800 Tropic', T.SHORT_TERM_RENTAL, {}), ('225 NE 1st St', T.LONG_TERM_RENTAL, {}), ('Old Duplex', T.LONG_TERM_RENTAL, {'is_active': False}),
                                  ('324 Lofts', T.COMMERCIAL, {}), ('Commercial (general)', T.COMMERCIAL, {'is_general': True})):
            Property.objects.create(name=name, property_type=kind, **extra)

    def request_for(self, path='/w/home/'):
        return type('R', (), {'path': path, 'user': self.user})

    def test_each_line_has_an_icon_and_a_count_of_its_real_active_properties(self):
        got = {l['slug']: l for l in menu.lines(self.request_for(), self.staff)}
        self.assertEqual({k: v['count'] for k, v in got.items()}, {'associations': 3, 'short-term-rentals': 1, 'long-term-rentals': 1, 'commercial': 1})     # no placeholders, no inactive
        self.assertEqual({k: v['icon'] for k, v in got.items()}, {'associations': 'landmark', 'short-term-rentals': 'home', 'long-term-rentals': 'key-round', 'commercial': 'store'})

    def test_the_counts_are_kept_for_a_couple_of_minutes_not_asked_for_on_every_page(self):
        menu.lines(self.request_for(), self.staff)
        with self.assertNumQueries(0):
            menu.lines(self.request_for(), self.staff)

    def test_the_strip_is_drawn_as_buttons_with_icons_counts_and_the_current_line_filled(self):
        html = self.client.get('/w/lines/commercial/').content.decode()
        strip = html.split('class="ws-lines"')[1].split('</div></div>')[0]
        self.assertEqual(strip.count('data-lucide='), 4)
        self.assertIn('<span class="ws-line-n">3</span>', strip)
        self.assertEqual(strip.count('class="on"'), 1)
        self.assertIn('class="on"><i data-lucide="store"', strip)
        css = html.split('.ws-lines a {')[1].split('}')[0]
        self.assertIn('border-radius: 999px', css)
        self.assertIn('font-weight: 600', css)
        self.assertIn('var(--brand-slate)', css)

    def test_the_strip_wraps_and_fits_a_phone(self):
        html = self.client.get('/w/home/').content.decode()
        self.assertIn('.ws-lines .container-fluid { display: flex; flex-wrap: wrap;', html)
        self.assertIn('@media (max-width: 575px) { .ws-lines a {', html)

    def test_a_problem_counting_never_takes_the_page_down(self):
        cache.clear()
        with mock.patch('core.views._is_placeholder', side_effect=RuntimeError('boom')):
            response = self.client.get('/w/home/')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('ws-line-n', response.content.decode().split('class="ws-lines"')[1].split('</div></div>')[0])
