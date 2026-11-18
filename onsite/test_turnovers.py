"""The On-Site Turnovers screen: the status of every unit right now, the seven-day board that starts today, the quiet units hidden, the Day view's boxes, the redirects from the two
screens it replaced, the tabs (Cleaning payments for admins only) and the On-Site Alerts button."""
from datetime import datetime, time, timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import Property, StaffProfile, Unit

from . import alerts
from .models import Booking, GuestRequest, Visit, VisitType
from .services import turnovers

STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}


def at(day, hour, minute=0):
    return timezone.make_aware(datetime.combine(day, time(hour, minute)))


class Base(TestCase):
    """Today is pinned to noon-ish so "right now" is the same on every run: `self.now` is 1 PM today."""

    def setUp(self):
        User = get_user_model()
        self.boss_user = User.objects.create_user('boss', 'b@example.com', 'pw12345678')
        self.boss = StaffProfile.objects.create(user=self.boss_user, role=StaffProfile.Role.ADMIN, is_company_admin=True, new_face=True)
        mu = User.objects.create_user('mgr', 'm@example.com', 'pw12345678', first_name='Mia')
        self.mgr = StaffProfile.objects.create(user=mu, role=StaffProfile.Role.PROPERTY_MANAGER, new_face=True)
        cu = User.objects.create_user('rosa', first_name='Rosa', password='pw12345678')
        self.rosa = StaffProfile.objects.create(user=cu, role=StaffProfile.Role.CLEANER, new_face=True)
        self.today = timezone.localdate()
        self.now = at(self.today, 13)
        self.vt = VisitType.objects.create(name='Turnover', slug='turnover')
        self.client.force_login(self.boss_user)
        self._n = 0
        patch = mock.patch('onsite.google_calendar_push.status', return_value={'ok': True, 'code': 'ok', 'message': ''})
        patch.start()
        self.addCleanup(patch.stop)

    def prop(self, name, unit=None):
        p = Property.objects.create(name=name, property_type='str', street='1 Palm Way', city='Delray Beach', state='FL', zip_code='33444')
        if unit:
            Unit.objects.create(property=p, label=unit)
        return p

    def stay(self, prop, check_in, check_out, **kw):
        self._n += 1
        unit = prop.units.first()
        return Booking.objects.create(property=prop, unit=unit, source='manual', external_uid=f'b{self._n}', guest_name=kw.pop('guest', 'Dana Guest'), check_in=check_in, check_out=check_out,
                                      listing_name=prop.name, **kw)

    def clean(self, booking, status=Visit.Status.SCHEDULED, assigned=True, **kw):
        return Visit.objects.create(property=booking.property, unit=booking.unit, booking=booking, visit_type=self.vt, scheduled_date=timezone.localtime(booking.check_out).date(), status=status,
                                    assigned_staff=self.rosa if assigned else None, **kw)

    def statuses(self, prop):
        got = turnovers.statuses([prop.pk], self.now)
        return got.get((prop.pk, prop.units.first().pk if prop.units.exists() else None), {}).get('status', 'clean')


