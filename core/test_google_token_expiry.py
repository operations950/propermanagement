from datetime import datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from core import google_calendar as gc


def token(expires_in=None, access='stored-token'):
    """A stand-in for the stored Google token. `expires_in` is how long the stored record says the access token has left."""
    saved = []
    t = SimpleNamespace(access_token=access, refresh_token='refresh', access_token_expires_at=(timezone.now() + expires_in) if expires_in is not None else None,
                        save=lambda update_fields=None: saved.append(update_fields), staff='Test Person')
    t.saved = saved
    return t


def refreshed_to(new_token, expiry):
    def refresh(self, request):
        self.token, self.expiry = new_token, expiry
    return mock.patch('google.oauth2.credentials.Credentials.refresh', refresh)


@override_settings(GOOGLE_OAUTH_CLIENT_ID='id', GOOGLE_OAUTH_CLIENT_SECRET='secret')
class CredentialsTests(SimpleTestCase):
    def test_a_token_with_plenty_of_time_left_is_used_as_it_is(self):
        t = token(timedelta(minutes=40))
        with mock.patch('google.oauth2.credentials.Credentials.refresh') as refresh:
            creds = gc.credentials_for(t)
        refresh.assert_not_called()
        self.assertEqual(creds.token, 'stored-token')
        self.assertEqual(t.saved, [])

    def test_an_expired_token_is_refreshed_and_the_new_expiry_is_saved_as_the_utc_time_google_gave(self):
        t = token(-timedelta(minutes=1))
        google_expiry = datetime(2026, 11, 18, 4, 30)                   # google-auth's expiry: a naive UTC time
        with refreshed_to('new-token', google_expiry):
            gc.credentials_for(t)
        self.assertEqual(t.access_token, 'new-token')
        self.assertEqual(t.access_token_expires_at, datetime(2026, 11, 18, 4, 30, tzinfo=dt_timezone.utc))      # not five hours later, as when it was read as New York time
        self.assertEqual(t.saved, [['access_token', 'access_token_expires_at']])

    def test_a_record_that_says_the_token_lasts_for_hours_was_written_in_the_wrong_zone_and_is_refreshed(self):
        t = token(timedelta(hours=4, minutes=30))                        # an access token lasts one hour: this one is already dead
        google_expiry = datetime.utcnow() + timedelta(hours=1)
        with refreshed_to('new-token', google_expiry):
            creds = gc.credentials_for(t)
        self.assertEqual(creds.token, 'new-token')
        self.assertLess(t.access_token_expires_at - timezone.now(), timedelta(hours=1, minutes=1))
        self.assertTrue(t.saved)

    def test_a_token_about_to_expire_is_refreshed_before_the_reads_start(self):
        t = token(timedelta(minutes=2))
        with refreshed_to('new-token', datetime.utcnow() + timedelta(hours=1)):
            creds = gc.credentials_for(t)
        self.assertEqual(creds.token, 'new-token')

    def test_no_stored_token_or_no_expiry_means_a_refresh(self):
        for t in (token(None), token(timedelta(minutes=40), access='')):
            with refreshed_to('new-token', datetime.utcnow() + timedelta(hours=1)):
                self.assertEqual(gc.credentials_for(t).token, 'new-token')
