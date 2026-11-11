from datetime import datetime, timedelta
from urllib.parse import urlparse

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.http import url_has_allowed_host_and_scheme

from core.models import Contact, Property, StaffProfile
from tickets.models import Ticket
from vendorportal.models import AccessAttempt

from .forms import ProcessAttachmentUploadForm, ProcessRunExternalAccessForm, ProcessTemplateAttachmentForm
from .projects import copy_steps
from .models import (
    ProcessRun,
    ProcessRunExternalAccess,
    ProcessRunStep,
    ProcessTemplate,
    ProcessTemplateAttachment,
    ProcessTemplateLink,
    ProcessTemplateStep,
    StepType,
)

# Step types whose completion is just "write whatever the form posted into
# response and mark complete" — everything else needs its own view because
# it either calls an external service (Google Calendar), creates another
# record (a Ticket), or has branching behavior (approval routing).
SIMPLE_STEP_TYPES = [
    StepType.CHECKBOX, StepType.CHECKLIST, StepType.SHORT_TEXT, StepType.LONG_TEXT,
    StepType.NUMBER_CURRENCY, StepType.DATE_TIME, StepType.DROPDOWN_MULTISELECT, StepType.RECORD_SELECTOR,
    StepType.CALCULATION_FORMULA,
]
UPLOAD_STEP_TYPES = [StepType.DOCUMENT_UPLOAD, StepType.PHOTO_VIDEO_UPLOAD, StepType.DIGITAL_SIGNATURE]


def _client_ip(request):
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '0.0.0.0')


def _run_redirect(request, run):
    """Back to where the step was worked: the run's own screen when that is where the person was, otherwise the ticket, property or contact it belongs to."""
    if run.session_line_id:
        return redirect('ws_run_detail', pk=run.pk)
    referer = request.META.get('HTTP_REFERER', '')
    if referer and url_has_allowed_host_and_scheme(referer, allowed_hosts={request.get_host()}) and urlparse(referer).path == reverse('ws_run_detail', args=[run.pk]):
        return redirect('ws_run_detail', pk=run.pk)
    if run.ticket_id:
        return redirect('ticket_detail', pk=run.ticket_id)
    if run.property_id:
        return redirect('property_detail', pk=run.property_id)
    return redirect('contact_edit', pk=run.contact_id)


# ---------------------------------------------------------------------------
# Builder — template authoring
# ---------------------------------------------------------------------------

@login_required
def process_template_list(request):
    templates = ProcessTemplate.objects.all()
    return render(request, 'processes/template_list.html', {'templates': templates})


@login_required
def process_template_create(request):
    if request.method == 'POST':
        template = ProcessTemplate.objects.create(
            name=request.POST.get('name', '').strip() or 'Untitled process',
            description=request.POST.get('description', '').strip(),
            category=request.POST.get('category', '').strip(),
            kind=request.POST.get('kind') if request.POST.get('kind') in ProcessTemplate.Kind.values else ProcessTemplate.Kind.PROJECT,
        )
        messages.success(request, f'Created "{template.name}".')
        return redirect('process_template_edit', pk=template.pk)
    return redirect('process_template_list')


@login_required
def process_template_edit(request, pk):
    template = get_object_or_404(ProcessTemplate, pk=pk)
    if request.method == 'POST':
        template.name = request.POST.get('name', template.name).strip()
        template.description = request.POST.get('description', '').strip()
        template.category = request.POST.get('category', '').strip()
        template.is_active = bool(request.POST.get('is_active'))
        if request.POST.get('kind') in ProcessTemplate.Kind.values:
            template.kind = request.POST['kind']
        template.save()
        messages.success(request, 'Saved.')
        return redirect('process_template_edit', pk=template.pk)
    return render(request, 'processes/template_form.html', {
        'template': template,
        'steps': template.steps.all(),
        'step_types': [(StepType.CHECKBOX.value, StepType.CHECKBOX.label)] if template.kind == ProcessTemplate.Kind.CHECKLIST else StepType.choices,
        'links': template.links.select_related('template_step'),
        'documents': template.attachments.select_related('template_step'),
        'assignee_choices': ProcessTemplateStep._meta.get_field('assignee_role').choices,
    })


@login_required
def process_template_preview(request, pk):
    template = get_object_or_404(ProcessTemplate, pk=pk)
    return render(request, 'processes/template_preview.html', {'template': template, 'steps': template.steps.all()})


