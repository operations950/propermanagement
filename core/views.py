import json
import logging
from datetime import timedelta

from django.conf import settings as django_settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login as auth_login
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.utils.http import url_has_allowed_host_and_scheme

from messaging.services import _followup_result_message, _group_followups, _to_dash_format, _to_e164, fetch_quo_conversation, send_followup_bulk
from processes.models import ProcessTemplate
from tickets.models import Frequency, FollowUpLog, PropertyPackage, Ticket
from tickets.views import OPEN_STATUSES, _parse_quo_timestamp

from . import app_settings, google_calendar, google_login, places, quickbooks, usps
from .contact_document_import import DocumentImportError, extract_contacts_from_document
from .duplicates import find_duplicate_groups, merge_all_into
from .forms import (
    ContactForm,
    EmailOrUsernameAuthenticationForm,
    PropertyForm,
    StaffCreateForm,
)
from .models import (
    Contact, ContactDocument, ContactImportCandidate, ContactUpdateCandidate, DuplicateDismissal,
    GoogleCalendarToken, Property, PropertyAttribute, PropertyAttributeAssignment, PropertyDocument,
    PropertyListingName, PropertySystemLocation, QuickBooksToken, StaffProfile, TRADE_CHOICES, Unit,
    creatable_contact_types, group_contacts_by_type, is_valid_phone, properties_by_type,
)

logger = logging.getLogger(__name__)


def _is_admin(user):
    """True for a Django superuser OR a staff member flagged Company Admin
    from /admin-tools/ — the two used to be separate concepts (Company
    Admin only unlocked the owner dashboard), but that split meant toggling
    someone "Company Admin" silently left them locked out of every other
    admin-gated screen (checklist editing, property delete, staff
    creation, Admin Tools itself). Unified at the user's explicit request:
    Company Admin now means full admin, same as is_superuser."""
    if user.is_superuser:
        return True
    return getattr(getattr(user, 'staff_profile', None), 'is_company_admin', False)


def _safe_next(request, default='dashboard'):
    next_url = request.POST.get('next') or request.GET.get('next')
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return next_url
    return redirect(default).url


class StaffLoginView(auth_views.LoginView):
    """Plain auth_views.LoginView, but google_signon_configured is computed
    per-request rather than baked in at urls.py import time — the
    GOOGLE_OAUTH_CLIENT_ID/SECRET pair can change at runtime via
    /admin-tools/ (see core/app_settings.py), so a static extra_context
    would go stale until the next deploy/restart."""
    template_name = 'registration/login.html'
    authentication_form = EmailOrUsernameAuthenticationForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['google_signon_configured'] = google_login.is_configured()
        return context


def google_login_start(request):
    """Kicks off Sign in with Google from the login page — a fresh identity
    check each time (no offline access needed), unlike calendar_connect's
    flow which requests a refresh token to use later."""
    if request.user.is_authenticated:
        return redirect('dashboard')
    if not google_login.is_configured():
        messages.error(request, 'Google sign-on isn\'t configured yet — log in with your email and password instead.')
        return redirect('login')

    flow = google_login.build_flow(request)
    auth_url, state = flow.authorization_url(access_type='online', prompt='select_account')
    request.session['google_login_state'] = state
    return redirect(auth_url)


def google_login_callback(request):
    if request.user.is_authenticated:
        return redirect('dashboard')

    state = request.session.pop('google_login_state', None)
    if not state or request.GET.get('state') != state:
        messages.error(request, 'Google sign-on failed (session expired) — try again.')
        return redirect('login')
    if request.GET.get('error'):
        messages.info(request, 'Google sign-on cancelled.')
        return redirect('login')

    flow = google_login.build_flow(request)
    try:
        flow.fetch_token(authorization_response=request.build_absolute_uri())
    except Exception:
        logger.exception('Google sign-on: token exchange failed')
        messages.error(request, 'Google sign-on failed — please try again.')
        return redirect('login')

    email = google_login.email_from_credentials(flow.credentials)
    if not email:
        messages.error(request, 'Google didn\'t return a verified email address — try again or use your password.')
        return redirect('login')

    User = get_user_model()
    try:
        user = User.objects.get(email__iexact=email, is_active=True)
    except User.DoesNotExist:
        messages.error(request, f'No staff account found for {email} — ask an admin to add your email to your account.')
        return redirect('login')

    auth_login(request, user, backend='core.auth_backends.EmailOrUsernameModelBackend')
    return redirect('dashboard')


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


@login_required
def calendar_select(request):
    """Bubble-lock picker on the dashboard (see _dashboard_calendar.html) —
    which of the staff member's own Google calendars to pull events from.
    An empty selection isn't allowed (falls back to the primary calendar in
    get_upcoming_events), so there's nothing to validate here beyond just
    saving whatever bubbles are locked."""
    next_url = _safe_next(request)
    if request.method == 'POST' and hasattr(request.user, 'staff_profile'):
        token = GoogleCalendarToken.objects.filter(staff=request.user.staff_profile).first()
        if token:
            token.enabled_calendar_ids = request.POST.getlist('calendar_ids')
            token.save(update_fields=['enabled_calendar_ids'])
            messages.success(request, 'Calendars updated.')
    return redirect(next_url)


@login_required
@user_passes_test(_is_admin)
def quickbooks_connect(request):
    """Connecting the company's single QuickBooks company file is an admin
    action (see admin_tools.html) even though viewing the resulting
    Company Financials numbers only requires Company Admin dashboard
    access — those are two different, deliberately separate permissions."""
    if not quickbooks.is_configured():
        messages.error(request, 'QuickBooks isn\'t configured yet — add the OAuth client ID/secret first.')
        return redirect('admin_tools')

    import secrets
    state = secrets.token_urlsafe(24)
    request.session['quickbooks_oauth_state'] = state
    return redirect(quickbooks.authorize_url(request, state))


@login_required
@user_passes_test(_is_admin)
def quickbooks_callback(request):
    state = request.session.pop('quickbooks_oauth_state', None)
    if not state or request.GET.get('state') != state:
        messages.error(request, 'QuickBooks connection failed (session expired) — try again.')
        return redirect('admin_tools')
    if request.GET.get('error'):
        messages.info(request, 'QuickBooks connection cancelled.')
        return redirect('admin_tools')

    code = request.GET.get('code')
    realm_id = request.GET.get('realmId')
    if not code or not realm_id:
        messages.error(request, 'QuickBooks connection failed — missing authorization code.')
        return redirect('admin_tools')

    token_data = quickbooks.exchange_code(request, code)
    if not token_data:
        messages.error(request, 'QuickBooks connection failed — please try again.')
        return redirect('admin_tools')

    # Exactly one company connection exists at a time — a new connect
    # replaces whatever was there before (see QuickBooksToken's docstring).
    QuickBooksToken.objects.all().delete()
    QuickBooksToken.objects.create(
        realm_id=realm_id,
        access_token=token_data.get('access_token', ''),
        refresh_token=token_data.get('refresh_token', ''),
        access_token_expires_at=timezone.now() + timedelta(seconds=token_data.get('expires_in', 3600)),
        refresh_token_expires_at=(
            timezone.now() + timedelta(seconds=token_data['x_refresh_token_expires_in'])
            if token_data.get('x_refresh_token_expires_in') else None
        ),
        connected_by=request.user,
    )
    messages.success(request, 'QuickBooks connected.')
    return redirect('admin_tools')


