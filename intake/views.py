import base64
import hashlib
import hmac as hmac_lib
import json
import logging
import re

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect, render
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import gmail_auth
from .models import GmailInboxToken, QuoMessage, QuoThreadState, QuoWebhookLog

logger = logging.getLogger(__name__)

# Fallback values for the two Quo webhook secrets below, from before they
# were made overridable via /admin-tools/ (see core/app_settings.py) —
# kept as the default so nothing breaks for a deploy where no AppSetting
# row exists yet. Prefer _quo_webhook_token()/_quo_webhook_signing_key()
# over reading these directly.
_QUO_WEBHOOK_TOKEN_FALLBACK = 'lzCh81OzYOhib5o7bB014g7feykYB25q'
_QUO_WEBHOOK_SIGNING_KEY_FALLBACK = 'V2gxdFZFQzlHRlRzT2JaMXBHekd5ZG1wMmswVW1zdTQ='


def _quo_webhook_token():
    return getattr(settings, 'QUO_WEBHOOK_TOKEN', '') or _QUO_WEBHOOK_TOKEN_FALLBACK


def _quo_webhook_signing_key():
    return getattr(settings, 'QUO_WEBHOOK_SIGNING_KEY', '') or _QUO_WEBHOOK_SIGNING_KEY_FALLBACK

_CONVERSATION_ID_RE = re.compile(r'/c/(CN[0-9a-f]+)')


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
    next_url = request.GET.get('next') if request.GET.get('next') in ('dashboard', 'admin_tools') else 'dashboard'
    if not gmail_auth.is_configured():
        messages.error(request, 'Google OAuth isn\'t configured yet — ask an admin to add the credentials.')
        return redirect(next_url)

    flow = gmail_auth.build_flow(request)
    auth_url, state = flow.authorization_url(
        access_type='offline', include_granted_scopes='true', prompt='consent',
    )
    request.session['gmail_oauth_state'] = state
    request.session['gmail_oauth_next'] = next_url
    return redirect(auth_url)


@login_required
@user_passes_test(_is_admin)
def gmail_callback(request):
    next_url = request.session.pop('gmail_oauth_next', 'dashboard')
    state = request.session.pop('gmail_oauth_state', None)
    if not state or request.GET.get('state') != state:
        messages.error(request, 'Gmail connection failed (session expired) — try again.')
        return redirect(next_url)
    if request.GET.get('error'):
        messages.info(request, 'Gmail connection cancelled.')
        return redirect(next_url)

    flow = gmail_auth.build_flow(request)
    try:
        flow.fetch_token(authorization_response=request.build_absolute_uri())
    except Exception:
        logger.exception('Gmail: token exchange failed')
        messages.error(request, 'Gmail connection failed — please try again.')
        return redirect(next_url)

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

    # Exactly one shared mailbox is supported (see GmailInboxToken's
    # docstring) — every other reader/writer uses .first()/.exists() with
    # no email filter. Connecting a *different* address than whatever was
    # connected before must replace it, not add a second row (mailbox_email
    # is unique, so update_or_create alone would just create a sibling row
    # keyed on the new address, leaving the old one as the stale thing
    # .first() keeps returning).
    GmailInboxToken.objects.exclude(mailbox_email=email or 'unknown').delete()
    GmailInboxToken.objects.update_or_create(
        mailbox_email=email or 'unknown',
        defaults={
            'refresh_token': creds.refresh_token or '',
            'access_token': creds.token or '',
            'access_token_expires_at': creds.expiry,
        },
    )
    from core.app_settings import _sync_email_backend
    _sync_email_backend()
    messages.success(request, f'Gmail connected: {email or "mailbox"} — now used to send follow-up emails too.')
    return redirect(next_url)


@login_required
@user_passes_test(_is_admin)
def gmail_disconnect(request):
    next_url = request.POST.get('next') if request.POST.get('next') in ('dashboard', 'admin_tools') else 'dashboard'
    if request.method == 'POST':
        GmailInboxToken.objects.all().delete()
        from core.app_settings import _sync_email_backend
        _sync_email_backend()
        messages.success(request, 'Gmail disconnected.')
    return redirect(next_url)


def _verify_quo_signature(request):
    """hmac;<version>;<timestamp>;<base64 sig> in Openphone-Signature —
    Quo is a rebrand of OpenPhone, and this is OpenPhone's own documented
    scheme (their docs don't cover it under the Quo name). Confirmed
    against a real captured event before this was wired in as an actual
    gate rather than just the URL token."""
    header = request.headers.get('Openphone-Signature', '')
    parts = header.split(';')
    if len(parts) != 4 or parts[0] != 'hmac':
        return False
    _scheme, _version, timestamp, provided_sig = parts
    signed_data = f'{timestamp}.{request.body.decode("utf-8", errors="replace")}'
    try:
        key_bytes = base64.b64decode(_quo_webhook_signing_key())
    except (ValueError, TypeError):
        return False
    computed_sig = base64.b64encode(
        hmac_lib.new(key_bytes, signed_data.encode('utf-8'), hashlib.sha256).digest()
    ).decode()
    return hmac_lib.compare_digest(computed_sig, provided_sig)