@login_required
def process_template_step_add(request, template_pk):
    template = get_object_or_404(ProcessTemplate, pk=template_pk)
    if request.method == 'POST':
        next_order = (template.steps.order_by('-sequence_order').values_list('sequence_order', flat=True).first() or 0) + 1
        step_type = request.POST.get('step_type', StepType.CHECKBOX)
        if step_type not in StepType.values or template.kind == ProcessTemplate.Kind.CHECKLIST:
            step_type = StepType.CHECKBOX      # a process is plain checkbox steps; richer step types belong to projects
        ProcessTemplateStep.objects.create(
            process_template=template, sequence_order=next_order, step_type=step_type,
            label=request.POST.get('label', '').strip() or 'Untitled step',
        )
        messages.success(request, 'Step added.')
    return redirect('process_template_edit', pk=template.pk)


@login_required
def process_template_step_edit(request, step_pk):
    step = get_object_or_404(ProcessTemplateStep, pk=step_pk)
    if request.method == 'POST':
        step.label = request.POST.get('label', step.label).strip()
        step.help_text = request.POST.get('help_text', '').strip()
        step.is_required = bool(request.POST.get('is_required'))
        step.requires_upload = bool(request.POST.get('requires_upload'))
        step.assignee_role = request.POST.get('assignee_role', '')
        staff_id = request.POST.get('assignee_staff_id')
        step.assignee_staff_id = staff_id or None
        raw_deadline = request.POST.get('deadline_days_after_start', '')
        step.deadline_days_after_start = int(raw_deadline) if raw_deadline.isdigit() else None
        step.config = _parse_step_config(step.step_type, request.POST)
        step.save()
        messages.success(request, 'Step saved.')
    return redirect('process_template_edit', pk=step.process_template_id)


@login_required
def process_template_step_delete(request, step_pk):
    step = get_object_or_404(ProcessTemplateStep, pk=step_pk)
    template_pk = step.process_template_id
    if request.method == 'POST':
        step.delete()
        messages.success(request, 'Step removed.')
    return redirect('process_template_edit', pk=template_pk)


def _step_of(template, raw):
    return template.steps.filter(pk=raw).first() if str(raw or '').isdigit() else None


@login_required
def process_template_link_add(request, template_pk):
    template = get_object_or_404(ProcessTemplate, pk=template_pk)
    if request.method == 'POST':
        url = (request.POST.get('url') or '').strip()
        if not url.lower().startswith(('http://', 'https://')):
            messages.error(request, 'Use a web address that starts with http:// or https://.')
        else:
            ProcessTemplateLink.objects.create(process_template=template, template_step=_step_of(template, request.POST.get('step')),
                                               label=(request.POST.get('label') or '').strip()[:200], url=url[:500])
            messages.success(request, 'Link added.')
    return redirect('process_template_edit', pk=template.pk)


@login_required
def process_template_link_delete(request, link_pk):
    link = get_object_or_404(ProcessTemplateLink, pk=link_pk)
    template_pk = link.process_template_id
    if request.method == 'POST':
        link.delete()
        messages.success(request, 'Link removed.')
    return redirect('process_template_edit', pk=template_pk)


@login_required
def process_template_attachment_add(request, template_pk):
    template = get_object_or_404(ProcessTemplate, pk=template_pk)
    if request.method == 'POST':
        form = ProcessTemplateAttachmentForm(request.POST, request.FILES)
        if form.is_valid():
            doc = form.save(commit=False)
            doc.process_template, doc.template_step = template, _step_of(template, request.POST.get('step'))
            doc.save()
            messages.success(request, 'Document added.')
        else:
            messages.error(request, ' '.join(m for errs in form.errors.values() for m in errs))
    return redirect('process_template_edit', pk=template.pk)


@login_required
def process_template_attachment_delete(request, attachment_pk):
    doc = get_object_or_404(ProcessTemplateAttachment, pk=attachment_pk)
    template_pk = doc.process_template_id
    if request.method == 'POST':
        doc.delete()
        messages.success(request, 'Document removed.')
    return redirect('process_template_edit', pk=template_pk)


@login_required
def process_template_step_move(request, step_pk):
    step = get_object_or_404(ProcessTemplateStep, pk=step_pk)
    if request.method == 'POST':
        step.move(request.POST.get('direction', ''))
    return redirect('process_template_edit', pk=step.process_template_id)