@login_required
@user_passes_test(_is_admin)
def quickbooks_disconnect(request):
    if request.method == 'POST':
        QuickBooksToken.objects.all().delete()
        messages.success(request, 'QuickBooks disconnected.')
    return redirect('admin_tools')


def _parse_calendar_event_form(request):
    """Shared POST parsing for calendar_event_create/update — raises
    ValueError with a user-facing message on anything unusable, rather
    than letting a bad date/time silently produce a wrong Google event."""
    title = request.POST.get('title', '').strip()
    if not title:
        raise ValueError('Give the event a title.')
    calendar_id = request.POST.get('calendar_id', '').strip()
    if not calendar_id:
        raise ValueError('Choose which calendar to use.')
    event_date = parse_date(request.POST.get('date', ''))
    if not event_date:
        raise ValueError('Choose a date.')

    if request.POST.get('all_day') == 'on':
        return calendar_id, title, event_date, event_date + timedelta(days=1), True

    start_dt = parse_datetime(f"{event_date}T{request.POST.get('start_time', '')}")
    end_dt = parse_datetime(f"{event_date}T{request.POST.get('end_time', '')}")
    if not start_dt or not end_dt:
        raise ValueError('Enter a start and end time, or check All day.')
    tz = timezone.get_current_timezone()
    start_dt = timezone.make_aware(start_dt, tz)
    end_dt = timezone.make_aware(end_dt, tz)
    if end_dt <= start_dt:
        raise ValueError('End time must be after the start time.')
    return calendar_id, title, start_dt, end_dt, False


@login_required
def calendar_event_create(request):
    next_url = _safe_next(request)
    staff_profile = getattr(request.user, 'staff_profile', None)
    token = getattr(staff_profile, 'google_calendar_token', None) if staff_profile else None
    if request.method == 'POST' and token:
        try:
            calendar_id, title, start, end, all_day = _parse_calendar_event_form(request)
            google_calendar.create_event(token, calendar_id, title, start, end, all_day)
            messages.success(request, f'"{title}" added to your calendar.')
        except ValueError as e:
            messages.error(request, str(e))
        except google_calendar.GoogleCalendarWriteError as e:
            messages.error(request, str(e))
    return redirect(next_url)


@login_required
def calendar_event_update(request):
    next_url = _safe_next(request)
    staff_profile = getattr(request.user, 'staff_profile', None)
    token = getattr(staff_profile, 'google_calendar_token', None) if staff_profile else None
    if request.method == 'POST' and token:
        event_id = request.POST.get('event_id', '').strip()
        if not event_id:
            messages.error(request, 'No event selected to update.')
            return redirect(next_url)
        try:
            calendar_id, title, start, end, all_day = _parse_calendar_event_form(request)
            google_calendar.update_event(token, calendar_id, event_id, title, start, end, all_day)
            messages.success(request, f'"{title}" updated.')
        except ValueError as e:
            messages.error(request, str(e))
        except google_calendar.GoogleCalendarWriteError as e:
            messages.error(request, str(e))
    return redirect(next_url)


@login_required
def calendar_event_delete(request):
    next_url = _safe_next(request)
    staff_profile = getattr(request.user, 'staff_profile', None)
    token = getattr(staff_profile, 'google_calendar_token', None) if staff_profile else None
    if request.method == 'POST' and token:
        calendar_id = request.POST.get('calendar_id', '').strip()
        event_id = request.POST.get('event_id', '').strip()
        if calendar_id and event_id:
            try:
                google_calendar.delete_event(token, calendar_id, event_id)
                messages.success(request, 'Event deleted.')
            except google_calendar.GoogleCalendarWriteError as e:
                messages.error(request, str(e))
    return redirect(next_url)


@login_required
def timezone_select(request):
    """Bubble-lock picker (see _dashboard_calendar.html) for a staff
    member's own StaffProfile.timezone — core.middleware.TimezoneMiddleware
    activates it on every subsequent request, overriding settings.TIME_ZONE
    for all of that user's due dates/calendar events/message timestamps."""
    next_url = _safe_next(request)
    if request.method == 'POST' and hasattr(request.user, 'staff_profile'):
        tz = request.POST.get('timezone')
        if tz in StaffProfile.Timezone.values:
            request.user.staff_profile.timezone = tz
            request.user.staff_profile.save(update_fields=['timezone'])
            messages.success(request, 'Timezone updated.')
    return redirect(next_url)


@login_required
def property_list(request):
    qs = Property.objects.all()
    q = request.GET.get('q', '').strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(address__icontains=q))
    selected_type = request.GET.get('type', '')
    if selected_type:
        qs = qs.filter(property_type=selected_type)
    show_inactive = request.GET.get('show_inactive') == '1'
    if not show_inactive:
        qs = qs.filter(is_active=True)
    qs = qs.annotate(unit_count=Count('units', distinct=True)).order_by('property_type', '-is_general', 'name')

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({
            'desktop': render_to_string('core/_property_table_rows.html', {'properties': qs}, request=request),
            'mobile': render_to_string('core/_property_mobile_cards.html', {'properties': qs}, request=request),
        })

    return render(request, 'core/property_list.html', {
        'properties': qs,
        'type_choices': Property.Type.choices,
        'q': q,
        'selected_type': selected_type,
        'show_inactive': show_inactive,
    })


def _list_quo_phone_lines():
    """This Quo account's own phone lines, for the scan/outbound pickers on
    admin_tools — cached like _build_contact_lookup's contact list, since
    these barely ever change and admin_tools shouldn't cost a live Quo API
    call (plus its retry/backoff delay) on every load. Returns [] rather
    than raising if QUO_API_KEY isn't configured or the call fails, so the
    page still renders with an explanatory empty state."""
    from django.core.cache import cache

    if not django_settings.QUO_API_KEY:
        return []
    cached = cache.get('quo_phone_lines')
    if cached is not None:
        return cached

    from intake.adapters.quo import QuoAdapter, QuoAPIError
    import requests

    try:
        numbers = QuoAdapter()._list_phone_numbers()
    except (requests.RequestException, QuoAPIError):
        logger.exception('Quo: failed to list phone numbers for admin_tools')
        return []
    lines = [
        {'id': n['id'], 'number': n.get('number', ''), 'label': n.get('name') or n.get('number', '')}
        for n in numbers if n.get('id')
    ]
    cache.set('quo_phone_lines', lines, timeout=3600)
    return lines