@override_settings(STORAGES=STORAGES)
class StatusTests(Base):
    """occupied = a guest in it now; dirty = left within three days and the cleaning is not finished; clean = everything else."""

    def test_a_guest_in_the_unit_now_is_occupied(self):
        p = self.prop('800 Tropic', 'B')
        self.stay(p, at(self.today - timedelta(days=2), 15), at(self.today + timedelta(days=1), 11))
        self.assertEqual(self.statuses(p), 'occupied')

    def test_a_check_in_later_today_is_not_occupied_yet_and_a_check_out_later_today_still_is(self):
        later_in, later_out = self.prop('A', 'u'), self.prop('B', 'u')
        self.stay(later_in, at(self.today, 15), at(self.today + timedelta(days=3), 11))
        self.stay(later_out, at(self.today - timedelta(days=2), 15), at(self.today, 14))
        self.assertEqual(self.statuses(later_in), 'clean')
        self.assertEqual(self.statuses(later_out), 'occupied')

    def test_a_unit_left_this_morning_with_no_cleaning_scheduled_is_dirty(self):
        p = self.prop('100 Neptune', 'u')
        self.stay(p, at(self.today - timedelta(days=3), 15), at(self.today, 10))
        self.assertEqual(self.statuses(p), 'dirty')

    def test_an_assigned_or_started_cleaning_is_still_dirty_until_it_is_submitted(self):
        for status in (Visit.Status.UNASSIGNED, Visit.Status.SCHEDULED, Visit.Status.IN_PROGRESS):
            p = self.prop(f'Unit {status}', 'u')
            b = self.stay(p, at(self.today - timedelta(days=3), 15), at(self.today, 10))
            self.clean(b, status, assigned=status != Visit.Status.UNASSIGNED)
            self.assertEqual(self.statuses(p), 'dirty', status)

    def test_a_submitted_or_verified_cleaning_makes_it_clean(self):
        for status in (Visit.Status.SUBMITTED, Visit.Status.VERIFIED):
            p = self.prop(f'Done {status}', 'u')
            b = self.stay(p, at(self.today - timedelta(days=3), 15), at(self.today, 10))
            self.clean(b, status)
            self.assertEqual(self.statuses(p), 'clean', status)

    def test_a_cancelled_cleaning_does_not_count_as_a_cleaning(self):
        p = self.prop('Cancelled', 'u')
        b = self.stay(p, at(self.today - timedelta(days=3), 15), at(self.today, 10))
        self.clean(b, Visit.Status.CANCELLED)
        self.assertEqual(self.statuses(p), 'dirty')

    def test_left_more_than_three_days_ago_with_nothing_recorded_is_called_clean(self):
        p = self.prop('Old', 'u')
        self.stay(p, at(self.today - timedelta(days=9), 15), at(self.today - timedelta(days=5), 10))
        self.assertEqual(self.statuses(p), 'clean')

    def test_a_unit_with_no_bookings_at_all_is_clean(self):
        self.assertEqual(self.statuses(self.prop('Empty', 'u')), 'clean')

    def test_a_cancelled_booking_is_ignored(self):
        p = self.prop('Cancelled stay', 'u')
        self.stay(p, at(self.today - timedelta(days=3), 15), at(self.today, 10), status=Booking.Status.CANCELLED)
        self.assertEqual(self.statuses(p), 'clean')