def _parse_step_config(step_type, post):
    """Reads the step-type-specific config sub-form fields — see
    ProcessTemplateStep.config's docstring for the shape each type expects."""
    if step_type == StepType.CHECKLIST:
        items = [line.strip() for line in post.get('config_checklist_items', '').splitlines() if line.strip()]
        return {'items': items}
    if step_type == StepType.DROPDOWN_MULTISELECT:
        options = [line.strip() for line in post.get('config_options', '').splitlines() if line.strip()]
        return {'options': options, 'multi': bool(post.get('config_multi'))}
    if step_type == StepType.RECORD_SELECTOR:
        return {'target': post.get('config_target', 'property')}
    if step_type == StepType.TASK_ASSIGNMENT:
        return {
            'default_role': post.get('config_default_role', ''),
            'default_days_until_due': post.get('config_default_days_until_due') or None,
        }
    if step_type == StepType.CALENDAR_EVENT:
        return {
            'add_meet': bool(post.get('config_add_meet')),
            'default_duration_minutes': int(post.get('config_duration_minutes') or 60),
        }
    if step_type == StepType.CALCULATION_FORMULA:
        return {'formula': post.get('config_formula', '').strip(), 'output_label': post.get('config_output_label', '').strip()}
    if step_type == StepType.WAIT_TIMER:
        return {
            'wait_mode': post.get('config_wait_mode', 'manual_resume'),
            'duration_days': int(post.get('config_duration_days') or 0) or None,
        }
    if step_type == StepType.APPROVAL_DECISION:
        routes = []
        labels = post.getlist('config_route_label')
        targets = post.getlist('config_route_target')
        for label, target in zip(labels, targets):
            if not label.strip():
                continue
            routes.append({
                'label': label.strip(), 'action': 'jump_to' if target else 'continue', 'target_step_key': target,
            })
        return {'routes': routes}
    if step_type == StepType.NUMBER_CURRENCY:
        return {'is_currency': bool(post.get('config_is_currency'))}
    if step_type == StepType.GENERATE_DOCUMENT:
        parents = [p.strip() for p in post.get('config_parent_forms', '').split(',') if p.strip()]
        return {'form': post.get('config_form', '').strip(), 'parent_forms': parents, 'email': bool(post.get('config_email'))}
    if step_type == StepType.CREATE_CONTACT:
        return {'role': post.get('config_role', 'owner')}
    return {}


# ---------------------------------------------------------------------------
# Runtime — attach + run
# ---------------------------------------------------------------------------

@login_required
def process_run_attach(request):
    """Attaches one or more ProcessTemplates to a ticket, property, or
    contact — copies each template's steps onto a new ProcessRun (snapshot,
    not a live reference, same rationale as generate_recurring_tickets'
    template->instance copy)."""
    if request.method != 'POST':
        return redirect('dashboard')

    target_type = request.POST.get('target_type')
    target_id = request.POST.get('target_id')
    target_kwargs = {}
    if target_type == 'ticket':
        target = get_object_or_404(Ticket, pk=target_id)
        target_kwargs = {'ticket': target}
    elif target_type == 'property':
        target = get_object_or_404(Property, pk=target_id)
        target_kwargs = {'property': target}
    elif target_type == 'contact':
        target = get_object_or_404(Contact, pk=target_id)
        target_kwargs = {'contact': target}
    else:
        messages.error(request, 'Could not attach — unknown target.')
        return redirect('dashboard')

    template_ids = request.POST.getlist('process_template_ids')
    run = None
    for template in ProcessTemplate.objects.filter(pk__in=template_ids, is_active=True):
        run = ProcessRun.objects.create(process_template=template, created_by=request.user, **target_kwargs)
        copy_steps(run, template)
        messages.success(request, f'Attached "{template.name}".')
    if run is None:
        # Same open-redirect guard as tickets/views.py::_safe_back_url —
        # HTTP_REFERER is attacker-controlled (a form on any external page
        # can make a victim's browser send whatever Referer it likes to
        # this endpoint), so it must be validated to stay on-site before
        # ever being trusted as a redirect target.
        referer = request.META.get('HTTP_REFERER', '')
        if referer and url_has_allowed_host_and_scheme(referer, allowed_hosts={request.get_host()}):
            return redirect(referer)
        return redirect('dashboard')
    return _run_redirect(request, run)


