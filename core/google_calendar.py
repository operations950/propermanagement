"""Per-staff Google Calendar OAuth + event lookup.

Each staff member connects their own Google account from their department
sub-dashboard (see tickets/views.py's department_dashboard). This is
deliberately separate from the shared-business-calendar concept stubbed in
intake/adapters (GOOGLE_CALENDAR_CREDENTIALS_PATH) — that one (not yet
wired live) would be a single shared calendar read for reactive intake;
this one is many individual personal calendars, read-only, for display
only on each person's own dashboard.
"""
import logging

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/calendar.readonly', 'openid', 'email']


def is_configured():
    return bool(settings.GOOGLE_OAUTH_CLIENT_ID and settings.GOOGLE_OAUTH_CLIENT_SECRET)


def _client_config():
    return {
        'web': {
            'client_id': settings.GOOGLE_OAUTH_CLIENT_ID,
            'client_secret': settings.GOOGLE_OAUTH_CLIENT_SECRET,
            'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
            'token_uri': 'https://oauth2.googleapis.com/token',
        }
    }


def build_flow(request):
    from google_auth_oauthlib.flow import Flow

    redirect_uri = request.build_absolute_uri(reverse('calendar_callback'))
    return Flow.from_client_config(_client_config(), scopes=SCOPES, redirect_uri=redirect_uri)


def redirect_uri_for_display(request):
    """The exact value that must be registered as an Authorized redirect URI
    on the Google Cloud OAuth client — shown to staff on the connect screen
    since a mismatch here is the #1 way this flow fails."""
    return request.build_absolute_uri(reverse('calendar_callback'))


def credentials_for(token):
    """Build a google.oauth2.Credentials from a stored GoogleCalendarToken,
    refreshing the access token first if it's missing or expired."""
    from google.auth.transport.requests import Request as GoogleRequest
    from google.oauth2.credentials import Credentials

    creds = Credentials(
        token=token.access_token or None,
        refresh_token=token.refresh_token,
        token_uri='https://oauth2.googleapis.com/token',
        client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
        client_secret=settings.GOOGLE_OAUTH_CLIENT_SECRET,
        scopes=SCOPES,
    )
    needs_refresh = (
        not token.access_token
        or not token.access_token_expires_at
        or token.access_token_expires_at <= timezone.now()
    )
    if needs_refresh:
        creds.refresh(GoogleRequest())
        token.access_token = creds.token
        if creds.expiry:
            token.access_token_expires_at = timezone.make_aware(creds.expiry) if timezone.is_naive(creds.expiry) else creds.expiry
        token.save(update_fields=['access_token', 'access_token_expires_at'])
    return creds


def get_upcoming_events(token, days_ahead=2):
    """Today's remaining events plus the next `days_ahead` days, for display
    on the owning staff member's dashboard. Returns [] (logged) on any
    failure — a broken calendar connection shouldn't break the dashboard."""
    from datetime import timedelta

    from googleapiclient.discovery import build

    try:
        creds = credentials_for(token)
        service = build('calendar', 'v3', credentials=creds, cache_discovery=False)
        now = timezone.now()
        time_max = timezone.make_aware(
            timezone.datetime.combine(timezone.localdate() + timedelta(days=days_ahead), timezone.datetime.max.time())
        )
        result = service.events().list(
            calendarId='primary', timeMin=now.isoformat(), timeMax=time_max.isoformat(),
            singleEvents=True, orderBy='startTime', maxResults=25,
        ).execute()
        return result.get('items', [])
    except Exception:
        logger.exception('Google Calendar: failed to fetch events for %s', token.staff)
        return []
