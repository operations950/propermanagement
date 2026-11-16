from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import StaffProfile

from . import context_processors, policy, services
from .models import OfficeConnection, PolicySignature, Punch, Status, TimeOff, TimeProfile
from .services import ClockError

STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}
TODAY = date(2026, 11, 20)          # a Friday, after the policy began


def make_person(username, role=StaffProfile.Role.CLEANER, menu_role='cleaner', variable=False, signed=True, days='0,1,2,3,4', applies=True, new_face=True):
    user = get_user_model().objects.create_user(username, f'{username}@example.com', 'pw12345678', first_name=username.title(), last_name='Tester')
    staff = StaffProfile.objects.create(user=user, role=role, new_face=new_face, menu_role=menu_role)
    profile = TimeProfile.objects.create(staff=staff, applies=applies, pay_type='hourly', hire_date=date(2026, 1, 5), work_days=days, variable_hours=variable)
    if signed:
        PolicySignature.objects.create(profile=profile, version=policy.POLICY_VERSION, typed_name='Test Person', text='x')
    return profile


def punch(profile, day, start=time(8, 0), hours=8, lunch=0, remote=False):
    t_in = timezone.make_aware(datetime.combine(day, start))
    return Punch.objects.create(profile=profile, day=day, clock_in=t_in, clock_out=t_in + timedelta(hours=hours + lunch / 60), lunch_minutes=lunch, in_remote=remote)


class NoLateAndNoMissedDayTests(TestCase):
    def setUp(self):
        self.flex = make_person('flex', variable=True)
        self.fixed = make_person('fixed', role=StaffProfile.Role.PROPERTY_MANAGER, menu_role='property_manager')

    def test_a_clock_in_hours_after_nine_is_never_late_for_someone_with_no_schedule(self):
        p_flex, p_fixed = punch(self.flex, date(2026, 11, 16), start=time(12, 30)), punch(self.fixed, date(2026, 11, 16), start=time(12, 30))
        self.assertFalse(services.is_late(p_flex, self.flex))
        self.assertTrue(services.is_late(p_fixed, self.fixed))                              # the same punch is late for someone scheduled 9 to 5
        self.assertEqual(services.late_report(self.flex, TODAY), {'period': 0, 'd30': 0, 'all': 0, 'items': []})
        self.assertEqual(services.late_report(self.fixed, TODAY)['all'], 1)
        report = services.PeriodReport(self.flex, policy.pay_period(TODAY)[0], TODAY)
        self.assertEqual(report.late_count, 0)
        self.assertEqual(services.attendance(self.flex, TODAY)['late'], 0)

    def test_the_board_does_not_show_her_late_even_when_clocked_in_at_noon(self):
        today = timezone.localdate()
        t_in = timezone.make_aware(datetime.combine(today, time(12, 0)))
        Punch.objects.create(profile=self.flex, day=today, clock_in=t_in)
        row = next(r for r in services.today_board([self.flex]) if r['profile'] == self.flex)
        self.assertEqual((row['state'], row['late_in']), ('in', 0))

    def test_a_day_with_no_punch_is_not_a_missed_day_and_nothing_waits_for_a_manager(self):
        self.assertEqual(services.missed_days(self.flex, TODAY), [])
        self.assertGreater(len(services.missed_days(self.fixed, TODAY)), 0)                  # the scheduled person does have missed days
        report = services.PeriodReport(self.flex, policy.pay_period(TODAY)[0], TODAY)
        self.assertEqual(report.blockers, [])
        self.assertFalse(any(r['kind'] == 'missed' for r in report.rows))
        kinds = {(i['kind'], i['profile'].pk) for i in services.collect_items(TODAY)}
        self.assertNotIn(('missed', self.flex.pk), kinds)
        self.assertIn(('missed', self.fixed.pk), kinds)

    def test_with_nobody_in_the_board_says_not_in_yet_without_a_problem_flag(self):
        row = next(r for r in services.today_board([self.flex]) if r['profile'] == self.flex)
        self.assertEqual((row['state'], row['late']), ('flex', 0))


