import threading
import time
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from core import google_calendar as gc


class FakeHttpError(Exception):
    pass


def fake_service(calendars, events, errors=None, delay=0.0, seen=None):
    """A stand-in for the Google Calendar client: `calendars` is what calendarList returns, `events` {calendar id: [events]}, `errors` {calendar id: exception}."""
    errors = errors or {}

    class Svc:
        def calendarList(self):
            return SimpleNamespace(list=lambda **k: SimpleNamespace(execute=lambda: {'items': calendars}))

        def events(self):
            def lst(calendarId, **k):
                def run():
                    if seen is not None:
                        seen.append((calendarId, threading.get_ident()))
                    time.sleep(delay)
                    if calendarId in errors:
                        raise errors[calendarId]
                    return {'items': [dict(e) for e in events.get(calendarId, [])]}
                return SimpleNamespace(execute=run)
            return SimpleNamespace(list=lst)
    return Svc()


CALS = [{'id': 'me@x.com', 'summary': 'Me', 'primary': True, 'accessRole': 'owner'}, {'id': 'team@x.com', 'summary': 'Team', 'accessRole': 'reader'}, {'id': 'rent@x.com', 'summary': 'Rentals', 'accessRole': 'owner'}]
EVENTS = {'me@x.com': [{'id': 'a'}], 'team@x.com': [{'id': 'b'}, {'id': 'c'}], 'rent@x.com': [{'id': 'd'}]}


def run(enabled, errors=None, delay=0.0, calendars=CALS, seen=None):
    token = SimpleNamespace(enabled_calendar_ids=enabled, staff='Test Person')
    with mock.patch.object(gc, 'credentials_for', return_value=object()), mock.patch.object(gc, '_timed_service', side_effect=lambda creds: fake_service(calendars, EVENTS, errors, delay, seen)):
        return gc.get_upcoming_events(token, days_ahead=6)


class ParallelReadTests(SimpleTestCase):
    """The chosen calendars are read at the same time; what comes back is exactly what the one-after-another read gave."""

    def test_events_come_back_in_the_order_the_calendars_were_chosen(self):
        events, avail = run(['rent@x.com', 'me@x.com', 'team@x.com'])
        self.assertEqual([e['id'] for e in events], ['d', 'a', 'b', 'c'])
        self.assertEqual([e['_calendar_id'] for e in events], ['rent@x.com', 'me@x.com', 'team@x.com', 'team@x.com'])

    def test_every_listed_calendar_is_offered_and_the_chosen_ones_are_marked(self):
        events, avail = run(['me@x.com', 'rent@x.com'])
        by = {c['id']: c for c in avail}
        self.assertEqual(set(by), {'me@x.com', 'team@x.com', 'rent@x.com'})
        self.assertTrue(by['me@x.com']['enabled'] and by['rent@x.com']['enabled'])
        self.assertFalse(by['team@x.com']['enabled'])
        self.assertTrue(by['me@x.com']['is_primary'])
        self.assertEqual(by['team@x.com']['access_role'], 'reader')

    def test_no_chosen_calendar_means_the_primary_one(self):
        events, avail = run([])
        self.assertEqual([e['id'] for e in events], ['a'])
        self.assertEqual({c['id'] for c in avail if c['enabled']}, {'me@x.com'})

    def test_one_calendar_that_google_refuses_never_hides_the_others(self):
        events, avail = run(['me@x.com', 'team@x.com', 'rent@x.com'], errors={'team@x.com': RuntimeError('boom')})
        self.assertEqual([e['id'] for e in events], ['a', 'd'])
        by = {c['id']: c for c in avail}
        self.assertEqual(by['team@x.com']['error'], 'Google would not return its events')
        self.assertEqual(by['me@x.com']['error'], '')

    def test_a_calendar_that_is_no_longer_listed_is_reported_and_not_read(self):
        seen = []
        events, avail = run(['me@x.com', 'gone@x.com'], seen=seen)
        by = {c['id']: c for c in avail}
        self.assertEqual(by['gone@x.com']['error'], 'it is no longer shared with you')
        self.assertEqual([e['id'] for e in events], ['a'])

    def test_the_primary_alias_is_added_when_google_does_not_list_it(self):
        events, avail = run(['primary'], calendars=[])
        self.assertEqual([c['id'] for c in avail], ['primary'])
        self.assertTrue(avail[0]['enabled'])

    def test_the_reads_run_at_the_same_time_not_one_after_another(self):
        seen = []
        started = time.perf_counter()
        events, avail = run(['me@x.com', 'team@x.com', 'rent@x.com'], delay=0.3, seen=seen)
        elapsed = time.perf_counter() - started
        self.assertEqual(len(events), 4)
        self.assertLess(elapsed, 0.75)                                          # three 0.3 s reads one after another would take 0.9 s
        self.assertGreater(len({t for _, t in seen}), 1)                        # and they ran on more than one thread

    def test_a_failure_before_the_events_return_empty_lists_like_before(self):
        token = SimpleNamespace(enabled_calendar_ids=['me@x.com'], staff='Test Person')
        with mock.patch.object(gc, 'credentials_for', side_effect=RuntimeError('no token')):
            self.assertEqual(gc.get_upcoming_events(token), ([], []))

    def test_the_reading_client_has_its_own_http_object_and_a_limit(self):
        with mock.patch('googleapiclient.discovery.build') as build, mock.patch('google_auth_httplib2.AuthorizedHttp') as authed, mock.patch('httplib2.Http') as http:
            gc._timed_service(object())
        http.assert_called_once_with(timeout=10)
        self.assertEqual(build.call_args.kwargs['http'], authed.return_value)