@login_required
def process_run_step_update(request, step_pk):
    """Generic completion for the "simple" step types — everything whose
    value is just a form field written into response — see
    SIMPLE_STEP_TYPES."""
    step = get_object_or_404(ProcessRunStep, pk=step_pk)
    if request.method == 'POST':
        if step.requires_upload and not step.attachments.exists():
            messages.error(request, 'Attach a file to this step before completing it.')
            return _run_redirect(request, step.run)

        if step.step_type == StepType.CHECKBOX:
            step.response = {'checked': True}
        elif step.step_type == StepType.CHECKLIST:
            step.response = {'checked_items': request.POST.getlist('checklist_item')}
        elif step.step_type in (StepType.SHORT_TEXT, StepType.LONG_TEXT):
            step.response = {'text': request.POST.get('value', '').strip()}
        elif step.step_type == StepType.NUMBER_CURRENCY:
            raw = request.POST.get('value', '').strip()
            try:
                step.response = {'value': float(raw)} if raw else {}
            except ValueError:
                messages.error(request, 'Enter a valid number.')
                return _run_redirect(request, step.run)
        elif step.step_type == StepType.DATE_TIME:
            step.response = {'value': request.POST.get('value', '').strip()}
        elif step.step_type == StepType.DROPDOWN_MULTISELECT:
            step.response = {'selected': request.POST.getlist('selected')}
        elif step.step_type == StepType.RECORD_SELECTOR:
            step.response = {'model': step.config.get('target', ''), 'id': request.POST.get('record_id', '').strip()}
        elif step.step_type == StepType.CALCULATION_FORMULA:
            step.response = {'accepted': True}
        step.mark_complete(request.user)
        messages.success(request, 'Saved.')
    return _run_redirect(request, step.run)


@login_required
def process_run_step_upload(request, step_pk):
    """Saves a proof-of-completion upload against one ProcessRunStep — used
    by DOCUMENT_UPLOAD/PHOTO_VIDEO_UPLOAD, and by the digital-signature
    canvas (which posts its PNG through the same field)."""
    step = get_object_or_404(ProcessRunStep, pk=step_pk)
    if request.method == 'POST':
        form = ProcessAttachmentUploadForm(request.POST, request.FILES)
        if form.is_valid():
            attachment = form.save(commit=False)
            attachment.run_step = step
            attachment.uploaded_by = request.user
            attachment.save()
            if step.step_type == StepType.DIGITAL_SIGNATURE:
                step.response = {'attachment_id': attachment.pk}
                step.mark_complete(request.user)
            messages.success(request, 'File attached.')
        else:
            messages.error(request, ' '.join(
                error for errors in form.errors.values() for error in errors
            ) or 'Could not attach that file.')
    return _run_redirect(request, step.run)


@login_required
def process_run_step_complete_upload(request, step_pk):
    """DOCUMENT_UPLOAD/PHOTO_VIDEO_UPLOAD steps: mark complete once at
    least one file is attached (the file itself is added via
    process_run_step_upload above — this is the separate "I'm done with
    this step" confirm, matching every other step type's explicit
    complete action)."""
    step = get_object_or_404(ProcessRunStep, pk=step_pk)
    if request.method == 'POST':
        if not step.attachments.exists():
            messages.error(request, 'Attach at least one file first.')
        else:
            step.mark_complete(request.user)
            messages.success(request, 'Saved.')
    return _run_redirect(request, step.run)


@login_required
def process_run_step_assign_task(request, step_pk):
    """TASK_ASSIGNMENT — creates a real Ticket (this app's existing "task"
    concept, see tickets/models.py) linked to this step."""
    step = get_object_or_404(ProcessRunStep, pk=step_pk)
    if request.method == 'POST':
        run = step.run
        role = request.POST.get('assigned_role') or step.config.get('default_role', '')
        due_days = request.POST.get('due_days') or step.config.get('default_days_until_due')
        due_date = None
        if due_days:
            try:
                due_date = timezone.now() + timedelta(days=int(due_days))
            except (ValueError, TypeError):
                due_date = None
        ticket = Ticket.objects.create(
            title=step.label, description=f'Created from process step "{step.label}" ({run.process_template.name}).',
            property=run.property or (run.ticket.property if run.ticket else None),
            assigned_role=role if role in StaffProfile.Role.values else '', due_date=due_date,
            created_by=request.user,
        )
        step.response = {'ticket_id': ticket.pk}
        step.save(update_fields=['response'])
        messages.success(request, f'Created task "{ticket.title}".')
    return _run_redirect(request, step.run)