@override_settings(STORAGES=STORAGES)
class WeekBoardTests(Base):
    def setUp(self):
        super().setUp()
        self.busy = self.prop('800 Tropic', 'B')
        self.quiet = self.prop('224 NW 4th Ave', 'u')          # a guest staying the whole week: no check-in or check-out
        self.empty = self.prop('2821 Frederick', 'u')
        d = self.today
        self.out = self.stay(self.busy, at(d - timedelta(days=3), 15), at(d + timedelta(days=1), 10))
        self.stay(self.quiet, at(d - timedelta(days=4), 15), at(d + timedelta(days=20), 11))

    def w(self, **kw):
        return turnovers.week(kw.pop('start', None), self.today, self.now, **kw)

    def test_the_week_has_seven_days_and_starts_today(self):
        w = self.w()
        self.assertEqual([x['date'] for x in w['days']], [self.today + timedelta(days=i) for i in range(7)])
        self.assertTrue(w['days'][0]['today'] and w['is_current'])

    def test_the_arrows_move_a_whole_week_from_wherever_the_board_starts(self):
        w = self.w()
        self.assertEqual(w['next'], self.today + timedelta(days=7))
        self.assertEqual(w['previous'], self.today - timedelta(days=7))
        later = self.w(start=w['next'])
        self.assertEqual(later['days'][0]['date'], self.today + timedelta(days=7))
        self.assertEqual(later['next'], self.today + timedelta(days=14))
        self.assertFalse(later['is_current'])

    def test_units_with_nothing_checking_in_or_out_are_hidden_and_counted(self):
        w = self.w()
        self.assertEqual([r['property'].name for r in w['rows']], ['800 Tropic'])
        self.assertEqual(w['hidden'], 2)
        self.assertEqual(w['total'], 3)

    def test_show_all_lists_every_unit_and_hides_nothing(self):
        w = self.w(show_all=True)
        self.assertEqual({r['property'].name for r in w['rows']}, {'800 Tropic', '224 NW 4th Ave', '2821 Frederick'})
        self.assertEqual(w['hidden'], 0)

    def test_a_status_filter_shows_only_those_units_even_if_they_would_be_hidden(self):
        self.assertEqual([r['property'].name for r in self.w(status_filter='occupied')['rows']].count('224 NW 4th Ave'), 1)
        clean = self.w(status_filter='clean')
        self.assertEqual({r['property'].name for r in clean['rows']}, {'2821 Frederick'})
        self.assertEqual(self.w(status_filter='nonsense')['filter'], '')

    def test_the_counts_cover_every_unit_not_only_the_ones_drawn(self):
        w = self.w()
        self.assertEqual(w['counts'], {'dirty': 0, 'occupied': 2, 'clean': 1})

    def test_the_rows_are_sorted_by_when_the_first_check_in_or_out_happens_and_not_by_city(self):
        early, late = self.prop('Zeta', 'u'), self.prop('Alpha', 'u')
        for p in (early, late):
            p.city = 'Zebra City' if p is early else 'Apple City'
            p.save()
        self.stay(early, at(self.today - timedelta(days=2), 15), at(self.today, 9))
        self.stay(late, at(self.today - timedelta(days=2), 15), at(self.today + timedelta(days=2), 9))
        names = [r['property'].name for r in self.w()['rows']]
        self.assertEqual(names, ['Zeta', '800 Tropic', 'Alpha'])          # today's 9 AM check-out, tomorrow's 10 AM, then in two days: by when, whatever the city or the name
        self.assertFalse(hasattr(self.w(), 'by_city'))

    def test_a_check_out_and_a_check_in_the_same_day_is_a_same_day_turnover(self):
        self.stay(self.busy, at(self.today + timedelta(days=1), 14), at(self.today + timedelta(days=5), 10))
        row = self.w()['rows'][0]
        cell = row['cells'][1]
        self.assertEqual(cell['kind'], 'both')
        self.assertEqual(len(cell['outs']), 1)
        self.assertEqual(len(cell['ins']), 1)

    def test_a_pending_time_change_shows_as_waiting_for_a_yes(self):
        GuestRequest.objects.create(booking=self.out, kind=GuestRequest.CHECKOUT_KINDS[0], requested_time=time(13, 0), status=GuestRequest.Status.PENDING)
        w = self.w()
        self.assertEqual(w['pending'], 1)
        self.assertEqual(w['rows'][0]['cells'][1]['outs'][0]['state'], 'pending')

    def test_the_cleaning_marks_say_none_unassigned_or_assigned(self):
        self.assertEqual(self.w()['rows'][0]['cells'][1]['outs'][0]['cleaning']['mark'], 'none')
        v = self.clean(self.out, assigned=False)
        self.assertEqual(self.w()['rows'][0]['cells'][1]['outs'][0]['cleaning']['mark'], 'open')
        v.assigned_staff = self.rosa
        v.save()
        self.assertEqual(self.w()['rows'][0]['cells'][1]['outs'][0]['cleaning']['mark'], '')
        self.assertEqual(self.w()['without_cleaning'], 0)

    def test_the_page_draws_the_status_squares_the_counts_and_the_hidden_note(self):
        with mock.patch('django.utils.timezone.now', return_value=self.now):
            html = self.client.get(reverse('onsite_turnovers')).content.decode()
        for word in ('Turnovers', 'Right now:', 'class="st occupied"', '2 occupied', '0 dirty', '1 clean', 'Show all units', 'Same-day turnover'):
            self.assertIn(word, html, word)
        self.assertIn('2 units with no check-in or check-out in these seven days are hidden.', html)
        self.assertNotIn('class="city"', html)
        self.assertNotIn('min gap', html)
        self.assertNotIn('Dana Guest', html)

    def test_the_page_filters_and_shows_all_from_the_address(self):
        show_all = self.client.get(reverse('onsite_turnovers') + '?all=1').content.decode()
        self.assertIn('224 NW 4th Ave', show_all)
        self.assertIn('Showing every unit.', show_all)
        only_clean = self.client.get(reverse('onsite_turnovers') + '?status=clean').content.decode()
        self.assertIn('2821 Frederick', only_clean)
        self.assertNotIn('>800 Tropic</a>', only_clean.split('class="pgscroll"')[1])

    def test_the_week_moves_by_the_start_parameter(self):
        later = self.today + timedelta(days=7)
        r = self.client.get(reverse('onsite_turnovers') + f'?start={later:%Y-%m-%d}')
        self.assertEqual(r.context['w']['start'], later)


