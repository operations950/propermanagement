"""Google Sign-On for the login screen — separate from google_calendar.py's
per-staff calendar OAuth (that one's post-login, offline, and stores a
refresh token; this one is a one-shot identity check with no stored
credentials, just "does this Google email match an existing staff User").

Deliberately does not auto-create accounts: matching an existing User by
email is the whole check, so a Google account with no corresponding staff
User (email set on their Django User) still can't get in. An admin sets a
staff member's email via Django admin (or the account already has one) to
enable this for them.
"""
from django.conf import settings
from django.urls import reverse

SCOPES = ['openid', 'https://www.googleapis.com/auth/userinfo.email', 'https://www.googleapis.com/auth/userinfo.profile']


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

    redirect_uri = request.build_absolute_uri(reverse('google_login_callback'))
    return Flow.from_client_config(_client_config(), scopes=SCOPES, redirect_uri=redirect_uri)


def email_from_credentials(creds):
    """Returns the verified email from the id_token, or '' if missing/unverified."""
    id_token = creds.id_token
    if not isinstance(id_token, dict):
        return ''
    if not id_token.get('email_verified'):
        return ''
    return id_token.get('email', '')