@login_required
def process_run_step_complete_task(request, step_pk):
    """Marks a TASK_ASSIGNMENT step complete once its linked ticket has
    reached a Ticket.DEPENDENCY_SATISFYING_STATUSES status — a manual
    confirm rather than a signal, matching this view's simple, explicit,
    one-view-per-action convention."""
    step = get_object_or_404(ProcessRunStep, pk=step_pk)
    if request.method == 'POST':
        ticket_id = step.response.get('ticket_id')
        ticket = Ticket.objects.filter(pk=ticket_id).first() if ticket_id else None
        if not ticket or ticket.status not in Ticket.DEPENDENCY_SATISFYING_STATUSES:
            messages.error(request, 'The linked task isn\'t finished yet.')
        else:
            step.mark_complete(request.user)
            messages.success(request, 'Saved.')
    return _run_redirect(request, step.run)


@login_required
def process_run_step_schedule_event(request, step_pk):
    """CALENDAR_EVENT — schedules on the logged-in staff member's own
    connected Google Calendar (no shared company calendar wired for writes
    today, see core/google_calendar.py), adding a Google Meet link when
    config['add_meet'] is set. Generalizes the old google_meet action type
    unchanged underneath."""
    from core.google_calendar import GoogleCalendarWriteError, create_event
    from core.models import GoogleCalendarToken

    step = get_object_or_404(ProcessRunStep, pk=step_pk)
    run = step.run
    token = GoogleCalendarToken.objects.filter(staff=getattr(request.user, 'staff_profile', None)).first()
    if not token:
        messages.error(request, 'Connect your Google Calendar first (see your department dashboard) to schedule an event.')
        return _run_redirect(request, run)

    if request.method == 'POST':
        start = parse_datetime(request.POST.get('event_datetime', ''))
        if start and timezone.is_naive(start):
            start = timezone.make_aware(start)
        if not start:
            messages.error(request, 'Pick a valid date and time — nothing was scheduled.')
            return _run_redirect(request, run)
        duration = step.config.get('default_duration_minutes', 60)
        end = start + timedelta(minutes=duration)
        calendar_id = token.enabled_calendar_ids[0] if token.enabled_calendar_ids else 'primary'
        try:
            event = create_event(
                token, calendar_id, summary=f'{run.process_template.name} — {step.label}',
                start=start, end=end, add_meet=bool(step.config.get('add_meet')),
            )
        except GoogleCalendarWriteError as e:
            messages.error(request, str(e))
            return _run_redirect(request, run)

        entry_points = event.get('conferenceData', {}).get('entryPoints', [])
        video_link = next((e['uri'] for e in entry_points if e.get('entryPointType') == 'video'), event.get('hangoutLink', ''))
        phone_entries = [e for e in entry_points if e.get('entryPointType') == 'phone']
        dial_in = (phone_entries[0].get('label') or phone_entries[0].get('uri', '')) if phone_entries else ''

        step.response = {
            'event_id': event.get('id', ''), 'calendar_id': calendar_id, 'meeting_link': video_link,
            'meeting_dial_in': dial_in, 'event_datetime': start.isoformat(),
        }
        step.mark_complete(request.user)
        messages.success(request, 'Event scheduled.')
    return _run_redirect(request, run)


@login_required
def process_run_step_decide(request, step_pk):
    """APPROVAL_DECISION — picking a configured route either continues
    (nothing else changes) or jumps forward, skipping every required step
    strictly between this one and the target (marking them not-required)
    so the run doesn't get permanently stuck on a branch that was routed
    around. Deliberately simple "skip ahead" routing, not an arbitrary
    flowchart, per this feature's agreed linear-first scope."""
    step = get_object_or_404(ProcessRunStep, pk=step_pk)
    if request.method == 'POST':
        route_label = request.POST.get('route_label', '')
        notes = request.POST.get('notes', '').strip()
        routes = step.config.get('routes', [])
        route = next((r for r in routes if r.get('label') == route_label), None)
        step.response = {'decision': route_label, 'notes': notes}
        step.mark_complete(request.user)
        if route and route.get('action') == 'jump_to' and route.get('target_step_key'):
            target = step.run.steps.filter(step_key=route['target_step_key']).first()
            if target:
                step.run.steps.filter(
                    sequence_order__gt=step.sequence_order, sequence_order__lt=target.sequence_order,
                ).update(is_required=False)
        messages.success(request, 'Decision recorded.')
    return _run_redirect(request, step.run)


