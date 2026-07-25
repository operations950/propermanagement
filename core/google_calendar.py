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
    """Today's remaining events plus the next `days_ahead` days, from
    whichever of the staff member's own Google calendars they've chosen to
    pull in (token.enabled_calendar_ids — empty means "just the primary
    calendar", the default before anyone's touched the picker on the
    dashboard). Returns (events, available_calendars) — the second list is
    every calendar the account has access to regardless of what's enabled,
    for the dashboard's calendar-picker bubble pool. Each event dict gets a
    `_calendar_id` key so the caller can tell which calendar it came from
    (see tickets/views.py's _format_calendar_events / _calendar_color).
    Returns ([], []) (logged) on any failure — a broken calendar connection
    shouldn't break the dashboard."""
    from datetime import timedelta

    from googleapiclient.discovery import build

    try:
        creds = credentials_for(token)
        service = build('calendar', 'v3', credentials=creds, cache_discovery=False)
        now = timezone.now()
        time_max = timezone.make_aware(
            timezone.datetime.combine(timezone.localdate() + timedelta(days=days_ahead), timezone.datetime.max.time())
        )

        calendar_list = service.calendarList().list().execute().get('items', [])
        available_calendars = [
            {
                'id': c['id'],
                'summary': c.get('summaryOverride') or c.get('summary') or c['id'],
                'is_primary': bool(c.get('primary')),
            }
            for c in calendar_list
        ]

        primary_id = next((c['id'] for c in available_calendars if c['is_primary']), 'primary')
        valid_ids = {c['id'] for c in available_calendars}
        enabled_ids = [cid for cid in (token.enabled_calendar_ids or [primary_id]) if cid in valid_ids]
        if not enabled_ids:
            enabled_ids = [primary_id]

        events = []
        for calendar_id in enabled_ids:
            result = service.events().list(
                calendarId=calendar_id, timeMin=now.isoformat(), timeMax=time_max.isoformat(),
                singleEvents=True, orderBy='startTime', maxResults=50,
            ).execute()
            for item in result.get('items', []):
                item['_calendar_id'] = calendar_id
            events.extend(result.get('items', []))
        return events, available_calendars
    except Exception:
        logger.exception('Google Calendar: failed to fetch events for %s', token.staff)
        return [], []
