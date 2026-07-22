"""Gmail OAuth for the ONE shared admin@proper-realty.com-style mailbox this
adapter reads. Mirrors core/google_calendar.py's pattern (same OAuth client,
different scopes/redirect/token model) — see that module's docstring for why
this is a deliberately separate concept from per-staff calendars.
"""
import logging

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly', 'openid', 'email']


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

    redirect_uri = request.build_absolute_uri(reverse('gmail_callback'))
    return Flow.from_client_config(_client_config(), scopes=SCOPES, redirect_uri=redirect_uri)


def redirect_uri_for_display(request):
    return request.build_absolute_uri(reverse('gmail_callback'))


def credentials_for(token):
    """Build a google.oauth2.Credentials from a stored GmailInboxToken,
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
