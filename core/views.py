import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from tickets.models import Frequency, PropertyPackage, PropertyTemplateOverride, TaskPackage, TaskPackageTemplate, TicketTemplate
from tickets.services import applicability

from . import google_calendar, places, usps
from .forms import ContactForm, PropertyForm, PropertyTemplateOverrideForm
from .models import (
    Contact, ContactImportCandidate, GoogleCalendarToken, Property, PropertyAttribute,
    PropertyAttributeAssignment, StaffProfile, is_valid_phone, properties_by_type, property_dropdown_queryset,
)

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

    return render(request, 'core/property_list.html', {
        'properties': qs,
        'type_choices': Property.Type.choices,
        'q': q,
        'selected_type': selected_type,
        'show_inactive': show_inactive,
    })


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
            messages.success(request, f'Property "{prop.name}" created — review its recurring tasks below.')
            return redirect('property_recurring_tasks', pk=prop.pk)
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
            return redirect('property_list')
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
def contact_list(request):
    qs = Contact.objects.prefetch_related('properties')
    q = request.GET.get('q', '').strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(phone__icontains=q) | Q(email__icontains=q))
    selected_type = request.GET.get('type', '')
    if selected_type:
        qs = qs.filter(contact_type=selected_type)
    qs = qs.order_by('name')

    return render(request, 'core/contact_list.html', {
        'contacts': qs,
        'type_choices': Contact.ContactType.choices,
        'q': q,
        'selected_type': selected_type,
        'pending_review_count': ContactImportCandidate.objects.filter(
            status=ContactImportCandidate.Status.PENDING,
        ).count(),
    })


def _contact_form_context(form, **extra):
    selected_ids = [str(v.pk if hasattr(v, 'pk') else v) for v in (form['properties'].value() or [])]
    return {
        'form': form, 'properties': property_dropdown_queryset(),
        'selected_property_ids': ','.join(selected_ids), **extra,
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
    return render(request, 'core/contact_review.html', {
        'candidates': candidates,
        'type_choices': Contact.ContactType.choices,
        'properties_by_type': properties_by_type(),
    })


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


@login_required
def contact_review_approve(request, pk):
    candidate = get_object_or_404(ContactImportCandidate, pk=pk, status=ContactImportCandidate.Status.PENDING)
    if request.method == 'POST':
        name = request.POST.get('name', '').strip() or candidate.name
        phone = request.POST.get('phone', '').strip()
        email = request.POST.get('email', '').strip()
        contact_type = request.POST.get('contact_type') or candidate.suggested_contact_type
        trade = request.POST.get('trade', '').strip()
        property_id = request.POST.get('property_id') or None

        if not is_valid_phone(phone):
            messages.error(request, 'Phone must be in XXX-XXX-XXXX format — nothing was approved.')
            return redirect('contact_review')

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
        candidate.resolved_by = request.user
        candidate.resolved_contact = contact
        candidate.save()
        messages.success(
            request,
            f'Approved — {"linked to existing" if existing else "created"} contact "{contact.name}".',
        )
    return redirect('contact_review')


@login_required
def contact_review_reject(request, pk):
    candidate = get_object_or_404(ContactImportCandidate, pk=pk, status=ContactImportCandidate.Status.PENDING)
    if request.method == 'POST':
        candidate.status = ContactImportCandidate.Status.REJECTED
        candidate.resolved_at = timezone.now()
        candidate.resolved_by = request.user
        candidate.save()
        messages.success(request, f'Rejected "{candidate.name}".')
    return redirect('contact_review')


def _template_source_label(template, prop, override, assigned_attribute_ids):
    """Human-readable reason a template shows up in this property's
    effective set — purely explanatory, not used for any logic."""
    if override and override.action == PropertyTemplateOverride.Action.INCLUDE:
        if override.frequency or override.assigned_role or override.assigned_staff_id:
            return 'Manual override'
        return 'Manual add'
    if template.property_id:
        return 'Auto — direct assignment'
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

    effective_templates = applicability.effective_templates_for_property(prop)
    overrides = {o.template_id: o for o in PropertyTemplateOverride.objects.filter(property=prop)}
    assigned_attribute_ids = set(prop.attribute_assignments.values_list('attribute_id', flat=True))
    assigned_package_ids = set(prop.packages.values_list('package_id', flat=True))

    frequency_labels = dict(Frequency.choices)
    role_labels = dict(StaffProfile.Role.choices)
    rows = []
    for t in effective_templates:
        override = overrides.get(t.pk)
        effective = applicability.effective_settings(t, prop, override=override)
        effective['frequency_display'] = frequency_labels.get(effective['frequency'], effective['frequency'])
        effective['assigned_role_display'] = role_labels.get(effective['assigned_role'], 'Unassigned')
        rows.append({
            'template': t,
            'override': override,
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
            .exclude(pk__in=[t.pk for t in effective_templates])
            .order_by('title')
        ),
        'frequency_choices': Frequency.choices,
        'role_choices': StaffProfile.Role.choices,
        'staff_list': StaffProfile.objects.select_related('user'),
    })