class RemoteStaysVisibleTests(TestCase):
    def setUp(self):
        self.flex = make_person('flex', variable=True)
        OfficeConnection.objects.create(ip='10.0.0.1')

    def test_clocking_in_off_the_office_connection_is_remote_and_needs_a_reason(self):
        with self.assertRaises(ClockError):
            services.clock_in(self.flex, '9.9.9.9')
        p = services.clock_in(self.flex, '9.9.9.9', 'Cleaning at 702')
        self.assertTrue(p.in_remote)
        self.assertEqual(p.remote_reason, 'Cleaning at 702')

    @override_settings(STORAGES=STORAGES)
    def test_the_clock_card_and_the_managers_board_both_say_remote(self):
        services.clock_in(self.flex, '9.9.9.9', 'Cleaning at 702')
        self.client.force_login(self.flex.staff.user)
        card = self.client.get(reverse('ws_home'), REMOTE_ADDR='9.9.9.9').content.decode()
        self.assertIn('Clocked in since', card)
        self.assertIn('>remote<', card)
        boss = get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678')
        self.client.force_login(boss)
        page = self.client.get(reverse('timeclock_manage')).content.decode()
        self.assertIn('variable hours', page)
        self.assertIn('· remote', page)


class VacationFromHoursTests(TestCase):
    def test_she_earns_vacation_from_the_hours_she_works_like_any_hourly_person(self):
        flex = make_person('flex', variable=True)
        for d in range(2, 14):                                                                # twelve days of 6.5 hours, on whatever days she worked
            punch(flex, date(2026, 11, d), hours=6.5)
        worked = Decimal(12) * Decimal('6.5')
        year_one = policy.entitlement_hours(1)
        earned = services.pto(flex, TODAY)['earned']
        self.assertEqual(earned.quantize(Decimal('0.0001')), (worked * year_one / policy.YEAR_HOURS).quantize(Decimal('0.0001')))
        self.assertGreater(earned, 0)

    def test_without_a_hire_date_nothing_is_earned(self):
        flex = make_person('flex', variable=True)
        flex.hire_date = None
        flex.save()
        punch(flex, date(2026, 11, 3))
        self.assertEqual(services.pto(flex, TODAY)['earned'], 0)


class PickedDatesTimeOffTests(TestCase):
    def setUp(self):
        self.flex = make_person('flex', variable=True)

    def test_the_days_she_picks_are_the_days_asked_for_each_worth_eight_hours(self):
        today = timezone.localdate()
        d1, d2 = [d for d in (today + timedelta(days=n) for n in range(10, 40)) if not policy.holiday_name(d)][::3][:2]
        days, error = services.picked_days(self.flex, [d2.isoformat(), '', d1.isoformat(), d1.isoformat()], today)
        self.assertIsNone(error)
        self.assertEqual(days, [d1, d2])
        self.client.force_login(self.flex.staff.user)
        response = self.client.post(reverse('timeclock_time_off'), {'dates': [d1.isoformat(), d2.isoformat()], 'note': 'family'})
        self.assertRedirects(response, reverse('timeclock_home'), fetch_redirect_response=False)
        t = TimeOff.objects.get(profile=self.flex)
        self.assertEqual((t.dates, t.hours, t.status, t.kind), ([d1, d2], 16, Status.PENDING, 'vacation'))
        self.assertEqual(services.pto(self.flex, today)['pending'], 16)

    def test_past_days_closed_days_and_repeats_are_refused_with_a_reason(self):
        today = timezone.localdate()
        self.assertIn('today or later', services.picked_days(self.flex, [(today - timedelta(days=1)).isoformat()], today)[1])
        self.assertIn('closed', services.picked_days(self.flex, ['2026-12-25'], date(2026, 12, 1))[1])
        self.assertIn('Pick at least one', services.picked_days(self.flex, ['', ' '], today)[1])
        day = today + timedelta(days=5)
        TimeOff.objects.create(profile=self.flex, days=[day.isoformat()], start_date=day, end_date=day)
        self.assertIn('already asked', services.picked_days(self.flex, [day.isoformat()], today)[1])

    def test_an_approved_day_shows_as_vacation_and_takes_the_balance_even_with_no_schedule(self):
        day = TODAY + timedelta(days=3)
        TimeOff.objects.create(profile=self.flex, days=[day.isoformat()], start_date=day, end_date=day, status=Status.APPROVED)
        roster = services.Roster([self.flex], day, day)
        self.assertEqual(roster.status(self.flex, day)[0], 'vacation')
        self.assertEqual(services.pto(self.flex, TODAY)['coming'], 8)

    def test_the_change_a_day_page_has_nothing_for_her(self):
        self.client.force_login(self.flex.staff.user)
        response = self.client.get(reverse('timeclock_change'))
        self.assertRedirects(response, reverse('timeclock_home'), fetch_redirect_response=False)

    @override_settings(STORAGES=STORAGES)
    def test_the_time_off_page_asks_for_dates_not_a_range(self):
        self.client.force_login(self.flex.staff.user)
        html = self.client.get(reverse('timeclock_time_off')).content.decode()
        self.assertIn('name="dates"', html)
        self.assertNotIn('name="start"', html)


