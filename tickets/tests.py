from datetime import datetime, timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from core import google_calendar
from tickets.views import _format_calendar_events


def event(title, start, end, **extra):
    return dict({'id': title, 'summary': title, 'start': {'dateTime': start.isoformat()}, 'end': {'dateTime': end.isoformat()}, '_calendar_id': 'primary'}, **extra)


class HomeCalendarShowsTheWholeDayTests(TestCase):
    """An event that is over stays on today's calendar (greyed) instead of vanishing."""

    def at(self, hour, minute=0, days=0):
        d = timezone.localdate() + timedelta(days=days)
        return timezone.make_aware(datetime(d.year, d.month, d.day, hour, minute))

    def test_events_that_have_ended_are_marked_past_and_the_rest_are_not(self):
        now = self.at(12)
        with mock.patch('django.utils.timezone.now', return_value=now):
            days = _format_calendar_events([event('Done', self.at(9), self.at(10)), event('Now', self.at(11, 30), self.at(12, 30)), event('Later', self.at(15), self.at(16)),
                                            event('Tomorrow', self.at(9, days=1), self.at(10, days=1))], days_ahead=1)
        today = {e['title']: e['past'] for e in days[0]['timed']}
        self.assertEqual(today, {'Done': True, 'Now': False, 'Later': False})
        self.assertEqual([e['past'] for e in days[1]['timed']], [False])

    def test_the_feed_asks_google_from_the_start_of_today_not_from_now(self):
        token = mock.Mock(enabled_calendar_ids=['primary'])
        service = mock.MagicMock()
        service.calendarList().list().execute.return_value = {'items': [{'id': 'primary', 'summary': 'Me', 'primary': True}]}
        service.events().list().execute.return_value = {'items': []}
        with mock.patch('core.google_calendar.credentials_for'), mock.patch('googleapiclient.discovery.build', return_value=service):
            google_calendar.get_upcoming_events(token, days_ahead=0)
        kwargs = service.events().list.call_args.kwargs
        start = datetime.fromisoformat(kwargs['timeMin'])
        local = timezone.localtime(start)
        self.assertEqual((local.date(), local.hour, local.minute), (timezone.localdate(), 0, 0))

    def test_a_past_event_is_drawn_greyed_on_the_page(self):
        from django.template.loader import render_to_string
        now = self.at(12)
        with mock.patch('django.utils.timezone.now', return_value=now):
            days = _format_calendar_events([event('Done', self.at(9), self.at(10)), event('Later', self.at(15), self.at(16))], days_ahead=0)
        html = render_to_string('tickets/_dashboard_calendar.html', {'calendar_token': mock.Mock(google_email='a@b.c'), 'calendar_days': days, 'available_calendars': [], 'timezone_choices': []})
        self.assertIn('Done</div>', html)
        self.assertIn('9:00 AM · done', html)
        self.assertNotIn('3:00 PM · done', html)
