import json
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import gmail_auth
from .models import GmailInboxToken, QuoWebhookLog

logger = logging.getLogger(__name__)

# Temporary, hardcoded shared secret for the test Quo webhook — Quo's docs
# don't document any request-signing mechanism, so this URL-embedded token
# is the only thing standing between this endpoint and the public internet.
# Fine for the discovery phase (see QuoWebhookLog's docstring); revisit if
# this becomes the permanent ingestion path.
QUO_WEBHOOK_TOKEN = 'lzCh81OzYOhib5o7bB014g7feykYB25q'


def _is_admin(user):
    return user.is_superuser


@login_required
@user_passes_test(_is_admin)
def gmail_connect(request):
    """Admin-only: grants this app read access to a shared mailbox (e.g.
    admin@proper-realty.com). Whoever completes Google's consent screen
    must be logged into that mailbox's own Google account — this view just
    starts the flow, it can't grant access to an inbox the person clicking
    "Allow" doesn't control."""
    if not gmail_auth.is_configured():
        messages.error(request, 'Google OAuth isn\'t configured yet — ask an admin to add the credentials.')
        return redirect('dashboard')

    flow = gmail_auth.build_flow(request)
    auth_url, state = flow.authorization_url(
        access_type='offline', include_granted_scopes='true', prompt='consent',
    )
    request.session['gmail_oauth_state'] = state
    return redirect(auth_url)


@login_required
@user_passes_test(_is_admin)
def gmail_callback(request):
    state = request.session.pop('gmail_oauth_state', None)
    if not state or request.GET.get('state') != state:
        messages.error(request, 'Gmail connection failed (session expired) — try again.')
        return redirect('dashboard')
    if request.GET.get('error'):
        messages.info(request, 'Gmail connection cancelled.')
        return redirect('dashboard')

    flow = gmail_auth.build_flow(request)
    try:
        flow.fetch_token(authorization_response=request.build_absolute_uri())
    except Exception:
        logger.exception('Gmail: token exchange failed')
        messages.error(request, 'Gmail connection failed — please try again.')
        return redirect('dashboard')

    creds = flow.credentials
    email = ''
    if creds.id_token and isinstance(creds.id_token, dict):
        email = creds.id_token.get('email', '')
    if not email:
        try:
            from googleapiclient.discovery import build
            profile = build('gmail', 'v1', credentials=creds, cache_discovery=False).users().getProfile(userId='me').execute()
            email = profile.get('emailAddress', '')
        except Exception:
            logger.exception('Gmail: failed to look up connected mailbox address')

    GmailInboxToken.objects.update_or_create(
        mailbox_email=email or 'unknown',
        defaults={
            'refresh_token': creds.refresh_token or '',
            'access_token': creds.token or '',
            'access_token_expires_at': creds.expiry,
        },
    )
    messages.success(request, f'Gmail connected: {email or "mailbox"}.')
    return redirect('dashboard')


@login_required
@user_passes_test(_is_admin)
def gmail_disconnect(request):
    if request.method == 'POST':
        GmailInboxToken.objects.all().delete()
        messages.success(request, 'Gmail disconnected.')
    return redirect('dashboard')


@csrf_exempt
@require_POST
def quo_webhook(request, token):
    """Discovery-phase Quo webhook receiver — see QuoWebhookLog's
    docstring. Just captures the raw payload for inspection; no event
    processing yet, since we don't know the real shape until one lands."""
    if token != QUO_WEBHOOK_TOKEN:
        return HttpResponseForbidden()

    body = request.body.decode('utf-8', errors='replace')
    try:
        parsed = json.loads(body) if body else None
    except ValueError:
        parsed = None

    QuoWebhookLog.objects.create(
        raw_body=body,
        parsed=parsed,
        headers={k: v for k, v in request.headers.items() if k.lower() not in ('cookie', 'authorization')},
    )
    logger.info('Quo webhook received: %s', body[:2000])
    return JsonResponse({'ok': True})


@login_required
@user_passes_test(_is_admin)
def quo_webhook_log(request):
    """Admin-only viewer for captured test-webhook payloads — avoids
    hunting through Railway's log UI for a JSON body Quo POSTed us."""
    logs = QuoWebhookLog.objects.all()[:50]
    return render(request, 'intake/quo_webhook_log.html', {'logs': logs})
