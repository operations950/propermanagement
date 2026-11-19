"""The home screen reads Google Calendar on every load, a round trip for the calendar list and for each chosen calendar: what was read is kept for a minute, never when the read failed, and a
write to the calendar drops it."""
from types import SimpleNamespace
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from . import google_calendar as gc

EVENTS = ([{'id': 'e1', 'summary': 'Meeting', '_calendar_id': 'a'}], [{'id': 'a', 'summary': 'Mine', 'enabled': True, 'error': ''}])


@override_settings(GOOGLE_CALENDAR_CACHE_SECONDS=60)
class TheCalendarIsKeptBriefly(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.token = SimpleNamespace(pk=7, enabled_calendar_ids=['a'], staff='pat')

    def test_a_second_look_does_not_go_back_to_google(self):
        with mock.patch.object(gc, '_fetch_upcoming_events', return_value=EVENTS) as fetch:
            first = gc.get_upcoming_events(self.token)
            second = gc.get_upcoming_events(self.token)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(first, second)

    def test_each_caller_gets_its_own_copy(self):
        with mock.patch.object(gc, '_fetch_upcoming_events', return_value=([{'id': 'e1'}], [{'id': 'a'}])):
            first, _ = gc.get_upcoming_events(self.token)
            first[0]['changed'] = True
            second, _ = gc.get_upcoming_events(self.token)
        self.assertNotIn('changed', second[0])

    def test_a_failed_read_is_not_kept(self):
        with mock.patch.object(gc, '_fetch_upcoming_events', return_value=([], [])) as fetch:
            gc.get_upcoming_events(self.token)
            gc.get_upcoming_events(self.token)
        self.assertEqual(fetch.call_count, 2)

    def test_a_different_window_or_calendar_choice_is_a_different_read(self):
        with mock.patch.object(gc, '_fetch_upcoming_events', return_value=EVENTS) as fetch:
            gc.get_upcoming_events(self.token)
            gc.get_upcoming_events(self.token, days_ahead=6)
            self.token.enabled_calendar_ids = ['a', 'b']
            gc.get_upcoming_events(self.token)
        self.assertEqual(fetch.call_count, 3)

    def test_forgetting_drops_what_was_kept(self):
        with mock.patch.object(gc, '_fetch_upcoming_events', return_value=EVENTS) as fetch:
            gc.get_upcoming_events(self.token)
            gc.forget_events(self.token)
            gc.get_upcoming_events(self.token)
        self.assertEqual(fetch.call_count, 2)

    def test_a_write_to_the_calendar_drops_it(self):
        with mock.patch.object(gc, '_fetch_upcoming_events', return_value=EVENTS) as fetch, mock.patch.object(gc, '_service_for', return_value=object()):
            gc.get_upcoming_events(self.token)
            gc._run_write(self.token, lambda service: 'done')
            gc.get_upcoming_events(self.token)
        self.assertEqual(fetch.call_count, 2)

    @override_settings(GOOGLE_CALENDAR_CACHE_SECONDS=0)
    def test_with_the_setting_at_zero_every_look_goes_to_google(self):
        with mock.patch.object(gc, '_fetch_upcoming_events', return_value=EVENTS) as fetch:
            gc.get_upcoming_events(self.token)
            gc.get_upcoming_events(self.token)
        self.assertEqual(fetch.call_count, 2)