@login_required
@user_passes_test(_is_admin)
def admin_tools(request):
    """Staff-facing admin toolbox — deliberately separate from the
    property edit screen (deactivating/reactivating a property is an
    administrative action, not a property-detail edit) and from Django's
    raw /admin/ (which stays available for anything not yet given its own
    control here, linked at the bottom of this page)."""
    properties = Property.objects.all().order_by('-is_active', 'property_type', 'name')
    # Email fields get their own section further down, shown in the clear —
    # unlike every other secret here, an admin needs to actually read these
    # back to debug a broken SMTP setup, not just confirm one is present.
    email_keys = {'EMAIL_HOST', 'EMAIL_HOST_USER', 'EMAIL_HOST_PASSWORD', 'DEFAULT_FROM_EMAIL'}
    secrets = [
        {
            'key': key, 'label': label,
            'is_set': bool(getattr(django_settings, key, '')),
            'masked': app_settings.masked(getattr(django_settings, key, '')),
        }
        for key, label in app_settings.SECRET_KEYS if key not in email_keys
    ]
    email_settings = [
        {'key': key, 'label': label, 'value': getattr(django_settings, key, '') or ''}
        for key, label in app_settings.SECRET_KEYS if key in email_keys
    ]
    google_redirect_uris = [
        request.build_absolute_uri(reverse(name))
        for name in ('google_login_callback', 'calendar_callback', 'gmail_callback')
    ]
    from intake.models import GmailInboxToken
    gmail_inbox_tokens = GmailInboxToken.objects.all().order_by('connected_at')
    return render(request, 'core/admin_tools.html', {
        'properties': properties, 'secrets': secrets, 'google_redirect_uris': google_redirect_uris,
        'quickbooks_configured': quickbooks.is_configured(),
        'quickbooks_token': QuickBooksToken.objects.first(),
        'quickbooks_redirect_uri': quickbooks.redirect_uri_for_display(request),
        'quo_phone_lines': _list_quo_phone_lines(),
        'scan_phone_number_id': django_settings.QUO_SCAN_PHONE_NUMBER_ID,
        'outbound_from_number': django_settings.QUO_DEFAULT_FROM_NUMBER,
        'staff_profiles': StaffProfile.objects.select_related('user').order_by('user__first_name', 'user__last_name'),
        'role_choices': StaffProfile.Role.choices,
        'email_settings': email_settings,
        'email_backend': django_settings.EMAIL_BACKEND,
        'email_is_console': django_settings.EMAIL_BACKEND.endswith('console.EmailBackend'),
        'email_port': django_settings.EMAIL_PORT,
        'email_use_tls': django_settings.EMAIL_USE_TLS,
        'email_use_ssl': getattr(django_settings, 'EMAIL_USE_SSL', False),
        'email_timeout': getattr(django_settings, 'EMAIL_TIMEOUT', None),
        'test_email_default_to': request.user.email,
        'gmail_inbox_tokens': gmail_inbox_tokens,
    })


@login_required
@user_passes_test(_is_admin)
def staff_create(request):
    """The only way to create a staff account short of Django admin.
    Checks the entered email against existing Contacts (a person often
    starts out as a vendor/guest/board-member Contact before joining the
    team) — if one matches, this holds off creating anything and asks for
    explicit confirmation before proceeding, rather than silently leaving
    both a Contact and a User for the same real person lying around."""
    matched_contact = None
    if request.method == 'POST':
        form = StaffCreateForm(request.POST)
        if form.is_valid():
            email = form.cleaned_data['email']
            matched_contact = Contact.objects.filter(email__iexact=email).first()
            confirmed = request.POST.get('confirm_merge') == '1'
            if not matched_contact or confirmed:
                User = get_user_model()
                user = User.objects.create_user(
                    username=email, email=email,
                    first_name=form.cleaned_data['first_name'], last_name=form.cleaned_data['last_name'],
                    password=form.cleaned_data['password'],
                )
                StaffProfile.objects.create(
                    user=user, role=form.cleaned_data['role'],
                    phone=form.cleaned_data['phone'] or (matched_contact.phone if matched_contact else ''),
                    timezone=form.cleaned_data['timezone'],
                )
                if matched_contact and confirmed:
                    # Converted, not deleted — every staff member needs a
                    # Staff-type Contact so they're still findable in any
                    # contact search across the app (see creatable_contact_types
                    # for why every OTHER contact-creation path is barred from
                    # ever producing this type themselves).
                    matched_contact.contact_type = Contact.ContactType.STAFF_ADJACENT
                    matched_contact.secondary_types = []
                    matched_contact.save(update_fields=['contact_type', 'secondary_types'])
                    messages.success(
                        request,
                        f'Staff account created for {user.get_full_name()} — merged contact '
                        f'"{matched_contact.name}" into it.',
                    )
                else:
                    Contact.objects.create(
                        name=user.get_full_name(),
                        contact_type=Contact.ContactType.STAFF_ADJACENT,
                        phone=form.cleaned_data['phone'],
                        email=email,
                    )
                    messages.success(request, f'Staff account created for {user.get_full_name()}.')
                return redirect('admin_tools')
            # matched_contact exists and not yet confirmed — fall through to
            # re-render the same form with a warning instead of saving.
    else:
        form = StaffCreateForm()
    return render(request, 'core/staff_form.html', {'form': form, 'matched_contact': matched_contact})


@login_required
@user_passes_test(_is_admin)
def admin_phone_settings_save(request):
    if request.method == 'POST':
        app_settings.set_secret('QUO_SCAN_PHONE_NUMBER_ID', request.POST.get('scan_phone_number_id', ''), user=request.user)
        app_settings.set_secret('QUO_DEFAULT_FROM_NUMBER', request.POST.get('outbound_from_number', ''), user=request.user)
        messages.success(request, 'Phone line settings updated.')
    return redirect('admin_tools')


@login_required
@user_passes_test(_is_admin)
def admin_settings_save(request):
    if request.method == 'POST':
        valid_keys = dict(app_settings.SECRET_KEYS)
        updated = 0
        for key in valid_keys:
            value = request.POST.get(key, '').strip()
            if value:  # blank means "leave unchanged" — the field never shows the real value to re-submit
                # set_secret sanitizes (strips whitespace, drops non-ASCII) before storing — see
                # app_settings._sanitize_secret for why that matters for every one of these values.
                app_settings.set_secret(key, value, user=request.user)
                updated += 1
        if updated:
            messages.success(request, f'Updated {updated} setting(s).')
        else:
            messages.info(request, 'Nothing changed — all fields were left blank.')
    return redirect('admin_tools')