class ManagerRecordsADayTests(TestCase):
    def setUp(self):
        self.flex = make_person('flex', variable=True)
        self.boss = get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678')
        self.client.force_login(self.boss)

    def test_a_day_off_she_did_not_ask_for_can_be_recorded_as_sick(self):
        day = policy.pay_period(timezone.localdate())[0]
        response = self.client.post(reverse('timeclock_punch'), {'pid': self.flex.pk, 'day': day.isoformat(), 'action': 'day_off', 'kind': 'sick', 'note': 'called in'})
        self.assertEqual(response.status_code, 302)
        t = TimeOff.objects.get(profile=self.flex)
        self.assertEqual((t.kind, t.status, t.asked_by_employee, t.dates), ('sick', Status.APPROVED, False, [day]))
        self.assertTrue(t.takes_balance)

    def test_a_day_with_a_clock_in_cannot_be_recorded_as_time_off(self):
        day = date(2026, 11, 16)
        punch(self.flex, day)
        with self.assertRaises(ClockError):
            services.record_day_off(self.flex, day, 'vacation', self.boss)


class PolicyWallStillAppliesTests(TestCase):
    @override_settings(STORAGES=STORAGES)
    def test_she_sees_nothing_else_until_she_signs_and_the_policy_page_reads_sensibly(self):
        flex = make_person('flex', variable=True, signed=False)
        self.client.force_login(flex.staff.user)
        self.assertRedirects(self.client.get(reverse('ws_home')), reverse('timeclock_policy'), fetch_redirect_response=False)
        self.assertRedirects(self.client.get(reverse('timeclock_home')), reverse('timeclock_policy'), fetch_redirect_response=False)
        page = self.client.get(reverse('timeclock_policy')).content.decode()
        self.assertIn('Your days vary', page)
        self.assertNotIn('none, 9:00', page)
        self.client.post(reverse('timeclock_policy'), {'typed_name': 'Flex Tester', 'agree': 'on'})
        self.assertEqual(self.client.get(reverse('timeclock_home')).status_code, 200)

    def test_the_policy_version_and_text_are_untouched(self):
        self.assertEqual(policy.POLICY_VERSION, 1)


class SetupToggleTests(TestCase):
    @override_settings(STORAGES=STORAGES)
    def test_the_variable_hours_switch_is_saved_and_hides_the_schedule_fields(self):
        flex = make_person('flex')
        boss = get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678')
        self.client.force_login(boss)
        html = self.client.get(reverse('timeclock_setup')).content.decode()
        self.assertIn(f'name="variable_hours" id="variable-{flex.staff.pk}"', html)
        self.assertEqual(html.count('tc-sched" hidden'), 0)                                    # off: the schedule fields show
        self.client.post(reverse('timeclock_setup_save', args=[flex.staff.pk]), {'applies': 'on', 'pay_type': 'hourly', 'hire_date': '2026-01-05', 'variable_hours': 'on',
                                                                                  'days': ['5', '6'], 'day_start': '06:00', 'day_end': '12:00'})
        flex.refresh_from_db()
        self.assertTrue(flex.variable_hours)
        self.assertEqual((flex.work_days, flex.day_start), ('0,1,2,3,4', time(9, 0)))           # the schedule is left as it was, not rewritten
        html = self.client.get(reverse('timeclock_setup')).content.decode()
        self.assertIn('checked', html[html.index(f'id="variable-{flex.staff.pk}"'):][:200])
        self.assertEqual(html.count('tc-sched" hidden'), 3)

    def test_switching_it_off_saves_the_days_again(self):
        flex = make_person('flex', variable=True)
        boss = get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678')
        self.client.force_login(boss)
        self.client.post(reverse('timeclock_setup_save', args=[flex.staff.pk]), {'applies': 'on', 'pay_type': 'hourly', 'days': ['1', '3'], 'day_start': '10:00', 'day_end': '15:00'})
        flex.refresh_from_db()
        self.assertEqual((flex.variable_hours, flex.work_days, flex.day_start), (False, '1,3', time(10, 0)))