@override_settings(STORAGES=STORAGES)
class DayViewTests(Base):
    def setUp(self):
        super().setUp()
        d = self.today
        self.turn = self.prop('800 Tropic', 'B')                                             # leaves at 2 PM, a new guest arrives at 4:30 PM: a same-day turnover
        self.b_out = self.stay(self.turn, at(d - timedelta(days=3), 15), at(d, 14), guest='Out Guest')
        self.b_in = self.stay(self.turn, at(d, 16, 30), at(d + timedelta(days=4), 10), guest='In Guest')
        self.clean(self.b_out, Visit.Status.SCHEDULED, scheduled_start=time(14, 30))
        self.done = self.prop('100 Neptune', 'u')                                             # left at 10, already cleaned
        b = self.stay(self.done, at(d - timedelta(days=3), 15), at(d, 10))
        self.clean(b, Visit.Status.SUBMITTED)
        self.dirty = self.prop('803 NE 7th Ave', 'u')                                         # left yesterday, the cleaning is this afternoon
        b = self.stay(self.dirty, at(d - timedelta(days=4), 15), at(d - timedelta(days=1), 11))
        self.clean(b, Visit.Status.SCHEDULED, scheduled_start=time(14, 0))
        self.vacant = self.prop('2821 Frederick', 'u')
        self.occupied = self.prop('716 Kittyhawk', 'u')
        self.stay(self.occupied, at(d - timedelta(days=1), 15), at(d + timedelta(days=3), 10), guest='Secret Name')
        self.arriving = self.prop('4606 Brady', 'u')
        self.stay(self.arriving, at(d, 16), at(d + timedelta(days=3), 11))

    def day(self, day=None):
        return turnovers.day_view(day or self.today, self.today, self.now)

    def names(self, box, day=None):
        return [x['name'] for x in self.day(day)[box]]

    def test_the_boxes_sort_the_units_by_what_they_are_doing(self):
        self.assertEqual(self.names('out'), ['100 Neptune', '800 Tropic'])             # by time: 10 AM, then 2 PM
        self.assertEqual(self.names('in'), ['4606 Brady', '800 Tropic'])               # 4:00 PM, then 4:30 PM
        self.assertEqual(self.names('dirty'), ['803 NE 7th Ave'])
        self.assertEqual(self.names('clean'), ['2821 Frederick'])
        self.assertEqual(self.names('occupied'), ['716 Kittyhawk'])

    def test_the_summary_counts_out_in_cleanings_done_turnovers_and_guests_staying(self):
        s = self.day()['summary']
        self.assertEqual(s, {'out': 2, 'in': 2, 'cleanings_done': 1, 'turnovers': 1, 'staying': 1})

    def test_a_turnover_is_flagged_on_both_its_rows(self):
        d = self.day()
        self.assertTrue(next(x for x in d['out'] if x['name'] == '800 Tropic')['same_day'])
        self.assertTrue(next(x for x in d['in'] if x['name'] == '800 Tropic')['same_day'])
        self.assertFalse(next(x for x in d['in'] if x['name'] == '4606 Brady')['same_day'])

    def test_a_check_in_says_whether_the_unit_will_be_ready(self):
        by = {x['name']: x for x in self.day()['in']}
        self.assertEqual(by['800 Tropic']['ready'], 'cleaning 2:30p · Rosa')          # the cleaning behind the same-day turnover
        self.assertEqual(by['4606 Brady']['ready'], 'clean & ready')

    def test_the_cleaning_badge_on_a_check_out_says_cleaned_or_who_is_on_it(self):
        by = {x['name']: x for x in self.day()['out']}
        self.assertEqual(by['100 Neptune']['e']['cleaning']['badge'], 'cleaned')
        self.assertEqual(by['800 Tropic']['e']['cleaning']['badge'], 'cleaning 2:30p · Rosa')

    def test_occupied_shows_the_check_out_day_and_no_guest_name(self):
        x = self.day()['occupied'][0]
        self.assertEqual(x['out_day'], self.today + timedelta(days=3))
        html = self.client.get(reverse('onsite_turnovers') + f'?day={self.today:%Y-%m-%d}').content.decode()
        self.assertNotIn('Secret Name', html)
        self.assertIn(f'out {(self.today + timedelta(days=3)):%a %b} {(self.today + timedelta(days=3)).day}', html)

    def test_a_later_day_calls_a_unit_dirty_only_when_nothing_cleans_it(self):
        tomorrow = self.today + timedelta(days=1)
        d = self.day(tomorrow)
        self.assertNotIn('803 NE 7th Ave', [x['name'] for x in d['dirty']])             # its cleaning is scheduled: by tomorrow it is done
        unplanned = self.prop('No cleaning', 'u')
        self.stay(unplanned, at(self.today - timedelta(days=3), 15), at(self.today, 11))
        self.assertIn('No cleaning', [x['name'] for x in self.day(tomorrow)['dirty']])

    def test_an_earlier_day_has_no_dirty_box_entries(self):
        self.assertEqual(self.day(self.today - timedelta(days=2))['dirty'], [])

    def test_the_page_draws_every_box_with_times_and_the_same_day_badge(self):
        with mock.patch('django.utils.timezone.now', return_value=self.now):
            html = self.client.get(reverse('onsite_turnovers') + f'?day={self.today:%Y-%m-%d}').content.decode()
        for word in ('Checking out', 'Checking in', 'Needs cleaning', 'Clean &amp; ready', 'Occupied &middot; staying through', 'same day', 'out 2p', 'in 4:30p', 'clean &amp; ready', 'cleaned',
                     'Previous day', 'Next day', '2 out', '2 in'):
            self.assertIn(word, html, word)
        self.assertNotIn('min gap', html)
        self.assertNotIn('Out Guest', html)
        self.assertIn('data-wk-time', html)                                                # a time is clickable
        self.assertIn('class="paper"', html)

    def test_a_time_change_waiting_for_a_yes_has_the_dotted_outline(self):
        GuestRequest.objects.create(booking=self.b_out, kind=GuestRequest.CHECKOUT_KINDS[0], requested_time=time(15, 0), status=GuestRequest.Status.PENDING)
        html = self.client.get(reverse('onsite_turnovers') + f'?day={self.today:%Y-%m-%d}').content.decode()
        self.assertIn('b out pending', html)

    def test_the_day_view_links_back_to_the_week_and_to_the_neighbouring_days(self):
        html = self.client.get(reverse('onsite_turnovers') + f'?day={self.today:%Y-%m-%d}').content.decode()
        self.assertIn(f'?day={(self.today + timedelta(days=1)):%Y-%m-%d}', html)
        self.assertIn(f'?day={(self.today - timedelta(days=1)):%Y-%m-%d}', html)
        self.assertIn(f'?start={self.today:%Y-%m-%d}', html)


