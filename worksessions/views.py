from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from core.models import PropertyAttribute, StaffProfile

from .forms import SessionTemplateForm
from .models import Session, SessionLine, SessionTemplate
from .services import generation as generation_service
from .services import lifecycle as lifecycle_service


@login_required
def my_sessions(request):
    """The default Sessions landing page — open sessions for the logged-in
    person, soonest due first. Deliberately not a ticket queue: no status
    filters, no assignment picker, just "what do I still need to submit."
    """
    staff = getattr(request.user, 'staff_profile', None)
    sessions = (
        Session.objects.filter(owner=staff, status=Session.Status.OPEN)
        .select_related('template')
        .prefetch_related('lines')
        .order_by('due_at', 'opens_at')
    ) if staff else Session.objects.none()

    recently_submitted = (
        Session.objects.filter(owner=staff, status=Session.Status.SUBMITTED)
        .select_related('template').order_by('-submitted_at')[:10]
    ) if staff else Session.objects.none()

    today = timezone.localdate()
    return render(request, 'sessions/my_sessions.html', {
        'sessions': sessions,
        'recently_submitted': recently_submitted,
        'today': today,
    })


@login_required
def session_detail(request, pk):
    session = get_object_or_404(
        Session.objects.select_related('template', 'owner__user').prefetch_related('lines'), pk=pk,
    )
    # Line-level actions (set_line_state/promote) are fired from a per-line
    # form and, on a session with a lot of lines, happen many times in a
    # row — a plain redirect reloads the whole page and resets scroll to
    # the top after every single click, which is exactly the friction the
    # one-click done toggle was built to avoid. AJAX-only in practice (see
    # ticket_related_contacts' identical comment on why): the page-local
    # script always sends X-Requested-With. The non-AJAX branch is kept
    # only as a plain-POST fallback.
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'set_line_state':
            line = get_object_or_404(SessionLine, pk=request.POST.get('line_id'), session=session)
            state = request.POST.get('state')
            if state not in SessionLine.State.values:
                if is_ajax:
                    return JsonResponse({'success': False, 'error': 'Invalid line state.'})
                messages.error(request, 'Invalid line state.')
                return redirect('session_detail', pk=session.pk)
            try:
                lifecycle_service.set_line_state(
                    line, state,
                    skip_reason=request.POST.get('skip_reason', '').strip(),
                    notes=request.POST.get('notes'),
                )
            except ValidationError as exc:
                error = ' '.join(exc.messages)
                if is_ajax:
                    return JsonResponse({'success': False, 'error': error})
                messages.error(request, error)
                return redirect('session_detail', pk=session.pk)
            if is_ajax:
                # Not session.progress() — `session` was fetched with
                # prefetch_related('lines') up top, so session.lines.all()
                # would return that stale cached queryset (evaluated before
                # this request's own update) rather than reflecting the
                # state just saved above. A fresh queryset sidesteps it.
                total = SessionLine.objects.filter(session=session).count()
                done = SessionLine.objects.filter(session=session).exclude(state=SessionLine.State.PENDING).count()
                return JsonResponse({'success': True, 'state': line.state, 'done': done, 'total': total})
            return redirect('session_detail', pk=session.pk)

        if action == 'promote':
            line = get_object_or_404(SessionLine, pk=request.POST.get('line_id'), session=session)
            ticket = lifecycle_service.promote_to_ticket(
                line, description=request.POST.get('description', ''), created_by=request.user,
            )
            if is_ajax:
                return JsonResponse({
                    'success': True,
                    'ticket_url': reverse('ticket_detail', args=[ticket.pk]),
                    'ticket_title': ticket.title,
                })
            messages.success(request, f'Promoted to ticket: {ticket.title}')
            return redirect('session_detail', pk=session.pk)

        if action == 'submit':
            # Deliberately no completeness gate — a session submitted with
            # pending lines is allowed, and explicit rather than silent (see
            # the build brief: "submitting with pending lines should be
            # possible but explicit — a session submitted with gaps is data,
            # not an error").
            lifecycle_service.submit_session(session)
            messages.success(request, 'Session submitted.')
            return redirect('my_sessions')

        if action == 'reopen':
            lifecycle_service.reopen_session(session)
            messages.success(request, 'Session reopened.')
            return redirect('session_detail', pk=session.pk)

    done, total = session.progress()
    return render(request, 'sessions/session_detail.html', {
        'session': session,
        'lines': session.lines.all(),
        'done': done,
        'total': total,
        'state_choices': SessionLine.State.choices,
    })


