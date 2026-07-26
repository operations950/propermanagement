import json
import logging
from datetime import timedelta

from django.conf import settings as django_settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login as auth_login
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.utils.http import url_has_allowed_host_and_scheme

from messaging.services import _followup_result_message, _group_followups, _to_dash_format, _to_e164, fetch_quo_conversation, send_followup_bulk
from tickets.models import (
    Frequency, FollowUpLog, PropertyPackage, PropertyTemplateOverride, TaskPackage, TaskPackageTemplate,
    Ticket, TicketTemplate,
)
from tickets.services import applicability
from tickets.views import OPEN_STATUSES, _parse_quo_timestamp

from . import app_settings, google_calendar, google_login, places, usps
from .contact_document_import import DocumentImportError, extract_contacts_from_document
from .duplicates import find_duplicate_groups, merge_all_into
from .forms import ContactForm, EmailOrUsernameAuthenticationForm, PropertyForm, PropertyTemplateOverrideForm
from .models import (
    Contact, ContactImportCandidate, ContactUpdateCandidate, DuplicateDismissal, GoogleCalendarToken, Property,
    PropertyAttribute, PropertyAttributeAssignment, PropertySystemLocation, StaffProfile, TRADE_CHOICES,
    is_valid_phone, properties_by_type,
)

logger = logging.getLogger(__name__)