@override_settings(STORAGES=STORAGES)
class RedirectAndTabTests(Base):
    def test_the_old_rentals_today_address_opens_the_day_view(self):
        r = self.client.get(reverse('onsite_str_today'))
        self.assertRedirects(r, f'{reverse("onsite_turnovers")}?day={self.today:%Y-%m-%d}', fetch_redirect_response=False)
        r = self.client.get(reverse('onsite_str_today') + '?date=2026-12-24')
        self.assertRedirects(r, f'{reverse("onsite_turnovers")}?day=2026-12-24', fetch_redirect_response=False)

    def test_the_old_properties_board_address_is_the_turnovers_screen(self):
        self.assertRedirects(self.client.get(reverse('ws_week')), reverse('onsite_turnovers'), fetch_redirect_response=False)
        later = self.today + timedelta(days=7)
        self.assertRedirects(self.client.get(reverse('ws_week') + f'?start={later:%Y-%m-%d}'), f'{reverse("onsite_turnovers")}?start={later:%Y-%m-%d}', fetch_redirect_response=False)

    def test_the_tabs_are_visits_turnovers_calendar_and_performance(self):
        html = self.client.get(reverse('onsite_dashboard')).content.decode()
        row = html.split('<nav class="np-seg"')[1].split('</nav>')[0]                       # the same segmented switch as Tickets, Compliance and Projects
        labels = [l for l in ('Visits', 'Turnovers', 'Calendar', 'Performance') if f'>{l}' in row]
        self.assertEqual(labels, ['Visits', 'Turnovers', 'Calendar', 'Performance'])
        for gone in ('>Properties<', '>Requests', '>Reservations<'):
            self.assertNotIn(gone, row)
        for name in ('onsite_dashboard', 'onsite_turnovers', 'onsite_rentals_calendar', 'onsite_performance'):
            self.assertIn(f'href="{reverse(name)}"', row, name)

    def test_cleaning_payments_is_not_a_permanent_link_in_the_header_but_stays_a_button_for_admins_on_the_visits_page(self):
        row = self.client.get(reverse('onsite_dashboard')).content.decode().split('<nav class="np-seg"')[1].split('</nav>')[0]
        self.assertNotIn(reverse('onsite_cleaning_payments'), row)
        self.assertIn(f'href="{reverse("onsite_cleaning_payments")}"', self.client.get(reverse('onsite_dashboard')).content.decode())          # the admin's button on the Visits page
        self.client.force_login(self.mgr.user)
        self.assertNotIn(f'href="{reverse("onsite_cleaning_payments")}"', self.client.get(reverse('onsite_dashboard')).content.decode())      # a manager without the box

    def test_the_header_and_tabs_match_tickets_compliance_and_projects(self):
        html = self.client.get(reverse('onsite_dashboard')).content.decode()
        self.assertIn('class="np-head"', html)
        self.assertIn('lucide="spray-can"', html)
        self.assertIn('class="btn-action"', html.split('class="np-head"')[1].split('</nav>')[0])        # New visit, the shared action button
        self.assertIn('css/newspaper.css', html)
        for name in ('onsite_turnovers', 'onsite_rentals_calendar', 'onsite_performance'):
            self.assertIn('class="np-seg"', self.client.get(reverse(name)).content.decode(), name)

    def test_every_tab_screen_carries_the_tab_row_and_the_alerts_button(self):
        for name in ('onsite_dashboard', 'onsite_turnovers', 'onsite_rentals_calendar', 'onsite_performance', 'onsite_cleaning_payments'):
            html = self.client.get(reverse(name)).content.decode()
            self.assertIn('class="np-seg"', html, name)
            self.assertIn('id="os-alerts-btn"', html, name)

    def test_the_visits_page_keeps_its_buttons_and_loses_the_alert_banners(self):
        html = self.client.get(reverse('onsite_dashboard')).content.decode()
        for name in ('onsite_visit_create', 'onsite_reservation_list', 'onsite_checklist_templates', 'onsite_checklist_custom_items', 'onsite_cleaner_activity', 'onsite_booking_feeds',
                     'onsite_visit_rule_list'):
            self.assertIn(f'href="{reverse(name)}"', html, name)
        for gone in ('Needs attention today', 'Google Calendar isn', 'need attention.'):
            self.assertNotIn(gone, html, gone)
        self.assertNotIn('things need attention in On-Site', html)

    def test_the_old_screen_and_its_template_are_gone(self):
        from pathlib import Path

        from django.conf import settings
        self.assertFalse((Path(settings.BASE_DIR) / 'onsite' / 'templates' / 'onsite' / 'str_today.html').exists())
        from . import views
        self.assertFalse(hasattr(views, 'str_today'))

    def test_changing_a_time_and_scheduling_a_cleaning_come_back_to_the_turnovers_screen(self):
        p = Property.objects.create(name='X', property_type='str', street='1 Palm Way', city='Delray Beach', state='FL', zip_code='33444')
        b = Booking.objects.create(property=p, source='manual', external_uid='z1', check_in=at(self.today - timedelta(days=1), 15), check_out=at(self.today, 11), listing_name='X')
        r = self.client.post(reverse('onsite_turnovers_time'), {'booking_id': b.pk, 'kind': 'checkout', 'action': 'remove', 'start': f'{self.today:%Y-%m-%d}'})
        self.assertRedirects(r, f'{reverse("onsite_turnovers")}?start={self.today:%Y-%m-%d}', fetch_redirect_response=False)
        r = self.client.post(reverse('onsite_turnovers_clean'), {'booking_id': b.pk, 'day': f'{self.today:%Y-%m-%d}'})
        self.assertRedirects(r, f'{reverse("onsite_turnovers")}?day={self.today:%Y-%m-%d}', fetch_redirect_response=False)

    def test_the_actions_need_a_post(self):
        self.assertEqual(self.client.get(reverse('onsite_turnovers_time')).status_code, 405)
        self.assertEqual(self.client.get(reverse('onsite_turnovers_clean')).status_code, 405)