def _parse_iso(ts):
    if not ts:
        return None
    dt = parse_datetime(ts)
    if dt and timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt


def _process_quo_webhook_event(event):
    """message.received/message.delivered -> a local QuoMessage row (see
    its docstring) plus keeping QuoThreadState in sync so the existing
    poll_quo-based paths (fetch_quo_conversation, caller-identity lookup)
    stay consistent. Anything else (call events, other message statuses)
    is ignored — this app doesn't act on those yet."""
    if event.get('type') not in ('message.received', 'message.delivered'):
        return

    data = event.get('data') or {}
    obj = data.get('object') or {}
    message_id = obj.get('id', '')
    if not message_id:
        return

    match = _CONVERSATION_ID_RE.search(data.get('deepLink', '') or '')
    conversation_id = match.group(1) if match else ''
    direction = QuoMessage.Direction.IN if obj.get('direction') == 'incoming' else QuoMessage.Direction.OUT
    participant = obj.get('from') if direction == QuoMessage.Direction.IN else obj.get('to')

    QuoMessage.objects.get_or_create(message_id=message_id, defaults={
        'conversation_id': conversation_id,
        'phone_number_id': obj.get('phoneNumberId', ''),
        'direction': direction,
        'from_number': obj.get('from', ''),
        'to_number': obj.get('to', ''),
        'body': obj.get('text', ''),
        'quo_created_at': _parse_iso(obj.get('createdAt', '')),
    })

    if conversation_id:
        QuoThreadState.objects.update_or_create(
            conversation_id=conversation_id,
            defaults={
                'phone_number_id': obj.get('phoneNumberId', ''),
                'participant': participant or '',
                'last_message_id': message_id,
            },
        )


@csrf_exempt
@require_POST
def quo_webhook(request, token):
    """Live Quo webhook receiver (message.received/message.delivered) —
    every event is logged verbatim to QuoWebhookLog regardless of
    verification outcome (audit trail), but only signature-verified events
    are actually processed into QuoMessage/QuoThreadState."""
    if token != _quo_webhook_token():
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

    if not _verify_quo_signature(request):
        logger.warning('Quo webhook: signature verification failed — event logged but not processed')
        return JsonResponse({'ok': True})

    if parsed and parsed.get('object') == 'event':
        try:
            _process_quo_webhook_event(parsed)
        except Exception:
            logger.exception('Quo webhook: failed to process event %s', parsed.get('id'))

    return JsonResponse({'ok': True})


@login_required
@user_passes_test(_is_admin)
def quo_webhook_log(request):
    """Admin-only viewer for captured test-webhook payloads — avoids
    hunting through Railway's log UI for a JSON body Quo POSTed us."""
    logs = QuoWebhookLog.objects.all()[:50]
    return render(request, 'intake/quo_webhook_log.html', {'logs': logs})


def _run_command_in_background(name, *args):
    """Kicks off a management command on a daemon thread and returns
    immediately — for admin-triggered commands (backfill, force-classify)
    that can take longer than a request/proxy timeout tolerates. Progress
    is only visible via Railway logs (both commands write their own
    progress lines via self.stdout.write), not this response.

    sys.stdout is block-buffered when it's not a real terminal (true of
    both this dev server's log capture and Railway's process supervisor),
    so self.stdout.write() calls from a long-running background command sit
    in the buffer and never actually reach the log until the buffer fills
    or the whole process exits — for a long-lived web server, that's
    effectively "never." Explicitly flushing after the command finishes (or
    on failure) is what actually gets the output out."""
    import sys
    import threading

    from django.core.management import call_command

    def _run():
        try:
            call_command(name, *args)
        except Exception:
            logger.exception('%s (background, admin-triggered) failed', name)
        finally:
            sys.stdout.flush()
            sys.stderr.flush()

    threading.Thread(target=_run, daemon=True).start()


@login_required
@user_passes_test(_is_admin)
def quo_backfill_trigger(request):
    """Admin-only: kicks off backfill_quo_messages in the background — the
    one-time (safe to re-run) historical sync so classify_quo_conversations
    has more than just what's arrived since the webhook went live. No
    Railway shell access, so this is the only way to run it on production."""
    if request.method == 'POST':
        _run_command_in_background('backfill_quo_messages')
        messages.success(request, 'Quo backfill started in the background — check Railway logs for progress.')
    return redirect('quo_webhook_log')