@login_required
@user_passes_test(_is_admin)
def admin_test_email_send(request):
    """A real send_mail() call, bypassing FollowUpLog/ticket machinery
    entirely — isolates "is SMTP actually configured right" from "is the
    ticket follow-up code path calling it correctly." fail_silently=False
    surfaces the real exception (auth failure, timeout, refused connection,
    ...) as a flash message instead of it disappearing into a try/except
    somewhere, which is the only way to actually diagnose a broken setup
    without shell/log access."""
    if request.method == 'POST':
        to_address = request.POST.get('to_address', '').strip()
        if not to_address:
            messages.error(request, 'Enter an address to send the test email to.')
            return redirect('admin_tools')
        from django.core.mail import send_mail

        from intake.models import GmailInboxToken
        gmail_token = GmailInboxToken.objects.filter(is_send_from=True).first() or GmailInboxToken.objects.first()
        using_gmail = django_settings.EMAIL_BACKEND.endswith('GmailAPIBackend')
        if using_gmail and gmail_token:
            from_address = django_settings.DEFAULT_FROM_EMAIL or gmail_token.mailbox_email
        else:
            from_address = django_settings.DEFAULT_FROM_EMAIL or django_settings.EMAIL_HOST_USER or 'noreply@example.com'
        try:
            send_mail(
                'PropTasks test email',
                'This is a test email sent from Admin Tools to verify your email configuration is working.',
                from_address, [to_address], fail_silently=False,
            )
        except Exception as exc:
            messages.error(request, f'Test email failed: {type(exc).__name__}: {exc}')
        else:
            if django_settings.EMAIL_BACKEND.endswith('console.EmailBackend'):
                messages.warning(
                    request,
                    'Sent, but no send path is configured — this only went to the server console/log, not a '
                    'real inbox. Connect Gmail or fill in Email Configuration below and save first.',
                )
            elif using_gmail:
                messages.success(
                    request, f'Test email sent to {to_address} via Gmail ({gmail_token.mailbox_email if gmail_token else "?"}) '
                    f'from {from_address} — check the inbox (and spam folder).',
                )
            else:
                messages.success(
                    request, f'Test email sent to {to_address} via {django_settings.EMAIL_HOST}:{django_settings.EMAIL_PORT} '
                    f'from {from_address} — check the inbox (and spam folder).',
                )
    return redirect('admin_tools')


@login_required
@user_passes_test(_is_admin)
def property_toggle_active(request, pk):
    """Soft-delete/restore only — a property with tickets, contacts, and
    follow-up history spanning years should never be hard-deleted from the
    UI. Deactivating just hides it from the active pickers/lists
    (property_list's default filter, New Ticket's property picker, etc.)
    while preserving every linked record permanently."""
    prop = get_object_or_404(Property, pk=pk)
    if request.method == 'POST':
        prop.is_active = not prop.is_active
        prop.save(update_fields=['is_active'])
        messages.success(
            request, f'"{prop.name}" is now {"active" if prop.is_active else "inactive"}.',
        )
    return redirect('admin_tools')


@login_required
@user_passes_test(_is_admin)
def staff_toggle_company_admin(request, pk):
    """The ongoing escape hatch for granting/revoking Owner Dashboard
    access (see StaffProfile.is_company_admin) beyond the two people the
    grant_company_admin management command seeds — this checkbox is the
    only other way to change it."""
    profile = get_object_or_404(StaffProfile, pk=pk)
    if request.method == 'POST':
        profile.is_company_admin = bool(request.POST.get('is_company_admin'))
        profile.save(update_fields=['is_company_admin'])
    return redirect('admin_tools')


@login_required
@user_passes_test(_is_admin)
def staff_set_role(request, pk):
    """Inline Department edit on the Admin Tools Staff table — auto-submits
    on change, same one-click pattern as the Company Admin checkbox right
    next to it. Previously the only way to change an existing staff
    member's department was Django admin (staff_create only sets it once,
    at account creation)."""
    profile = get_object_or_404(StaffProfile, pk=pk)
    if request.method == 'POST':
        new_role = request.POST.get('role', '')
        if new_role == '' or new_role in StaffProfile.Role.values:
            profile.role = new_role
            profile.save(update_fields=['role'])
            messages.success(request, f'{profile} is now {profile.get_role_display() or "in no department"}.')
    return redirect('admin_tools')


def _standardize_property_address(request, prop):
    """Runs USPS standardization on a just-validated (not yet saved)
    Property instance — overwrites street/city/state/zip_code with USPS's
    standardized values and sets address_verified on a confirmed match;
    otherwise leaves the submitted values as-is with address_verified
    False and a warning message. Never blocks the save either way. No-op
    (silently) for general placeholders, which have no real address."""
    if prop.is_general:
        return
    result = usps.standardize(prop.street, prop.city, prop.state, prop.zip_code)
    if result['verified']:
        prop.street = result['street']
        prop.city = result['city']
        prop.state = result['state']
        prop.zip_code = result['zip_code']
        prop.address_verified = True
    else:
        prop.address_verified = False
        messages.warning(request, f"Saved, but USPS couldn't verify this address — showing it as entered. ({result['error']})")


@login_required
def property_create(request):
    if request.method == 'POST':
        form = PropertyForm(request.POST)
        if form.is_valid():
            prop = form.save(commit=False)
            _standardize_property_address(request, prop)
            prop.save()
            messages.success(request, f'Property "{prop.name}" created.')
            return redirect('property_detail', pk=prop.pk)
    else:
        form = PropertyForm(initial={'property_type': Property.Type.SHORT_TERM_RENTAL})
    return render(request, 'core/property_form.html', {
        'form': form, 'is_new': True, 'places_configured': places.is_configured(),
    })


@login_required
def property_edit(request, pk):
    prop = get_object_or_404(Property, pk=pk)
    if request.method == 'POST':
        form = PropertyForm(request.POST, instance=prop)
        if form.is_valid():
            prop = form.save(commit=False)
            _standardize_property_address(request, prop)
            prop.save()
            messages.success(request, f'Property "{prop.name}" updated.')
            return redirect('property_detail', pk=prop.pk)
    else:
        form = PropertyForm(instance=prop)
    return render(request, 'core/property_form.html', {
        'form': form, 'is_new': False, 'property': prop, 'property_contacts': prop.contacts.all(),
        'places_configured': places.is_configured(),
    })


@login_required
def property_address_autocomplete(request):
    return JsonResponse({'suggestions': places.autocomplete(request.GET.get('q', ''))})


@login_required
def property_address_lookup(request, place_id):
    return JsonResponse(places.place_details(place_id) or {})