@override_settings(STORAGES=STORAGES)
class AlertsTests(Base):
    def keys(self, user=None):
        return {a['key']: a for a in alerts.collect(user or self.boss_user, self.today)}

    def test_nothing_to_look_at_gives_an_empty_list_and_a_plain_button(self):
        with mock.patch('onsite.services.feeds.feeds_needing_attention', return_value=[]):
            self.assertEqual(self.keys(), {})
            html = self.client.get(reverse('onsite_dashboard')).content.decode()
        button = html.split('id="os-alerts-btn"')[1].split('</button>')[0]
        self.assertNotIn('btn-alerts-on', button.split('>')[0])
        self.assertIn('btn-outline-secondary', button.split('>')[0])
        self.assertNotIn('alerts-n', button)
        self.assertIn('Nothing on On-Site needs attention.', html)

    def test_reservations_to_review_are_an_alert(self):
        with mock.patch('onsite.services.review.counts', return_value=(2, 1)):
            got = self.keys()
        self.assertEqual(got['reservations']['count'], 3)
        self.assertEqual(got['reservations']['url'], reverse('onsite_reservation_review'))

    def test_booking_calendar_lines_and_google_are_for_admins_only(self):
        with mock.patch('onsite.services.feeds.feeds_needing_attention', return_value=[1, 2]), \
                mock.patch('onsite.google_calendar_push.status', return_value={'ok': False, 'code': 'error', 'message': 'It broke'}):
            admin = self.keys()
            manager = self.keys(self.mgr.user)
        self.assertEqual(admin['feeds']['count'], 2)
        self.assertEqual(admin['google']['detail'], 'It broke')
        self.assertNotIn('feeds', manager)
        self.assertNotIn('google', manager)

    def test_todays_checkouts_with_no_cleaning_unassigned_visits_and_at_risk_visits(self):
        p = self.prop('800 Tropic', 'B')
        self.stay(p, at(self.today - timedelta(days=2), 15), at(self.today, 11))
        got = self.keys()
        self.assertEqual(got['no_visit']['count'], 1)
        self.assertIn('800 Tropic', got['no_visit']['detail'])
        Visit.objects.create(property=p, visit_type=self.vt, scheduled_date=self.today, status=Visit.Status.UNASSIGNED)
        self.assertEqual(self.keys()['unassigned']['count'], 1)
        Visit.objects.create(property=p, visit_type=self.vt, scheduled_date=self.today, status=Visit.Status.SCHEDULED, assigned_staff=self.rosa, ready_by=timezone.now() + timedelta(minutes=10))
        self.assertEqual(self.keys()['at_risk']['count'], 1)

    def test_the_property_reminders_are_not_here(self):
        self.prop('800 Tropic', 'B')                       # no size details, no listing link, no trash schedule: those belong to the Properties screen's Alerts button
        with mock.patch('onsite.services.feeds.feeds_needing_attention', return_value=[]):
            self.assertEqual(set(self.keys()), set())

    def test_the_button_is_orange_with_a_count_when_there_is_something_to_look_at(self):
        with mock.patch('onsite.services.review.counts', return_value=(1, 0)):
            html = self.client.get(reverse('onsite_dashboard')).content.decode()
        button = html.split('id="os-alerts-btn"')[1].split('</button>')[0]
        self.assertIn('btn-alerts-on', button.split('>')[0])
        self.assertIn('<span class="alerts-n">1</span>', button)
        self.assertNotIn('accent-contrast', html.split('.os-panel {')[1].split('}')[0])           # the shared orange style, not a colour of its own
        self.assertIn('id="os-alerts-panel" hidden', html)                                       # closed until it is pressed
        self.assertIn('1 reservation to review', html)

    def test_a_collector_that_fails_never_takes_the_page_down(self):
        with mock.patch('onsite.alerts._reservations', side_effect=RuntimeError('boom')), mock.patch.object(alerts, 'COLLECTORS', (alerts._today,)):
            self.assertEqual(self.client.get(reverse('onsite_dashboard')).status_code, 200)
        with mock.patch('onsite.services.review.counts', side_effect=RuntimeError('boom')):
            self.assertEqual(self.client.get(reverse('onsite_dashboard')).status_code, 200)

    def test_the_page_alerts_strip_is_gone_from_the_visits_page(self):
        html = self.client.get(reverse('onsite_dashboard')).content.decode()
        self.assertNotIn('bell-ring" class="icon"></i> ', html.split('class="np-seg"')[0])
        self.assertNotIn('need attention in On-Site', html)