@login_required
def process_run_step_mark_sent(request, step_pk):
    """EMAIL_TEXT_ACTION — an explicit "mark sent" confirm after using the
    linked Follow-Up compose, rather than fragile auto-detection across
    every possible attached-record type."""
    step = get_object_or_404(ProcessRunStep, pk=step_pk)
    if request.method == 'POST':
        step.response = {'note': request.POST.get('note', '').strip()}
        step.mark_complete(request.user)
        messages.success(request, 'Marked sent.')
    return _run_redirect(request, step.run)


@login_required
def process_run_step_meeting(request, step_pk):
    """MEETING_DETAILS: the date, time, place and video link of a meeting, written once and used to fill every notice, agenda and affidavit made later in the run."""
    step = get_object_or_404(ProcessRunStep, pk=step_pk)
    if request.method == 'POST':
        raw_date = request.POST.get('date', '').strip()
        try:
            date_value = datetime.strptime(raw_date, '%Y-%m-%d').date()
        except ValueError:
            messages.error(request, 'Enter the date of the meeting.')
            return _run_redirect(request, step.run)
        time_text, place = request.POST.get('time', '').strip(), request.POST.get('place', '').strip()
        if not time_text or not place:
            messages.error(request, 'Enter the time and the place of the meeting.')
            return _run_redirect(request, step.run)
        step.response = {'date': date_value.isoformat(), 'time': time_text[:40], 'place': place[:300], 'video_link': request.POST.get('video_link', '').strip()[:300]}
        step.mark_complete(request.user)
        messages.success(request, 'Meeting details saved. They fill in the notices and the affidavit.')
    return _run_redirect(request, step.run)


@login_required
def process_run_step_person(request, step_pk):
    """CREATE_CONTACT: a person's name, phone and email, made into a contact (an owner or a tenant) linked to the association and unit of this run, so the letters can be emailed to them."""
    from .projects import run_property, run_unit
    step = get_object_or_404(ProcessRunStep, pk=step_pk)
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        email = request.POST.get('email', '').strip()
        phone = request.POST.get('phone', '').strip()
        if not name:
            messages.error(request, 'Enter the name.')
            return _run_redirect(request, step.run)
        if email:
            try:
                validate_email(email)
            except ValidationError:
                messages.error(request, 'Enter a valid email address, or leave it blank for now.')
                return _run_redirect(request, step.run)
        if phone:
            try:
                Contact._meta.get_field('phone').run_validators(phone)
            except ValidationError:
                messages.error(request, 'Enter the phone as XXX-XXX-XXXX, for example 561-599-6300.')
                return _run_redirect(request, step.run)
        kind = Contact.ContactType.TENANT if step.config.get('role') == 'tenant' else Contact.ContactType.OWNER
        contact = Contact.objects.filter(email__iexact=email, contact_type=kind).first() if email else None
        if contact is None:
            contact = Contact.objects.create(name=name, email=email, phone=phone, contact_type=kind)
        prop = run_property(step.run)
        if prop:
            contact.properties.add(prop)
            unit = run_unit(step.run, prop)
            if unit:
                contact.units.add(unit)
        step.response = {'contact_id': contact.pk, 'name': contact.name, 'email': contact.email, 'phone': contact.phone}
        step.mark_complete(request.user)
        messages.success(request, f'{contact.name} saved as a contact.' + ('' if contact.email else ' No email yet: add it before sending a letter by email.'))
    return _run_redirect(request, step.run)


@login_required
def process_run_step_resume_wait(request, step_pk):
    """WAIT_TIMER — staff override to resume immediately instead of waiting
    for the configured date/duration (or for resume_expired_wait_steps to
    catch it on its next scheduled pass)."""
    step = get_object_or_404(ProcessRunStep, pk=step_pk)
    if request.method == 'POST':
        step.response = {**step.response, 'resumed_at': timezone.now().isoformat(), 'resumed_by': 'manual'}
        step.mark_complete(request.user)
        messages.success(request, 'Resumed.')
    return _run_redirect(request, step.run)


