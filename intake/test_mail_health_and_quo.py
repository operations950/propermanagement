"""Email that cannot be sent because the mailbox is not connected: recorded, a plain message for the person, a banner for admins until it is fixed. And Quo group texts."""
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.mail import EmailMessage
from django.http import HttpResponse
from django.test import TestCase, override_settings
from django.urls import path
from google.auth.exceptions import RefreshError

from core.email_backends import GmailAPIBackend, MailNotConnected
from core.models import StaffProfile
from intake import mail_health, views as intake_views
from intake.models import GmailInboxToken, MailProblem, QuoMessage, QuoThreadState

User = get_user_model()


def boom(request):
    raise MailNotConnected('the mailbox needs to be reconnected')


def fine(request):
    return HttpResponse('ok')


urlpatterns = [path('boom/', boom), path('fine/', fine)]


def staff(email, **flags):
    user = User.objects.create_user(username=email, email=email, password='pw12345678')
    StaffProfile.objects.create(user=user, **flags)
    return user


class BackendTests(TestCase):
    def setUp(self):
        cache.clear()
        GmailInboxToken.objects.create(mailbox_email='admin@x.com', refresh_token='r', is_send_from=True)
        self.message = EmailMessage('Hi', 'Body', 'admin@x.com', ['t@x.com'])

    def test_a_cancelled_sign_in_is_recorded_and_raised_as_not_connected(self):
        with mock.patch('intake.gmail_auth.credentials_for', side_effect=RefreshError('invalid_grant: Token has been expired or revoked.')):
            with self.assertRaises(MailNotConnected):
                GmailAPIBackend(fail_silently=False).send_messages([self.message])
        problem = MailProblem.objects.get()
        self.assertEqual((problem.mailbox_email, problem.reason, problem.fail_count, problem.resolved_at), ('admin@x.com', 'expired', 1, None))

    def test_an_automatic_send_is_recorded_too_and_a_repeat_adds_to_the_same_problem(self):
        with mock.patch('intake.gmail_auth.credentials_for', side_effect=RefreshError('invalid_grant')):
            self.assertEqual(GmailAPIBackend(fail_silently=True).send_messages([self.message]), 0)
            self.assertEqual(GmailAPIBackend(fail_silently=True).send_messages([self.message]), 0)
        problem = MailProblem.objects.get()
        self.assertEqual(problem.fail_count, 2)

    def test_no_mailbox_at_all_is_a_problem_too(self):
        GmailInboxToken.objects.all().delete()
        with self.assertRaises(MailNotConnected):
            GmailAPIBackend(fail_silently=False).send_messages([self.message])
        self.assertEqual(MailProblem.objects.get().reason, 'not_connected')

    def test_a_send_that_works_closes_the_problem(self):
        mail_health.record_failure('admin@x.com', 'expired')
        service = mock.Mock()
        with mock.patch('intake.gmail_auth.credentials_for', return_value=object()), mock.patch('googleapiclient.discovery.build', return_value=service):
            self.assertEqual(GmailAPIBackend(fail_silently=False).send_messages([self.message]), 1)
        self.assertIsNotNone(MailProblem.objects.get().resolved_at)

    def test_another_kind_of_failure_is_still_an_error_not_a_connection_problem(self):
        with mock.patch('intake.gmail_auth.credentials_for', side_effect=ValueError('something else')):
            with self.assertRaises(ValueError):
                GmailAPIBackend(fail_silently=False).send_messages([self.message])
        self.assertFalse(MailProblem.objects.exists())


class BannerTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_only_admins_see_the_banner_and_it_goes_when_the_problem_is_closed(self):
        admin, plain = staff('a@x.com', is_company_admin=True), staff('p@x.com')
        mail_health.record_failure('admin@x.com', 'expired', user=plain)
        self.client.force_login(admin)
        html = self.client.get('/w/home/').content.decode()
        self.assertIn('Email is not connected', html)
        self.assertIn('admin@x.com', html)
        self.assertIn('Reconnect Gmail', html)
        self.client.force_login(plain)
        self.assertNotIn('Email is not connected', self.client.get('/w/home/').content.decode())
        mail_health.resolve('admin@x.com')
        self.client.force_login(admin)
        self.assertNotIn('Email is not connected', self.client.get('/w/home/').content.decode())

    def test_a_reconnect_closes_it(self):
        mail_health.record_failure('admin@x.com', 'expired')
        self.assertEqual(mail_health.resolve('admin@x.com'), 1)
        self.assertIsNone(mail_health.open_problem())


@override_settings(ROOT_URLCONF='intake.test_mail_health_and_quo')
class PersonMessageTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_the_person_gets_a_plain_message_and_goes_back_not_an_error_page(self):
        plain = staff('p@x.com')
        self.client.force_login(plain)
        r = self.client.get('/boom/', HTTP_REFERER='http://testserver/fine/', follow=False)
        self.assertEqual((r.status_code, r['Location']), (302, 'http://testserver/fine/'))
        shown = [str(m) for m in r.wsgi_request._messages]
        self.assertTrue(any('reconnected by an admin, who has been alerted' in m for m in shown), shown)
        self.assertEqual(MailProblem.objects.count(), 0)          # recorded by the send, not by this screen

    def test_an_admin_is_told_where_the_button_is(self):
        admin = staff('a@x.com', is_company_admin=True)
        self.client.force_login(admin)
        r = self.client.get('/boom/', HTTP_REFERER='http://testserver/fine/')
        self.assertTrue(any('red button at the top' in str(m) for m in r.wsgi_request._messages))

    def test_a_script_gets_a_503_with_the_reason(self):
        self.client.force_login(staff('p@x.com'))
        r = self.client.get('/boom/', HTTP_ACCEPT='application/json')
        self.assertEqual(r.status_code, 503)
        self.assertIn('error', r.json())


class QuoGroupTextTests(TestCase):
    def event(self, to):
        return {'type': 'message.received', 'data': {'deepLink': 'https://my.quo.com/inbox/PN1/c/CN123abc', 'object': {
            'id': 'AC1', 'direction': 'incoming', 'from': '+15615551234', 'to': to, 'text': 'hi all', 'phoneNumberId': 'PN1', 'createdAt': '2026-11-16T12:00:00Z'}}}

    def test_a_group_text_with_several_numbers_is_saved(self):
        to = ['+15615550001', '+15615550002', '+15615550003']
        intake_views._process_quo_webhook_event(self.event(to))
        m = QuoMessage.objects.get(message_id='AC1')
        self.assertEqual(m.to_number, ','.join(to))
        self.assertEqual(m.from_number, '+15615551234')

    def test_one_number_in_a_list_or_as_text_is_saved_plain(self):
        intake_views._process_quo_webhook_event(self.event(['+15615550001']))
        self.assertEqual(QuoMessage.objects.get(message_id='AC1').to_number, '+15615550001')
        e = self.event('+15615550009')
        e['data']['object']['id'] = 'AC2'
        intake_views._process_quo_webhook_event(e)
        self.assertEqual(QuoMessage.objects.get(message_id='AC2').to_number, '+15615550009')

    def test_an_outgoing_group_text_keeps_every_number_on_the_thread(self):
        e = self.event(['+15615550001', '+15615550002', '+15615550003'])
        e['data']['object']['direction'] = 'outgoing'
        intake_views._process_quo_webhook_event(e)
        self.assertEqual(QuoThreadState.objects.get(conversation_id='CN123abc').participant, '+15615550001,+15615550002,+15615550003')