class ClockCardTests(TestCase):
    @override_settings(STORAGES=STORAGES)
    def test_the_cleaner_gets_the_big_card_first_on_her_home_and_a_property_manager_does_not(self):
        flex = make_person('flex', variable=True)
        pm = make_person('manager', role=StaffProfile.Role.PROPERTY_MANAGER, menu_role='property_manager')
        outsider = make_person('outsider', applies=False)
        self.client.force_login(flex.staff.user)
        html = self.client.get(reverse('ws_home')).content.decode()
        self.assertIn('tc-clock', html)
        self.assertIn('Clock in', html)
        self.assertLess(html.index('class="card mb-3 tc-clock"'), html.index('class="ws-tabs"'))   # before anything else on the page
        self.client.force_login(pm.staff.user)
        self.assertNotIn('tc-clock', self.client.get(reverse('ws_home')).content.decode())
        self.client.force_login(outsider.staff.user)
        self.assertNotIn('tc-clock', self.client.get(reverse('ws_home')).content.decode())

    def test_a_cleaner_without_the_variable_switch_also_gets_it_but_only_on_the_home_screens(self):
        cleaner = make_person('cleaner')
        rf = RequestFactory()
        request = rf.get('/')
        request.user = cleaner.staff.user
        out = context_processors.nav(request)
        self.assertIn('tc_clock', out)
        self.assertNotIn('tc_card', out)                                                        # the one-line card would only repeat the buttons
        other = rf.get('/tickets/')
        other.user = cleaner.staff.user
        self.assertNotIn('tc_clock', context_processors.nav(other))

    @override_settings(STORAGES=STORAGES)
    def test_clocking_in_and_out_from_the_card_with_lunch_comes_back_to_the_same_screen(self):
        flex = make_person('flex', variable=True)
        self.client.force_login(flex.staff.user)
        response = self.client.post(reverse('timeclock_in'), {'next': reverse('ws_home')})
        self.assertRedirects(response, reverse('ws_home'), fetch_redirect_response=False)
        html = self.client.get(reverse('ws_home')).content.decode()
        self.assertIn('Clocked in since', html)
        self.assertIn('Lunch today', html)
        self.assertIn('Clock out', html)
        self.assertRedirects(self.client.post(reverse('timeclock_out'), {'next': reverse('ws_home')}), reverse('ws_home'), fetch_redirect_response=False)   # no lunch answer: refused
        self.assertIsNone(Punch.objects.get(profile=flex).clock_out)
        self.client.post(reverse('timeclock_out'), {'next': reverse('ws_home'), 'lunch': '30'})
        p = Punch.objects.get(profile=flex)
        self.assertEqual(p.lunch_minutes, 30)
        self.assertIsNotNone(p.clock_out)
        self.assertIn('Done for today', self.client.get(reverse('ws_home')).content.decode())

    def test_next_never_sends_her_to_another_site(self):
        flex = make_person('flex', variable=True)
        self.client.force_login(flex.staff.user)
        response = self.client.post(reverse('timeclock_in'), {'next': 'https://evil.example/'})
        self.assertRedirects(response, reverse('timeclock_home'), fetch_redirect_response=False)