@login_required
def session_template_list(request):
    templates = list(SessionTemplate.objects.select_related('owner__user').order_by('name'))

    # One query for every template's recent sessions instead of one query
    # per template (was a real N+1 — this page lists every template).
    # Fetch-once-then-group-in-Python, same shape (and same reason —
    # portability over a Postgres-only DISTINCT ON or a window-function
    # subquery) as supplies/services.py::_attach_cart_state already uses
    # for an identical "latest N per group" resolution.
    recent_by_template = {}
    sessions = Session.objects.filter(template_id__in=[t.pk for t in templates]).order_by('template_id', '-opens_at')
    for session in sessions:
        bucket = recent_by_template.setdefault(session.template_id, [])
        if len(bucket) < 5:
            bucket.append(session)

    rows = []
    for template in templates:
        rows.append({
            'template': template,
            'recent_sessions': recent_by_template.get(template.pk, []),
            'next_occurrences': generation_service.preview_next_occurrences(template, count=1),
        })
    return render(request, 'sessions/session_template_list.html', {'rows': rows})


@login_required
def session_template_detail(request, pk):
    template = get_object_or_404(SessionTemplate.objects.select_related('owner__user'), pk=pk)
    recent_sessions = template.sessions.order_by('-opens_at')[:20]
    next_occurrences = generation_service.preview_next_occurrences(template, count=5)
    return render(request, 'sessions/session_template_detail.html', {
        'template': template,
        'recent_sessions': recent_sessions,
        'next_occurrences': next_occurrences,
        'static_lines': template.static_lines.all(),
    })


@login_required
def session_template_form_view(request, pk=None):
    template = get_object_or_404(SessionTemplate, pk=pk) if pk else None
    if request.method == 'POST':
        form = SessionTemplateForm(request.POST, instance=template)
        if form.is_valid():
            saved = form.save()
            messages.success(request, f'Saved "{saved.name}".')
            return redirect('session_template_detail', saved.pk)
    else:
        form = SessionTemplateForm(instance=template)

    return render(request, 'sessions/session_template_form.html', {
        'form': form,
        'template': template,
        'attributes': PropertyAttribute.objects.order_by('category', 'label'),
        'staff': StaffProfile.objects.select_related('user').order_by('user__first_name', 'user__username'),
    })


@login_required
def session_template_preview(request):
    """Live preview for Phase 3's setup UI — pure computation off whatever
    the form currently holds (including unsaved edits), never a DB write.
    Returns the actual consequence of the in-progress configuration: the
    next few occurrence dates, and, for a query-driven template, the real
    list of matching property names — "This will create a session on Sep 1
    with 14 lines — Sawgrass Pointe, ..." rather than an abstract rule
    description."""
    frequency = request.POST.get('frequency', '')
    next_open_date_raw = request.POST.get('next_open_date', '')
    workday_of_month_raw = request.POST.get('workday_of_month', '')
    active_from_raw = request.POST.get('active_from', '')
    active_until_raw = request.POST.get('active_until', '')
    line_source = request.POST.get('line_source', SessionTemplate.LineSource.STATIC)
    property_types = request.POST.getlist('property_types')
    required_attribute_ids = [v for v in request.POST.getlist('required_attributes') if v]
    query_by_unit = request.POST.get('query_by_unit') == 'on'

    def _parse_date(raw):
        try:
            return date.fromisoformat(raw) if raw else None
        except ValueError:
            return None

    stand_in = SessionTemplate(
        frequency=frequency,
        next_open_date=_parse_date(next_open_date_raw) or timezone.localdate(),
        workday_of_month=int(workday_of_month_raw) if workday_of_month_raw.isdigit() else None,
        active_from=_parse_date(active_from_raw),
        active_until=_parse_date(active_until_raw),
        property_types=property_types,
    )
    occurrences = generation_service.preview_next_occurrences(stand_in, count=5)
    payload = {
        'occurrences': [{'date': o['date'].isoformat(), 'label': o['label']} for o in occurrences],
    }

    if line_source == SessionTemplate.LineSource.QUERY:
        from core.models import Property
        qs = Property.objects.filter(is_active=True)
        if property_types:
            qs = qs.filter(property_type__in=property_types)
        for attr_id in required_attribute_ids:
            qs = qs.filter(attribute_assignments__attribute_id=attr_id)
        properties = list(qs.distinct().order_by('name').prefetch_related('units'))
        if query_by_unit:
            labels = []
            for prop in properties:
                units = [u for u in prop.units.all() if u.is_active]
                if units:
                    labels.extend(f'{prop.name} — {u.label}' for u in sorted(units, key=lambda u: u.label))
                else:
                    labels.append(prop.name)
        else:
            labels = [prop.name for prop in properties]
        payload['line_count'] = len(labels)
        payload['line_names'] = labels[:25]
    else:
        static_labels = [l.strip() for l in request.POST.get('static_lines_text', '').split('\n') if l.strip()]
        payload['line_count'] = len(static_labels)
        payload['line_names'] = static_labels[:25]

    return JsonResponse(payload)
