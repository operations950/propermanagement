import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect
from django.utils.http import url_has_allowed_host_and_scheme

from . import google_calendar
from .models import GoogleCalendarToken

logger = logging.getLogger(__name__)


def _safe_next(request, default='dashboard'):
    next_url = request.POST.get('next') or request.GET.get('next')
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return next_url
    return redirect(default).url


@login_required
def calendar_connect(request):
    next_url = _safe_next(request)
    if not google_calendar.is_configured():
        messages.error(request, 'Google Calendar isn\'t configured yet — ask an admin to add the OAuth credentials.')
        return redirect(next_url)
    if not hasattr(request.user, 'staff_profile'):
        messages.error(request, 'Your account has no staff profile to attach a calendar to.')
        return redirect(next_url)

    flow = google_calendar.build_flow(request)
    auth_url, state = flow.authorization_url(
        access_type='offline', include_granted_scopes='true', prompt='consent',
    )
    request.session['google_oauth_state'] = state
    request.session['google_oauth_next'] = next_url
    return redirect(auth_url)


@login_required
def calendar_callback(request):
    next_url = request.session.pop('google_oauth_next', None) or 'dashboard'
    if not hasattr(request.user, 'staff_profile'):
        messages.error(request, 'Your account has no staff profile to attach a calendar to.')
        return redirect(next_url)

    state = request.session.pop('google_oauth_state', None)
    if not state or request.GET.get('state') != state:
        messages.error(request, 'Google Calendar connection failed (session expired) — try again.')
        return redirect(next_url)
    if request.GET.get('error'):
        messages.info(request, 'Google Calendar connection cancelled.')
        return redirect(next_url)

    flow = google_calendar.build_flow(request)
    try:
        flow.fetch_token(authorization_response=request.build_absolute_uri())
    except Exception:
        logger.exception('Google Calendar: token exchange failed')
        messages.error(request, 'Google Calendar connection failed — please try again.')
        return redirect(next_url)

    creds = flow.credentials
    email = ''
    if creds.id_token:
        email = creds.id_token.get('email', '') if isinstance(creds.id_token, dict) else ''

    GoogleCalendarToken.objects.update_or_create(
        staff=request.user.staff_profile,
        defaults={
            'refresh_token': creds.refresh_token or '',
            'access_token': creds.token or '',
            'access_token_expires_at': creds.expiry,
            'google_email': email,
        },
    )
    messages.success(request, 'Google Calendar connected.')
    return redirect(next_url)


@login_required
def calendar_disconnect(request):
    next_url = _safe_next(request)
    if request.method == 'POST' and hasattr(request.user, 'staff_profile'):
        GoogleCalendarToken.objects.filter(staff=request.user.staff_profile).delete()
        messages.success(request, 'Google Calendar disconnected.')
    return redirect(next_url)