@login_required
def property_detail(request, pk):
    """Everything-about-this-property dashboard: facts, access/system info,
    amenities, contacts (with an on-demand live Quo thread per contact —
    see property_contact_thread for why that's on-demand rather than
    preloaded), a bubble-lock Communication card reusing the exact
    send_followup_bulk/_group_followups machinery the ticket detail screen's
    Follow-Up card uses, and this property's open tickets/tasks."""
    prop = get_object_or_404(Property, pk=pk)

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'save_access_info':
            fields = [
                'gate_code', 'door_code', 'lockbox_code', 'alarm_code', 'wifi_network', 'wifi_password',
                'access_notes', 'board_meeting_address',
            ]
            for field in fields:
                setattr(prop, field, request.POST.get(field, '').strip())
            for time_field in ('default_check_in_time', 'default_check_out_time'):
                setattr(prop, time_field, request.POST.get(time_field) or None)
            prop.save(update_fields=fields + ['default_check_in_time', 'default_check_out_time'])
            messages.success(request, 'Access info saved.')
        elif action == 'add_listing_name':
            platform = request.POST.get('platform')
            name = request.POST.get('listing_name', '').strip()
            label = dict(PropertyListingName.Platform.choices).get(platform)
            unit_id = request.POST.get('unit_id') or None
            unit = prop.units.filter(pk=unit_id).first() if unit_id else None
            if not (label and name):
                messages.error(request, 'Enter a listing name.')
            elif unit_id and not unit:
                messages.error(request, 'That unit doesn\'t belong to this property.')
            else:
                other = PropertyListingName.objects.filter(platform=platform, name=name).exclude(property=prop).first()
                if other:
                    messages.error(
                        request,
                        f'"{name}" is already {label}\'s listing name for {other.property.name} — remove it '
                        'there first, or use a different name here.',
                    )
                else:
                    listing, created = PropertyListingName.objects.get_or_create(
                        property=prop, platform=platform, name=name, defaults={'unit': unit},
                    )
                    if not created and listing.unit_id != (unit.pk if unit else None):
                        # Re-submitting an existing name with a different unit
                        # picked — e.g. fixing one that was added before this
                        # property had units, or correcting a wrong pick.
                        # Same "no separate edit control" UI as everywhere
                        # else on this card (remove + re-add), just made to
                        # actually update instead of silently no-op.
                        listing.unit = unit
                        listing.save(update_fields=['unit'])
                    if created:
                        messages.success(request, f'Added "{name}" as a {label} listing name.' + (f' → {unit.label}' if unit else ''))
                    else:
                        messages.success(request, f'Updated "{name}"\'s unit.' if unit else f'"{name}" is now unassigned to a specific unit.')
        elif action == 'remove_listing_name':
            PropertyListingName.objects.filter(pk=request.POST.get('listing_name_id'), property=prop).delete()
            messages.success(request, 'Removed.')
        elif action == 'add_document':
            name = request.POST.get('name', '').strip()
            file = request.FILES.get('file')
            if name and file:
                PropertyDocument.objects.create(
                    property=prop, name=name, category=request.POST.get('category', '').strip(),
                    file=file, uploaded_by=request.user,
                )
                messages.success(request, 'Document added.')
            else:
                messages.error(request, 'A name and a file are both required.')
        elif action == 'delete_document':
            PropertyDocument.objects.filter(pk=request.POST.get('document_id'), property=prop).delete()
            messages.success(request, 'Removed.')
        elif action == 'add_system_location':
            system_name = request.POST.get('system_name', '').strip()
            location = request.POST.get('location', '').strip()
            if system_name and location:
                PropertySystemLocation.objects.create(
                    property=prop, system_name=system_name, location=location,
                    notes=request.POST.get('notes', '').strip(),
                )
                messages.success(request, 'Added.')
            else:
                messages.error(request, 'System name and location are both required.')
        elif action == 'delete_system_location':
            PropertySystemLocation.objects.filter(pk=request.POST.get('system_location_id'), property=prop).delete()
            messages.success(request, 'Removed.')
        elif action == 'add_unit':
            label = request.POST.get('label', '').strip()
            if not label:
                messages.error(request, 'Enter a label for the unit.')
            elif Unit.objects.filter(property=prop, label__iexact=label).exists():
                messages.error(request, f'"{label}" is already a unit on this property.')
            else:
                Unit.objects.create(
                    property=prop, label=label, notes=request.POST.get('notes', '').strip(),
                    access_code=request.POST.get('access_code', '').strip(),
                )
                messages.success(request, f'Added unit "{label}".')
        elif action == 'update_unit':
            unit = get_object_or_404(Unit, pk=request.POST.get('unit_id'), property=prop)
            label = request.POST.get('label', '').strip()
            if label:
                unit.label = label
            unit.access_code = request.POST.get('access_code', '').strip()
            unit.notes = request.POST.get('notes', '').strip()
            unit.is_active = request.POST.get('is_active') == 'on'
            unit.save()
            messages.success(request, 'Unit updated.')
        elif action == 'delete_unit':
            Unit.objects.filter(pk=request.POST.get('unit_id'), property=prop).delete()
            messages.success(request, 'Unit removed.')
        elif action == 'toggle_attribute':
            attribute_id = request.POST.get('attribute_id')
            existing = PropertyAttributeAssignment.objects.filter(property=prop, attribute_id=attribute_id)
            if existing.exists():
                existing.delete()
                messages.success(request, 'Attribute removed.')
            else:
                PropertyAttributeAssignment.objects.create(property=prop, attribute_id=attribute_id)
                messages.success(request, 'Attribute added.')
        return redirect('property_detail', pk=prop.pk)

    contacts = list(prop.contacts.all())
    contacts_by_type = {}
    for c in contacts:
        contacts_by_type.setdefault(c.contact_type, []).append(c)
    contact_type_labels = dict(Contact.ContactType.choices)
    contact_groups = [
        {'type_value': value, 'type_label': contact_type_labels[value], 'contacts': contacts_by_type[value]}
        for value in contacts_by_type
    ]
    contact_groups.sort(key=lambda g: g['type_label'])

    # Cheap bulk existence check — zero live API calls — vs. the expensive
    # per-contact live fetch (property_contact_thread), which only ever
    # runs on-demand for one contact at a time. See the plan's reasoning:
    # fetch_quo_conversation is a real synchronous HTTP round-trip with no
    # batching, so pre-fetching it for every contact on this property (could
    # be a dozen+) on every page load would be a real latency/reliability
    # problem.
    from intake.models import QuoThreadState
    contact_participants = {c.pk: _to_e164(c.phone) for c in contacts if c.phone}
    threaded_participants = set(
        QuoThreadState.objects.filter(participant__in=[p for p in contact_participants.values() if p])
        .values_list('participant', flat=True)
    )
    contacts_with_thread_ids = {
        pk for pk, participant in contact_participants.items() if participant and participant in threaded_participants
    }

    open_tickets = (
        Ticket.objects.filter(property=prop, status__in=OPEN_STATUSES)
        .select_related('assigned_staff__user', 'assigned_contact')
        .order_by('due_date')
    )

    assigned_attribute_ids = set(prop.attribute_assignments.values_list('attribute_id', flat=True))

    return render(request, 'core/property_detail.html', {
        'property': prop,
        'contact_groups': contact_groups,
        'contacts_with_thread_ids': contacts_with_thread_ids,
        'text_contacts': [c for c in contacts if c.phone],
        'email_contacts': [c for c in contacts if c.email],
        'open_tickets': open_tickets,
        'system_locations': prop.system_locations.all(),
        'units': prop.units.all(),
        'airbnb_listing_names': prop.listing_names.filter(platform=PropertyListingName.Platform.AIRBNB).select_related('unit'),
        'vrbo_listing_names': prop.listing_names.filter(platform=PropertyListingName.Platform.VRBO).select_related('unit'),
        'documents': prop.documents.all(),
        'text_contact_groups': group_contacts_by_type([c for c in contacts if c.phone]),
        'email_contact_groups': group_contacts_by_type([c for c in contacts if c.email]),
        'attributes': PropertyAttribute.objects.filter(is_active=True),
        'assigned_attribute_ids': assigned_attribute_ids,
        'followup_batches': _group_followups(prop.followups.select_related('contact')[:30]),
        'process_runs': prop.process_runs.select_related('process_template').prefetch_related('steps__attachments'),
        'attachable_process_templates': ProcessTemplate.objects.filter(is_active=True),
        'now': timezone.now(),
    })