class HolidayForNoScheduleTests(TestCase):
    def test_a_weekday_holiday_needs_a_decision_only_if_she_was_working_in_the_two_weeks_before(self):
        flex = make_person('flex', variable=True)
        thanksgiving = date(2026, 11, 26)
        self.assertEqual(services._holiday_dates(flex, date(2026, 11, 1), date(2026, 11, 30)), [])
        punch(flex, date(2026, 11, 18))
        self.assertEqual(services._holiday_dates(flex, date(2026, 11, 1), date(2026, 11, 30)), [(thanksgiving, 'Thanksgiving'), (thanksgiving + timedelta(days=1), 'Day after Thanksgiving')])


class NewestFirstTests(TestCase):
    """Wherever days or weeks are listed, the latest is on top and the oldest at the bottom."""
    def setUp(self):
        self.person = make_person('flex', variable=True)
        self.start = policy.pay_period(TODAY)[0]
        for offset in (0, 1, 4, 8, 11):
            punch(self.person, self.start + timedelta(days=offset))

    def test_the_weeks_and_each_weeks_days_run_from_the_latest_to_the_oldest(self):
        report = services.PeriodReport(self.person, self.start, TODAY)
        weeks = report.weeks_to_show
        self.assertEqual([w['start'] for w in weeks], sorted((w['start'] for w in report.weeks), reverse=True))
        for w in weeks:
            days = [r['date'] for r in w['rows_newest']]
            self.assertEqual(days, sorted(days, reverse=True))
        days = [r['date'] for r in report.rows_newest]
        self.assertEqual(days, sorted(days, reverse=True))
        self.assertEqual((days[0], days[-1]), (self.start + timedelta(days=13), self.start))

    def test_a_finished_period_is_in_the_same_order_as_one_in_progress(self):
        later = self.start + timedelta(days=30)                                                    # the period is over: no week is "current"
        report = services.PeriodReport(self.person, self.start, later)
        self.assertFalse(any(w['current'] for w in report.weeks))
        self.assertEqual([w['start'] for w in report.weeks_to_show], [self.start + timedelta(days=7), self.start])

    def test_the_csv_lists_the_latest_day_first_with_the_total_last(self):
        report = services.PeriodReport(self.person, self.start, TODAY)
        lines = services.hours_csv([report]).strip().splitlines()
        dates = [ln.split(',')[2] for ln in lines[1:-1]]
        self.assertEqual(dates, sorted(dates, reverse=True))
        self.assertEqual(dates[0], (self.start + timedelta(days=11)).isoformat())
        self.assertEqual(lines[-1].split(',')[2], 'TOTAL')

    @override_settings(STORAGES=STORAGES)
    def test_the_hours_page_shows_today_at_the_top_and_the_oldest_day_at_the_bottom(self):
        import re
        from unittest import mock
        boss = get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678')
        self.client.force_login(boss)
        with mock.patch.object(timezone, 'localdate', return_value=TODAY):
            page = self.client.get(reverse('timeclock_hours')).content.decode()
        shown = re.findall(r'timeclock/punch/?\?pid=\d+&amp;day=(\d{4}-\d{2}-\d{2})|\?pid=\d+&day=(\d{4}-\d{2}-\d{2})', page)
        days = [a or b for a, b in shown]
        self.assertTrue(days)
        self.assertEqual(days[0], TODAY.isoformat())
        self.assertEqual(days, sorted(days, reverse=True))
        self.assertEqual(days[-1], self.start.isoformat())
        weeks = re.findall(r'Week of ([A-Z][a-z]{2} \d+)', page)
        self.assertEqual(weeks, [f'{(self.start + timedelta(days=7)):%b} {(self.start + timedelta(days=7)).day}', f'{self.start:%b} {self.start.day}'])


class HoursAndCsvTests(TestCase):
    def test_her_hours_are_in_the_pay_period_and_the_csv_like_other_hourly_people(self):
        flex = make_person('flex', variable=True)
        start = policy.pay_period(date(2026, 11, 14))[0]
        punch(flex, start + timedelta(days=2), hours=7, lunch=30, remote=True)
        report = services.PeriodReport(flex, start, date(2026, 11, 27))
        self.assertEqual(report.worked_min, 7 * 60 - 0)                                         # 7 hours worked after the 30 minute lunch
        csv_text = services.hours_csv([report])
        self.assertIn('Flex Tester', csv_text)
        self.assertIn('Remote', csv_text)
        self.assertIn('7.00', csv_text)