# ---------------------------------------------------------------------------
# External secure link
# ---------------------------------------------------------------------------

@login_required
def process_run_external_link_create(request, run_pk):
    run = get_object_or_404(ProcessRun, pk=run_pk)
    if request.method == 'POST':
        form = ProcessRunExternalAccessForm(request.POST)
        if form.is_valid():
            access = form.save(commit=False)
            access.run = run
            access.created_by = request.user
            access.assigned_step_keys = request.POST.getlist('assigned_step_keys')
            access.save()
            link_url = request.build_absolute_uri(f'/processes/access/{access.token}/')
            messages.success(request, f'Link created: {link_url}')
        else:
            messages.error(request, 'Could not create link.')
    return _run_redirect(request, run)


def process_external_access(request, token):
    if AccessAttempt.is_rate_limited(_client_ip(request)):
        return HttpResponse('Too many requests. Please try again later.', status=429)

    access = get_object_or_404(ProcessRunExternalAccess, token=token)
    if not access.is_valid():
        return render(request, 'processes/external_expired.html', status=410)

    steps = access.visible_steps()
    if request.method == 'POST':
        step = get_object_or_404(steps, pk=request.POST.get('step_id'))
        action = request.POST.get('action')
        if action == 'upload':
            form = ProcessAttachmentUploadForm(request.POST, request.FILES)
            if form.is_valid():
                attachment = form.save(commit=False)
                attachment.run_step = step
                attachment.save()
                # Unlike the staff-side flow (process_run_step_upload +
                # a separate process_run_step_complete_upload confirm),
                # external_access.html gives DOCUMENT_UPLOAD/
                # PHOTO_VIDEO_UPLOAD the exact same single-button "Upload"
                # form as DIGITAL_SIGNATURE, with no second step to mark
                # it done — so without this, those two step types could
                # never actually complete through an external link: the
                # same upload form just re-rendered forever since
                # is_complete never became True. No `response` set for
                # these two (matching process_run_step_complete_upload's
                # own convention — attachments.exists() is the proof,
                # nothing else needs recording), unlike digital_signature,
                # which does store which attachment IS the signature.
                if step.step_type == StepType.DIGITAL_SIGNATURE:
                    step.response = {'attachment_id': attachment.pk}
                    step.mark_complete()
                elif step.step_type in (StepType.DOCUMENT_UPLOAD, StepType.PHOTO_VIDEO_UPLOAD):
                    step.mark_complete()
                messages.success(request, 'File attached.')
        elif step.requires_upload and not step.attachments.exists() and not request.FILES.get('file'):
            # CHECKBOX/CHECKLIST/SHORT_TEXT/LONG_TEXT can have
            # requires_upload set too (any step type can, per
            # ProcessRunStep.requires_upload's own docstring) — but this
            # template renders their answer AND proof-of-completion file
            # in one combined form (no separate upload step like DOCUMENT_
            # UPLOAD/PHOTO_VIDEO_UPLOAD/DIGITAL_SIGNATURE get), so without
            # this branch a requires_upload step of these types could
            # never actually complete externally: the old code just fell
            # through this elif with no attachment ever having been
            # possible to create, silently re-rendering the same form
            # forever with no explanation.
            messages.error(request, 'Attach a file to confirm this step.')
        elif not step.requires_upload or step.attachments.exists() or request.FILES.get('file'):
            if request.FILES.get('file'):
                upload_form = ProcessAttachmentUploadForm(request.POST, request.FILES)
                if upload_form.is_valid():
                    attachment = upload_form.save(commit=False)
                    attachment.run_step = step
                    attachment.save()
                else:
                    messages.error(request, ' '.join(
                        error for errors in upload_form.errors.values() for error in errors
                    ) or 'Could not attach that file.')
                    return redirect('process_external_access', token=token)
            if step.step_type == StepType.CHECKBOX:
                step.response = {'checked': True}
            elif step.step_type == StepType.CHECKLIST:
                step.response = {'checked_items': request.POST.getlist('checklist_item')}
            elif step.step_type in (StepType.SHORT_TEXT, StepType.LONG_TEXT):
                step.response = {'text': request.POST.get('value', '').strip()}
            step.mark_complete()
            messages.success(request, 'Saved.')
        return redirect('process_external_access', token=token)

    return render(request, 'processes/external_access.html', {'access': access, 'run': access.run, 'steps': steps})