@login_required
def property_followup_sms(request, pk):
    prop = get_object_or_404(Property, pk=pk)
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    if request.method == 'POST':
        contact_ids = request.POST.getlist('contact_ids')
        body = request.POST.get('body', '').strip()
        if contact_ids and body:
            logs = send_followup_bulk(FollowUpLog.Channel.SMS, contact_ids, body, property=prop, user=request.user)
            if is_ajax:
                ok = any(log.success for log in logs)
                return JsonResponse({
                    'success': ok,
                    'error': '' if ok else "Send failed — check the recipient's phone number.",
                })
            _followup_result_message(request, logs, 'recipient(s) by text')
        elif is_ajax:
            return JsonResponse({'success': False, 'error': 'Write a message first.'})
        else:
            messages.error(request, 'Choose at least one recipient and write a message first.')
    return redirect('property_detail', pk=prop.pk)


@login_required
def property_followup_email(request, pk):
    prop = get_object_or_404(Property, pk=pk)
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    if request.method == 'POST':
        contact_ids = request.POST.getlist('contact_ids')
        subject = request.POST.get('subject', '').strip()
        body = request.POST.get('body', '').strip()
        group = request.POST.get('group') == '1'
        if contact_ids and body:
            logs = send_followup_bulk(
                FollowUpLog.Channel.EMAIL, contact_ids, body, property=prop, subject=subject,
                group=group, user=request.user,
            )
            if is_ajax:
                ok = any(log.success for log in logs)
                return JsonResponse({'success': ok, 'error': '' if ok else 'Send failed.'})
            _followup_result_message(request, logs, 'recipient(s) by email')
        elif is_ajax:
            return JsonResponse({'success': False, 'error': 'Write a message first.'})
        else:
            messages.error(request, 'Choose at least one recipient and write a message first.')
    return redirect('property_detail', pk=prop.pk)


@login_required
def property_contact_thread(request, pk, contact_pk):
    """On-demand only — fetched by the property dashboard's "view
    conversation" expand, once per click, never preloaded for every
    contact on the property (see property_detail's docstring)."""
    contact = get_object_or_404(Contact, pk=contact_pk, properties__pk=pk)
    quo_messages = fetch_quo_conversation(contact)
    entries = []
    for m in (quo_messages or []):
        at = _parse_quo_timestamp(m.get('at', ''))
        if at:
            entries.append({'direction': m['direction'], 'body': m['body'], 'at': at})
    entries.sort(key=lambda e: e['at'])
    return render(request, 'core/_property_contact_thread.html', {
        'entries': entries, 'has_quo_thread': quo_messages is not None, 'contact': contact,
    })


@login_required
def contact_list(request):
    qs = Contact.objects.prefetch_related('properties')
    q = request.GET.get('q', '').strip()
    if q:
        qs = qs.filter(
            Q(name__icontains=q) | Q(phone__icontains=q) | Q(email__icontains=q) | Q(trade__icontains=q)
            | Q(contact_type__icontains=q)
        )
    selected_type = request.GET.get('type', '')
    if selected_type:
        qs = qs.filter(contact_type=selected_type)
    qs = qs.order_by('name')

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({
            'desktop': render_to_string('core/_contact_table_rows.html', {'contacts': qs}, request=request),
            'mobile': render_to_string('core/_contact_mobile_cards.html', {'contacts': qs}, request=request),
        })

    return render(request, 'core/contact_list.html', {
        'contacts': qs,
        'type_choices': Contact.ContactType.choices,
        'creatable_type_choices': creatable_contact_types(),
        'q': q,
        'selected_type': selected_type,
        'pending_review_count': (
            ContactImportCandidate.objects.filter(status=ContactImportCandidate.Status.PENDING).count()
            + ContactUpdateCandidate.objects.filter(status=ContactUpdateCandidate.Status.PENDING).count()
        ),
        'duplicate_group_count': len(find_duplicate_groups()),
        'active_properties': Property.objects.filter(is_active=True).order_by('name'),
    })


@login_required
def contact_import_parse(request):
    """Drag-and-drop contact import, step 1: hands the uploaded document to
    Claude and returns a plain JSON list of extracted contacts — nothing is
    saved yet. The staff member reviews/edits/excludes rows in the browser;
    contact_import_commit is what actually creates Contacts, once they
    click Accept all."""
    if request.method != 'POST':
        return redirect('contact_list')
    upload = request.FILES.get('file')
    if not upload:
        return JsonResponse({'success': False, 'error': 'No file received.'}, status=400)
    try:
        contacts = extract_contacts_from_document(upload)
    except DocumentImportError as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)
    return JsonResponse({'success': True, 'contacts': contacts})


@login_required
def contact_import_commit(request):
    """Step 2: creates real Contact rows from whatever the staff member
    left in the preview (after their own edits/exclusions) — no separate
    review queue, since they've already reviewed it right here. Still
    skips anything that already matches an existing Contact by phone or
    email, same dedup every other import path in this app does."""
    if request.method != 'POST':
        return redirect('contact_list')
    try:
        payload = json.loads(request.body)
    except (ValueError, TypeError):
        return JsonResponse({'success': False, 'error': 'Malformed request.'}, status=400)

    rows = payload.get('contacts') or []
    existing_phones = {_to_e164(p) for p in Contact.objects.exclude(phone='').values_list('phone', flat=True)}
    existing_phones.discard('')
    existing_emails = {e.lower() for e in Contact.objects.exclude(email='').values_list('email', flat=True)}

    created = skipped_duplicate = skipped_no_name = 0
    for row in rows:
        name = (row.get('name') or '').strip()
        if not name:
            skipped_no_name += 1
            continue
        phone_raw = (row.get('phone') or '').strip()
        phone = _to_dash_format(phone_raw) if phone_raw else ''
        email = (row.get('email') or '').strip()
        phone_key = _to_e164(phone) if phone else ''
        if (phone_key and phone_key in existing_phones) or (email and email.lower() in existing_emails):
            skipped_duplicate += 1
            continue

        contact_type = row.get('contact_type') or Contact.ContactType.OTHER
        if contact_type not in Contact.ContactType.values:
            contact_type = Contact.ContactType.OTHER

        contact = Contact.objects.create(
            name=name, phone=phone, email=email, contact_type=contact_type,
            trade=(row.get('trade') or '').strip() if contact_type == Contact.ContactType.VENDOR else '',
            source=Contact.Source.DOCUMENT,
        )
        property_id = row.get('property_id')
        if property_id:
            contact.properties.set(Property.objects.filter(pk=property_id))
        if phone_key:
            existing_phones.add(phone_key)
        if email:
            existing_emails.add(email.lower())
        created += 1

    parts = [f'Added {created} contact{"s" if created != 1 else ""}.']
    if skipped_duplicate:
        parts.append(f'Skipped {skipped_duplicate} already in Proper Management.')
    if skipped_no_name:
        parts.append(f'Skipped {skipped_no_name} with no name.')
    return JsonResponse({'success': True, 'created': created, 'message': ' '.join(parts)})