def _is_admin(user):
    return user.is_superuser


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
    qs = qs.order_by('property_type', '-is_general', 'name')

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
    secrets = [
        {
            'key': key, 'label': label,
            'is_set': bool(getattr(django_settings, key, '')),
            'masked': app_settings.masked(getattr(django_settings, key, '')),
        }
        for key, label in app_settings.SECRET_KEYS
    ]
    google_redirect_uris = [
        request.build_absolute_uri(reverse(name))
        for name in ('google_login_callback', 'calendar_callback', 'gmail_callback')
    ]
    return render(request, 'core/admin_tools.html', {
        'properties': properties, 'secrets': secrets, 'google_redirect_uris': google_redirect_uris,
        'quo_phone_lines': _list_quo_phone_lines(),
        'scan_phone_number_id': django_settings.QUO_SCAN_PHONE_NUMBER_ID,
        'outbound_from_number': django_settings.QUO_DEFAULT_FROM_NUMBER,
    })


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
            for field in ['gate_code', 'lockbox_code', 'alarm_code', 'wifi_network', 'wifi_password', 'access_notes']:
                setattr(prop, field, request.POST.get(field, '').strip())
            prop.save(update_fields=['gate_code', 'lockbox_code', 'alarm_code', 'wifi_network', 'wifi_password', 'access_notes'])
            messages.success(request, 'Access info saved.')
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
        'attributes': PropertyAttribute.objects.filter(is_active=True),
        'assigned_attribute_ids': assigned_attribute_ids,
        'followup_batches': _group_followups(prop.followups.select_related('contact')[:30]),
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
        return render(request, 'core/_contact_table_rows.html', {'contacts': qs})

    return render(request, 'core/contact_list.html', {
        'contacts': qs,
        'type_choices': Contact.ContactType.choices,
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
    trade_value = form['trade'].value() or ''
    return {
        'form': form, 'properties_by_type': properties_by_type(),
        'selected_property_ids': ','.join(selected_ids),
        'trade_choices': TRADE_CHOICES,
        'trade_is_other': bool(trade_value) and trade_value not in TRADE_CHOICES,
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
        form = ContactForm(request.POST, instance=contact)
        if form.is_valid():
            form.save()
            messages.success(request, f'Contact "{contact.name}" updated.')
            return redirect('contact_list')
    else:
        form = ContactForm(instance=contact)
    return render(request, 'core/contact_form.html', _contact_form_context(form, is_new=False, contact=contact))


@login_required
def contact_review(request):
    candidates = ContactImportCandidate.objects.filter(status=ContactImportCandidate.Status.PENDING)
    update_candidates = ContactUpdateCandidate.objects.filter(
        status=ContactUpdateCandidate.Status.PENDING,
    ).select_related('contact')
    return render(request, 'core/contact_review.html', {
        'candidates': candidates,
        'update_candidates': update_candidates,
        'type_choices': Contact.ContactType.choices,
        'properties_by_type': properties_by_type(),
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


def _template_source_label(template, prop, override, assigned_attribute_ids):
    """Human-readable reason a template shows up in this property's
    effective set — purely explanatory, not used for any logic. A short
    caption shown alongside the row's Inherited/Added/Excluded/Modified
    state (see _row_state) — this is the "why", that's the "what"."""
    if override and override.action == PropertyTemplateOverride.Action.INCLUDE:
        if override.frequency or override.assigned_role or override.assigned_staff_id:
            return 'Manual override'
        return 'Manual add'
    if template.target_type == TicketTemplate.TargetType.PROPERTY:
        return 'Auto — direct assignment'
    if template.target_type == TicketTemplate.TargetType.CONTACT:
        return f'Auto — linked to {template.contact}' if template.contact_id else 'Auto — contact match'
    package_step = TaskPackageTemplate.objects.filter(
        template=template, package__is_active=True, package__property_assignments__property=prop,
    ).select_related('package').first()
    if package_step:
        return f'Auto — package: {package_step.package.title}'
    required_ids = set(template.required_attributes.values_list('id', flat=True))
    if required_ids and required_ids <= assigned_attribute_ids:
        return 'Auto — attribute match'
    if template.property_types:
        return 'Auto — type match'
    return 'Auto — every type'


class RowState:
    """The 4 explicit states a Task Rule row can show for one property —
    deliberately not a stored field: it's always derived fresh from
    (override, base_match) so it can never drift from what the applicability
    engine would actually do."""
    INHERITED = 'inherited'
    ADDED = 'added'
    EXCLUDED = 'excluded'
    MODIFIED = 'modified'


ROW_STATE_LABELS = {
    RowState.INHERITED: 'Inherited',
    RowState.ADDED: 'Added directly',
    RowState.EXCLUDED: 'Excluded locally',
    RowState.MODIFIED: 'Modified locally',
}


def _row_state(override, base_match):
    """Classifies one (template, property) pairing. Returns None when the
    rule neither applies nor has any override at all — those don't get a
    row. An EXCLUDE override always shows (even if the rule no longer
    matches at all — e.g. its property_types changed since — surfaced for
    transparency rather than silently pruned). An INCLUDE override that
    changes nothing on a rule that already matches is treated as Inherited
    (the override is a redundant no-op)."""
    if override and override.action == PropertyTemplateOverride.Action.EXCLUDE:
        return RowState.EXCLUDED
    if override and override.action == PropertyTemplateOverride.Action.INCLUDE:
        if not base_match:
            return RowState.ADDED
        field_changed = bool(
            override.frequency or override.assigned_role or override.assigned_staff_id
            or override.workday_of_month is not None
        )
        return RowState.MODIFIED if field_changed else RowState.INHERITED
    return RowState.INHERITED if base_match else None


@login_required
def property_recurring_tasks(request, pk):
    """A property's operational profile: the recurring task templates the
    applicability rule engine (tickets.services.applicability) currently
    resolves for it, plus the controls to review/adjust that result — add
    a one-off template, exclude an applied one, override its frequency/
    department/assignee for this property only, or toggle which task
    packages and characteristics apply. Computed live on every load, same
    as generation itself — see the build plan for why this isn't cached."""
    prop = get_object_or_404(Property, pk=pk)

    if request.method == 'POST':
        action = request.POST.get('action')
        template_id = request.POST.get('template_id')

        if action == 'exclude' and template_id:
            PropertyTemplateOverride.objects.update_or_create(
                property=prop, template_id=template_id,
                defaults={'action': PropertyTemplateOverride.Action.EXCLUDE, 'created_by': request.user},
            )
            messages.success(request, 'Excluded for this property.')
        elif action == 'reset' and template_id:
            PropertyTemplateOverride.objects.filter(property=prop, template_id=template_id).delete()
            messages.success(request, 'Reset to default.')
        elif action == 'adjust' and template_id:
            form = PropertyTemplateOverrideForm(request.POST)
            if form.is_valid():
                PropertyTemplateOverride.objects.update_or_create(
                    property=prop, template_id=template_id,
                    defaults={
                        'action': PropertyTemplateOverride.Action.INCLUDE,
                        'frequency': form.cleaned_data['frequency'],
                        'workday_of_month': form.cleaned_data['workday_of_month'],
                        'assigned_role': form.cleaned_data['assigned_role'],
                        'assigned_staff': form.cleaned_data['assigned_staff'],
                        'created_by': request.user,
                    },
                )
                messages.success(request, 'Adjustment saved.')
            else:
                messages.error(request, 'Could not save that adjustment.')
        elif action == 'add_one_off' and template_id:
            PropertyTemplateOverride.objects.update_or_create(
                property=prop, template_id=template_id,
                defaults={'action': PropertyTemplateOverride.Action.INCLUDE, 'created_by': request.user},
            )
            messages.success(request, 'Added.')
        elif action == 'toggle_package':
            package_id = request.POST.get('package_id')
            existing = PropertyPackage.objects.filter(property=prop, package_id=package_id)
            if existing.exists():
                existing.delete()
                messages.success(request, 'Package removed.')
            else:
                PropertyPackage.objects.create(property=prop, package_id=package_id)
                messages.success(request, 'Package added.')
        elif action == 'toggle_attribute':
            attribute_id = request.POST.get('attribute_id')
            existing = PropertyAttributeAssignment.objects.filter(property=prop, attribute_id=attribute_id)
            if existing.exists():
                existing.delete()
                messages.success(request, 'Attribute removed.')
            else:
                PropertyAttributeAssignment.objects.create(property=prop, attribute_id=attribute_id)
                messages.success(request, 'Attribute added.')
        return redirect('property_recurring_tasks', pk=prop.pk)

    overrides = {o.template_id: o for o in PropertyTemplateOverride.objects.filter(property=prop)}
    assigned_attribute_ids = set(prop.attribute_assignments.values_list('attribute_id', flat=True))
    assigned_package_ids = set(prop.packages.values_list('package_id', flat=True))

    # Every active, property-scopable template is a candidate row — not just
    # the ones that currently apply — so an EXCLUDE override on a rule that
    # WOULD otherwise match still shows up (as Excluded locally) instead of
    # silently disappearing. COMPANY-target rules never scope to one
    # property, so they're never candidates here at all.
    candidates = (
        TicketTemplate.objects.filter(is_active=True)
        .exclude(target_type=TicketTemplate.TargetType.COMPANY)
        .prefetch_related('required_attributes')
    )

    frequency_labels = dict(Frequency.choices)
    role_labels = dict(StaffProfile.Role.choices)
    rows = []
    for t in candidates:
        override = overrides.get(t.pk)
        base_match = applicability.template_applies_to_property(t, prop, respect_overrides=False)
        state = _row_state(override, base_match)
        if state is None:
            continue
        effective = applicability.effective_settings(t, prop, override=override)
        effective['frequency_display'] = frequency_labels.get(effective['frequency'], effective['frequency'])
        effective['assigned_role_display'] = role_labels.get(effective['assigned_role'], 'Unassigned')
        rows.append({
            'template': t,
            'override': override,
            'state': state,
            'state_label': ROW_STATE_LABELS[state],
            'base_match': base_match,
            'effective': effective,
            'source': _template_source_label(t, prop, override, assigned_attribute_ids),
        })
    rows.sort(key=lambda r: r['template'].title)

    return render(request, 'core/property_recurring_tasks.html', {
        'property': prop,
        'rows': rows,
        'packages': TaskPackage.objects.filter(is_active=True),
        'assigned_package_ids': assigned_package_ids,
        'attributes': PropertyAttribute.objects.filter(is_active=True),
        'assigned_attribute_ids': assigned_attribute_ids,
        'addable_templates': (
            TicketTemplate.objects.filter(is_active=True)
            .exclude(target_type=TicketTemplate.TargetType.COMPANY)
            .exclude(pk__in=[r['template'].pk for r in rows])
            .order_by('title')
        ),
        'frequency_choices': Frequency.choices,
        'role_choices': StaffProfile.Role.choices,
        'staff_list': StaffProfile.objects.select_related('user'),
    })