@login_required
@user_passes_test(_is_admin)
def quo_classify_trigger(request):
    """Admin-only: force an immediate classify_quo_conversations pass
    instead of waiting for its next scheduled run (see
    settings.QUO_CLASSIFY_INTERVAL_MINUTES) — mainly for testing/verifying
    the pipeline without sitting around."""
    if request.method == 'POST':
        _run_command_in_background('classify_quo_conversations')
        messages.success(request, 'Quo classification pass started in the background — check Railway logs for progress.')
    return redirect('quo_webhook_log')


@login_required
@user_passes_test(_is_admin)
def quo_classify_contacts_trigger(request):
    """Admin-only: runs classify_pending_contacts (AI contact_type/property
    suggestions from each pending contact's captured message history) in the
    background — same no-shell-access workaround as the other triggers."""
    if request.method == 'POST':
        _run_command_in_background('classify_pending_contacts')
        messages.success(request, 'Contact classification pass started in the background — check Railway logs for progress.')
    return redirect('quo_webhook_log')


@login_required
@user_passes_test(_is_admin)
def quo_sync_contacts_trigger(request):
    """Admin-only: force an immediate sync_quo_contacts pass instead of
    waiting for its daily schedule (settings.QUO_CONTACT_SYNC_INTERVAL_MINUTES)."""
    if request.method == 'POST':
        _run_command_in_background('sync_quo_contacts')
        messages.success(request, 'Quo contact sync started in the background — check Railway logs for progress.')
    return redirect('quo_webhook_log')


@login_required
@user_passes_test(_is_admin)
def quo_reconcile_candidates_trigger(request):
    """Admin-only, one-time: cleans up pending Quo candidates that a
    phone-format bug in an earlier sync_quo_contacts run mass-staged as
    duplicates of contacts that already existed (see
    reconcile_pending_quo_candidates's docstring)."""
    if request.method == 'POST':
        _run_command_in_background('reconcile_pending_quo_candidates')
        messages.success(request, 'Reconciling pending Quo candidates in the background — check Railway logs for progress.')
    return redirect('quo_webhook_log')


@login_required
@user_passes_test(_is_admin)
def quo_reset_candidates_trigger(request):
    """Admin-only, one-time: wipes the entire pending Quo review queue
    outright (not a reject) so it can be repopulated cleanly by re-running
    the sync now that phone formatting and nameless-contact filtering are
    fixed. See reset_pending_quo_candidates's docstring."""
    if request.method == 'POST':
        _run_command_in_background('reset_pending_quo_candidates')
        messages.success(request, 'Clearing the pending Quo review queue in the background — check Railway logs, then run the sync again.')
    return redirect('quo_webhook_log')


@login_required
@user_passes_test(_is_admin)
def quo_analyze_contacts_count(request):
    """Admin-only: runs analyze_recent_quo_contacts --count-only in the
    background. No longer synchronous — determining "qualifies" now means
    crawling Quo's full conversation history plus a /v1/calls check per
    remaining contact (see quo_contact_activity.py), which a large backlog
    could take well past a request timeout to finish. Check Railway logs
    for the resulting count.

    The include_staged checkbox passes --include-staged through, for the
    one-time case where a review-queue clear was immediately followed by the
    daily sync re-staging the same contacts before this pass ran."""
    if request.method == 'POST':
        args = ['--count-only']
        if request.POST.get('include_staged'):
            args.append('--include-staged')
        _run_command_in_background('analyze_recent_quo_contacts', *args)
        messages.success(
            request,
            'Counting qualifying Quo contacts in the background — check Railway logs for the result.',
        )
    return redirect('contact_review')


@login_required
@user_passes_test(_is_admin)
def quo_analyze_contacts_trigger(request):
    """Admin-only, one-time baseline pass: runs analyze_recent_quo_contacts
    for real (creating ContactImportCandidate rows) in the background — one
    Claude API call per qualifying contact, so this can take a while for a
    large batch. Check Railway logs for progress; new candidates show up at
    /contacts/review/ as they're created.

    The include_staged checkbox passes --include-staged through: contacts
    already sitting in the pending queue (e.g. re-staged by the daily sync
    right after a review-queue clear) get their existing candidate upgraded
    in place with the AI classification instead of being skipped."""
    if request.method == 'POST':
        args = []
        if request.POST.get('include_staged'):
            args.append('--include-staged')
        _run_command_in_background('analyze_recent_quo_contacts', *args)
        messages.success(
            request,
            'Started analyzing recent Quo contacts in the background — check Railway logs for progress. '
            'New candidates will appear below as they\'re created.',
        )
    return redirect('contact_review')