def _contact_form_context(form, **extra):
    selected_ids = [str(v.pk if hasattr(v, 'pk') else v) for v in (form['properties'].value() or [])]
    selected_unit_ids = [str(v.pk if hasattr(v, 'pk') else v) for v in (form['units'].value() or [])]
    trade_value = form['trade'].value() or ''
    selected_secondary_types = form['secondary_types'].value() or []
    return {
        'form': form, 'properties_by_type': properties_by_type(),
        'selected_property_ids': ','.join(selected_ids),
        'all_units': Unit.objects.filter(is_active=True).select_related('property').order_by('property__name', 'label'),
        'selected_unit_ids': [int(v) for v in selected_unit_ids],
        'trade_choices': TRADE_CHOICES,
        'trade_is_other': bool(trade_value) and trade_value not in TRADE_CHOICES,
        'selected_secondary_types': ','.join(selected_secondary_types),
        **extra,
    }


@login_required
def contact_create(request):
    initial = {}
    property_id = request.GET.get('property')
    if property_id:
        initial['properties'] = [property_id]
    if request.method == 'POST':
        form = ContactForm(request.POST)
        if form.is_valid():
            contact = form.save()
            messages.success(request, f'Contact "{contact.name}" created.')
            if property_id:
                return redirect('property_edit', pk=property_id)
            return redirect('contact_list')
    else:
        form = ContactForm(initial=initial)
    return render(request, 'core/contact_form.html', _contact_form_context(form, is_new=True))


@login_required
def contact_edit(request, pk):
    contact = get_object_or_404(Contact, pk=pk)
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'add_document':
            name = request.POST.get('name', '').strip()
            file = request.FILES.get('file')
            if name and file:
                ContactDocument.objects.create(contact=contact, name=name, file=file, uploaded_by=request.user)
                messages.success(request, 'Document added.')
            else:
                messages.error(request, 'A name and a file are both required.')
            return redirect('contact_edit', pk=contact.pk)
        elif action == 'delete_document':
            ContactDocument.objects.filter(pk=request.POST.get('document_id'), contact=contact).delete()
            messages.success(request, 'Removed.')
            return redirect('contact_edit', pk=contact.pk)

        form = ContactForm(request.POST, instance=contact)
        if form.is_valid():
            form.save()
            messages.success(request, f'Contact "{contact.name}" updated.')
            return redirect('contact_list')
    else:
        form = ContactForm(instance=contact)
    return render(request, 'core/contact_form.html', _contact_form_context(
        form, is_new=False, contact=contact, documents=contact.documents.all(),
        process_runs=contact.process_runs.select_related('process_template').prefetch_related('steps__attachments'),
        attachable_process_templates=ProcessTemplate.objects.filter(is_active=True),
    ))


@login_required
def contact_delete(request, pk):
    """Deleting a Contact is safe by design — every relationship that
    matters (Ticket.assigned_contact, FollowUpLog.from_contact/to_contact,
    TicketAttachment.uploaded_by_contact) is on_delete=SET_NULL, so real
    tickets/attachments/follow-ups stay intact and simply lose the
    assignment; only pure link/audit rows (TicketContact, duplicate-
    dismissal pairs, pending update candidates) cascade away with them.

    One wrinkle since Ticket's assignment CheckConstraint requires exactly
    one of assigned_staff/assigned_contact: Django's SET_NULL cascade is a
    raw bulk UPDATE, not a per-instance save(), so it bypasses
    Ticket.save()'s own "never leave neither set" fallback. For any ticket
    where this contact is the SOLE assignee, resolve that explicitly
    through save() first — its own fallback logic (department default,
    then any company-admin) kicks in — so the cascade that follows has
    nothing left to null out."""
    contact = get_object_or_404(Contact, pk=pk)
    if request.method == 'POST':
        name = contact.name
        for ticket in Ticket.objects.filter(assigned_contact=contact, assigned_staff__isnull=True):
            ticket.assigned_contact = None
            ticket.save(update_fields=['assigned_contact', 'assigned_staff', 'assignment_source'])
        contact.delete()
        messages.success(request, f'Deleted contact "{name}".')
        return redirect('contact_list')
    return redirect('contact_edit', pk=pk)


@login_required
def contact_review(request):
    candidates = ContactImportCandidate.objects.filter(status=ContactImportCandidate.Status.PENDING)
    update_candidates = ContactUpdateCandidate.objects.filter(
        status=ContactUpdateCandidate.Status.PENDING,
    ).select_related('contact')
    return render(request, 'core/contact_review.html', {
        'candidates': candidates,
        'update_candidates': update_candidates,
        'type_choices': creatable_contact_types(),
        'trade_choices': TRADE_CHOICES,
        'properties_by_type': properties_by_type(),
        'is_admin': _is_admin(request.user),
    })


@login_required
@user_passes_test(_is_admin)
def contact_candidates_clear_all(request):
    """Admin-only, one-time: hard-deletes every still-PENDING
    ContactImportCandidate outright, regardless of source (Quo/Gmail/Yardi)
    — not a reject, which would just leave 725+ resolved-but-still-there
    rows around. Used to reset a review queue that's grown too large/stale
    to work through row by row before repopulating it with a better-
    targeted pass (see analyze_recent_quo_contacts). Never touches already-
    approved/rejected candidates or real Contacts."""
    if request.method == 'POST':
        qs = ContactImportCandidate.objects.filter(status=ContactImportCandidate.Status.PENDING)
        count = qs.count()
        qs.delete()
        messages.success(request, f'Deleted {count} pending contact candidate(s).')
    return redirect('contact_review')


def _candidate_dupe(candidate):
    """A Contact already matching this candidate's phone or email, if any —
    checked again at approval time (not just at import time) in case
    something else created a matching Contact in the meantime."""
    lookup = Q()
    if candidate.phone:
        lookup |= Q(phone=candidate.phone)
    if candidate.email:
        lookup |= Q(email=candidate.email)
    if not lookup:
        return None
    return Contact.objects.filter(lookup).first()


def _approve_candidate(candidate, user, name, phone, email, contact_type, trade, property_id):
    """Shared by the single-candidate approve view (kept for direct/API use)
    and the review screen's bulk-save endpoint. Returns (ok, error, linked_existing)
    — on failure the candidate is left untouched (still PENDING) so it's
    simply skipped this round rather than silently mis-saved."""
    name = name.strip() or candidate.name
    phone = phone.strip()
    email = email.strip()
    contact_type = contact_type or candidate.suggested_contact_type
    trade = trade.strip()

    if not is_valid_phone(phone):
        return False, 'Phone must be in XXX-XXX-XXXX format.', False
    if contact_type == Contact.ContactType.VENDOR and not trade:
        return False, 'Choose a trade for vendor/contractor contacts.', False

    candidate.name, candidate.phone, candidate.email = name, phone, email
    existing = _candidate_dupe(candidate)
    if existing:
        contact = existing
    else:
        contact = Contact.objects.create(
            name=name, phone=phone, email=email, contact_type=contact_type, trade=trade,
            source=candidate.source,
        )
        if property_id:
            contact.properties.add(property_id)
    candidate.status = ContactImportCandidate.Status.APPROVED
    candidate.resolved_at = timezone.now()
    candidate.resolved_by = user
    candidate.resolved_contact = contact
    candidate.save()
    return True, None, bool(existing)


def _reject_candidate(candidate, user):
    candidate.status = ContactImportCandidate.Status.REJECTED
    candidate.resolved_at = timezone.now()
    candidate.resolved_by = user
    candidate.save()


def _apply_update(update, user):
    contact = update.contact
    if update.proposed_name:
        contact.name = update.proposed_name
    if update.proposed_phone:
        contact.phone = update.proposed_phone
    if update.proposed_email:
        contact.email = update.proposed_email
    contact.save(update_fields=['name', 'phone', 'email'])
    update.status = ContactUpdateCandidate.Status.APPLIED
    update.resolved_at = timezone.now()
    update.resolved_by = user
    update.save()


def _dismiss_update(update, user):
    update.status = ContactUpdateCandidate.Status.DISMISSED
    update.resolved_at = timezone.now()
    update.resolved_by = user
    update.save()


@login_required
def contact_review_approve(request, pk):
    candidate = get_object_or_404(ContactImportCandidate, pk=pk, status=ContactImportCandidate.Status.PENDING)
    if request.method == 'POST':
        ok, error, linked_existing = _approve_candidate(
            candidate, request.user,
            request.POST.get('name', ''), request.POST.get('phone', ''), request.POST.get('email', ''),
            request.POST.get('contact_type', ''), request.POST.get('trade', ''),
            request.POST.get('property_id') or None,
        )
        if ok:
            messages.success(
                request,
                f'Approved — {"linked to existing" if linked_existing else "created"} '
                f'contact "{candidate.resolved_contact.name}".',
            )
        else:
            messages.error(request, f'{error} — nothing was approved.')
    return redirect('contact_review')


@login_required
def contact_review_reject(request, pk):
    candidate = get_object_or_404(ContactImportCandidate, pk=pk, status=ContactImportCandidate.Status.PENDING)
    if request.method == 'POST':
        _reject_candidate(candidate, request.user)
        messages.success(request, f'Rejected "{candidate.name}".')
    return redirect('contact_review')


@login_required
def contact_update_apply(request, pk):
    update = get_object_or_404(
        ContactUpdateCandidate, pk=pk, status=ContactUpdateCandidate.Status.PENDING,
    )
    if request.method == 'POST':
        contact_name = update.contact.name
        _apply_update(update, request.user)
        messages.success(request, f'Updated "{contact_name}" from Quo.')
    return redirect('contact_review')


@login_required
def contact_review_bulk_save(request):
    """The review screen's floating "Save Changes" button — every
    approve/reject/apply/dismiss decision is marked client-side only (no
    request per click, see contact_review.html), so this processes every
    marked decision in one request instead of the page reloading after
    each single one. Reads a JSON body (not request.POST) since entries mix
    two different candidate tables with a decision and, for approvals, the
    row's live-edited field values. A candidate resolved by someone else
    since the page loaded (already not PENDING) is skipped quietly rather
    than erroring the whole batch."""
    if request.method != 'POST':
        return redirect('contact_review')

    try:
        entries = json.loads(request.body or '[]')
    except (ValueError, TypeError):
        return JsonResponse({'success': False, 'error': 'Malformed request.'}, status=400)

    approved = rejected = applied = dismissed = 0
    errors = []
    for entry in entries:
        pk = entry.get('pk')
        decision = entry.get('decision')
        kind = entry.get('type')
        try:
            if kind == 'candidate':
                candidate = ContactImportCandidate.objects.get(
                    pk=pk, status=ContactImportCandidate.Status.PENDING,
                )
                if decision == 'approve':
                    ok, error, _linked_existing = _approve_candidate(
                        candidate, request.user,
                        entry.get('name', ''), entry.get('phone', ''), entry.get('email', ''),
                        entry.get('contact_type', ''), entry.get('trade', ''),
                        entry.get('property_id') or None,
                    )
                    if ok:
                        approved += 1
                    else:
                        errors.append(f'{candidate.name or candidate.phone or "unnamed"}: {error}')
                elif decision == 'reject':
                    _reject_candidate(candidate, request.user)
                    rejected += 1
            elif kind == 'update':
                update = ContactUpdateCandidate.objects.get(
                    pk=pk, status=ContactUpdateCandidate.Status.PENDING,
                )
                if decision == 'apply':
                    _apply_update(update, request.user)
                    applied += 1
                elif decision == 'dismiss':
                    _dismiss_update(update, request.user)
                    dismissed += 1
        except (ContactImportCandidate.DoesNotExist, ContactUpdateCandidate.DoesNotExist):
            continue

    return JsonResponse({
        'success': True, 'approved': approved, 'rejected': rejected,
        'applied': applied, 'dismissed': dismissed, 'errors': errors,
    })


@login_required
def contact_update_dismiss(request, pk):
    update = get_object_or_404(
        ContactUpdateCandidate, pk=pk, status=ContactUpdateCandidate.Status.PENDING,
    )
    if request.method == 'POST':
        _dismiss_update(update, request.user)
        messages.success(request, f'Dismissed the proposed update for "{update.contact.name}".')
    return redirect('contact_review')


@login_required
def contact_duplicates(request):
    return render(request, 'core/contact_duplicates.html', {'groups': find_duplicate_groups()})


@login_required
def contact_duplicates_merge(request):
    if request.method == 'POST':
        primary_id = request.POST.get('primary_id')
        contact_ids = request.POST.getlist('contact_ids')
        if primary_id and len(contact_ids) > 1:
            primary = merge_all_into(primary_id, contact_ids)
            messages.success(request, f'Merged {len(contact_ids) - 1} duplicate(s) into "{primary.name}".')
    return redirect('contact_duplicates')


@login_required
def contact_duplicates_dismiss(request):
    if request.method == 'POST':
        import itertools

        contacts = list(Contact.objects.filter(pk__in=request.POST.getlist('contact_ids')))
        for c1, c2 in itertools.combinations(contacts, 2):
            DuplicateDismissal.record(c1, c2, user=request.user)
        messages.success(request, 'Marked as not duplicates.')
    return redirect('contact_duplicates')

