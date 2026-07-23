import json
from datetime import date, datetime, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from core.google_calendar import get_upcoming_events, is_configured as calendar_is_configured
from core.models import Contact, Property, StaffProfile, property_dropdown_queryset
from messaging.services import send_followup_bulk

from .forms import ReassignForm, TicketForm
from .models import FollowUpLog, TaskPackageTemplate, Ticket, TicketAssignmentLog, TicketChecklistItem, TicketContact
from .services.package_engine import unblock_dependents

OPEN_STATUSES = [
    Ticket.Status.OPEN, Ticket.Status.ASSIGNED, Ticket.Status.IN_PROGRESS, Ticket.Status.BLOCKED,
    Ticket.Status.UPCOMING, Ticket.Status.DEFERRED,
]

# The two buckets staff actually think in: still-active work, and done work
# kept only for the record. Completed/Verified/Cancelled/Skipped/Not-applicable
# tickets are noise on a day-to-day list — the tickets screen defaults to
# hiding them (see ticket_list below) and only shows them when explicitly asked for.
COMPLETE_STATUSES = [
    Ticket.Status.COMPLETED, Ticket.Status.VERIFIED, Ticket.Status.CANCELLED,
    Ticket.Status.SKIPPED, Ticket.Status.NOT_APPLICABLE,
]

# Fixed display order for the dashboard's role boxes — matches how the
# business actually thinks about who owns what, not alphabetical/model order.
DASHBOARD_ROLE_ORDER = [
    StaffProfile.Role.PROPERTY_MANAGER,
    StaffProfile.Role.ADMIN,
    StaffProfile.Role.CLEANER,
    StaffProfile.Role.MAINTENANCE,
    StaffProfile.Role.ACCOUNTING,
    StaffProfile.Role.CONTRACTOR,
]

PRIORITY_RANK = {'urgent': 0, 'high': 1, 'medium': 2, 'low': 3}
BOX_PREVIEW_SIZE = 5


def _ticket_urgency_key(ticket, now):
    is_overdue = ticket.due_date and timezone.localtime(ticket.due_date).date() < timezone.localtime(now).date()
    overdue_first = 0 if is_overdue else 1
    priority_rank = PRIORITY_RANK.get(ticket.priority, 2)
    due = ticket.due_date or datetime.max.replace(tzinfo=timezone.get_current_timezone())
    return (overdue_first, priority_rank, due)


def _daily_checklist_key(ticket, now):
    """Sort key for a department dashboard's Today list: anything just
    closed via "Close No Follow-Up" today sinks to the bottom (with
    strikethrough — see _dashboard_item.html) instead of competing with
    still-open work on urgency."""
    closed = 1 if ticket.status == Ticket.Status.COMPLETED else 0
    return (closed,) + _ticket_urgency_key(ticket, now)


@login_required
def dashboard(request):
    now = timezone.now()
    # A ticket only enters a role's queue once it has a property — see
    # ticket_pending for the triage screen where property-less tickets wait.
    open_tickets = list(
        Ticket.objects.filter(status__in=OPEN_STATUSES, property__isnull=False)
        .select_related('property', 'assigned_staff__user', 'assigned_contact')
    )

    boxes = []
    for role in DASHBOARD_ROLE_ORDER:
        role_tickets = [t for t in open_tickets if t.assigned_role == role]
        role_tickets.sort(key=lambda t: _ticket_urgency_key(t, now))
        boxes.append({
            'role': role,
            'label': dict(StaffProfile.Role.choices)[role],
            'top': role_tickets[:BOX_PREVIEW_SIZE],
            'total': len(role_tickets),
            'overdue_count': sum(
                1 for t in role_tickets
                if t.due_date and timezone.localtime(t.due_date).date() < timezone.localtime(now).date()
            ),
        })

    pending_property_count = (
        Ticket.objects.filter(property__isnull=True).exclude(status=Ticket.Status.CANCELLED).count()
    )
    no_role_count = sum(1 for t in open_tickets if not t.assigned_role)
    awaiting_verification = Ticket.objects.filter(status=Ticket.Status.COMPLETED).select_related('property')

    return render(request, 'tickets/dashboard.html', {
        'boxes': boxes,
        'now': now,
        'pending_property_count': pending_property_count,
        'no_role_count': no_role_count,
        'awaiting_verification': awaiting_verification,
    })


def _format_calendar_events(events):
    """Google Calendar API event dicts -> simple display-ready rows."""
    today = timezone.localdate()
    rows = []
    for e in events:
        start = e.get('start', {})
        if 'dateTime' in start:
            dt = parse_datetime(start['dateTime'])
            if dt and timezone.is_naive(dt):
                dt = timezone.make_aware(dt)
            day = timezone.localtime(dt).date() if dt else None
            label = timezone.localtime(dt).strftime('%I:%M %p').lstrip('0') if dt else ''
        else:
            day = date.fromisoformat(start['date']) if start.get('date') else None
            label = 'All day'
        rows.append({
            'title': e.get('summary') or '(no title)',
            'label': label,
            'day': day,
            'is_today': day == today,
        })
    return rows


@login_required
def department_dashboard(request, role):
    """A department's own front page, split into the three things staff
    actually distinguish: reactive Tickets, generated proactive Tasks
    (source == recurring — otherwise identical Ticket rows), and the
    logged-in viewer's own Google Calendar (about their day, not the
    team's, so it's the same regardless of which department they're
    looking at).

    Each of Tickets/Tasks is split into three groups:
    - Needs a due date: nobody's triaged these yet, so they're not
      "Today's" work until someone assigns one — shown first, as a
      to-do, not folded into Today where they'd get lost among real
      due-today items.
    - Today: due today or overdue, PLUS anything closed today via
      "Close No Follow-Up" (kept visible with strikethrough, sorted to
      the bottom, as same-day done-confirmation — see
      _daily_checklist_key/_dashboard_item.html).
    - Next 2 days, and a collapsed count of everything further out.
    """
    if role not in StaffProfile.Role.values:
        raise Http404
    now = timezone.now()
    today = timezone.localdate()
    soon_cutoff = today + timedelta(days=2)

    qs = (
        Ticket.objects.filter(assigned_role=role, property__isnull=False)
        .filter(Q(status__in=OPEN_STATUSES) | Q(status=Ticket.Status.COMPLETED, completed_at__date=today))
        .select_related('property', 'assigned_staff__user', 'assigned_contact', 'created_from_template')
        .prefetch_related('checklist_items')
    )

    needs_date_tickets, needs_date_tasks = [], []
    today_tickets, soon_tickets = [], []
    today_tasks, soon_tasks = [], []
    later_ticket_count = later_task_count = 0
    for t in qs:
        is_task = t.source == Ticket.Source.RECURRING
        today_bucket = today_tasks if is_task else today_tickets
        soon_bucket = soon_tasks if is_task else soon_tickets
        needs_date_bucket = needs_date_tasks if is_task else needs_date_tickets

        if t.status == Ticket.Status.COMPLETED:
            # Closed today via "Close No Follow-Up" — stays on today's
            # list (struck through, sorted last) rather than vanishing.
            today_bucket.append(t)
        elif t.due_date:
            d = timezone.localtime(t.due_date).date()
            if d <= today:
                today_bucket.append(t)
            elif d <= soon_cutoff:
                soon_bucket.append(t)
            elif is_task:
                later_task_count += 1
            else:
                later_ticket_count += 1
        else:
            needs_date_bucket.append(t)

    for bucket in (today_tickets, today_tasks):
        bucket.sort(key=lambda t: _daily_checklist_key(t, now))
    for bucket in (soon_tickets, soon_tasks):
        bucket.sort(key=lambda t: _ticket_urgency_key(t, now))
    for bucket in (needs_date_tickets, needs_date_tasks):
        bucket.sort(key=lambda t: (PRIORITY_RANK.get(t.priority, 2), t.title))

    staff_profile = getattr(request.user, 'staff_profile', None)
    calendar_token = getattr(staff_profile, 'google_calendar_token', None) if staff_profile else None
    calendar_events = _format_calendar_events(get_upcoming_events(calendar_token)) if calendar_token else []

    return render(request, 'tickets/department_dashboard.html', {
        'role': role,
        'role_label': dict(StaffProfile.Role.choices).get(role),
        'needs_date_tickets': needs_date_tickets,
        'needs_date_tasks': needs_date_tasks,
        'today_tickets': today_tickets,
        'soon_tickets': soon_tickets,
        'later_ticket_count': later_ticket_count,
        'ticket_total': len(needs_date_tickets) + len(today_tickets) + len(soon_tickets) + later_ticket_count,
        'today_tasks': today_tasks,
        'soon_tasks': soon_tasks,
        'later_task_count': later_task_count,
        'task_total': len(needs_date_tasks) + len(today_tasks) + len(soon_tasks) + later_task_count,
        'ticket_list_url': f"{reverse('ticket_list')}?role={role}&source=reactive",
        'task_list_url': f"{reverse('ticket_list')}?role={role}&source=recurring",
        'now': now,
        'calendar_configured': calendar_is_configured(),
        'calendar_token': calendar_token,
        'calendar_events': calendar_events,
    })


@login_required
def ticket_pending(request):
    """Tickets with no property yet — held here instead of any role's queue
    until a property is assigned, since the source (usually Quo) couldn't
    tell which property the request was about."""
    tickets = (
        Ticket.objects.filter(property__isnull=True).exclude(status=Ticket.Status.CANCELLED)
        .select_related('assigned_staff__user', 'assigned_contact').order_by('-created_at')
    )
    return render(request, 'tickets/pending.html', {
        'tickets': tickets, 'properties': property_dropdown_queryset(), 'now': timezone.now(),
    })


@login_required
def ticket_pending_save(request, pk):
    """Pending items are unconfirmed candidates, not finished tickets yet —
    this is where staff clean up the description and either assign it a
    property (which moves it into its department's real queue) or leave
    the property blank to keep refining it later."""
    ticket = get_object_or_404(Ticket, pk=pk, property__isnull=True)
    if request.method == 'POST':
        ticket.description = request.POST.get('description', '').strip()
        property_id = request.POST.get('property_id')
        if property_id:
            ticket.property_id = property_id
        ticket.save()
        if property_id:
            messages.success(request, f'Saved and moved to {ticket.property.name}.')
        else:
            messages.success(request, 'Saved.')
    return redirect('ticket_pending')


@login_required
def ticket_pending_delete(request, pk):
    """Not every reactive-intake candidate deserves to be a ticket — this
    lets staff discard noise/false-positives outright rather than being
    forced to assign it a property just to make it go away. Scoped to
    still-pending items only; once something's a real, queued ticket it
    should be cancelled (with a reason, kept for the record) rather than
    deleted."""
    ticket = get_object_or_404(Ticket, pk=pk, property__isnull=True)
    if request.method == 'POST':
        title = ticket.title
        ticket.delete()
        messages.success(request, f'Deleted "{title}".')
    return redirect('ticket_pending')


@login_required
def ticket_list(request):
    """Defaults to the active bucket (open/assigned/in_progress/blocked) —
    completed/verified/cancelled tickets are only noise day-to-day, so they
    stay hidden unless staff explicitly ask for them via the status filter
    ('complete' for the whole historical bucket, or a specific status like
    'cancelled' to drill into just one)."""
    qs = Ticket.objects.select_related('property', 'assigned_staff__user', 'assigned_contact').all()
    status = request.GET.get('status') or 'active'
    if status == 'active':
        qs = qs.filter(status__in=OPEN_STATUSES)
    elif status == 'complete':
        qs = qs.filter(status__in=COMPLETE_STATUSES)
    elif status == 'all':
        pass
    elif status in Ticket.Status.values:
        qs = qs.filter(status=status)
    else:
        status = 'active'
        qs = qs.filter(status__in=OPEN_STATUSES)
    role = request.GET.get('role')
    if role == 'none':
        qs = qs.filter(assigned_role='')
    elif role:
        qs = qs.filter(assigned_role=role)

    # A recurring task isn't just a ticket — the main-menu "Tickets" link only
    # ever shows one-off/reactive rows, "Recurring Tasks" only shows
    # source=recurring ones. A bookmarked/plain /tickets/ URL with no source
    # param still shows everything, for anyone filtering by department/status
    # across both kinds at once.
    source = request.GET.get('source', '')
    if source == 'reactive':
        qs = qs.exclude(source=Ticket.Source.RECURRING)
    elif source == 'recurring':
        qs = qs.filter(source=Ticket.Source.RECURRING)

    return render(request, 'tickets/ticket_list.html', {
        'tickets': qs,
        'now': timezone.now(),
        'status_choices': Ticket.Status.choices,
        'role_choices': StaffProfile.Role.choices,
        'selected_status': status,
        'selected_role': role,
        'selected_role_label': dict(StaffProfile.Role.choices).get(role) if role else None,
        'selected_source': source,
        'staff_list': StaffProfile.objects.select_related('user'),
        'vendor_list': Contact.objects.filter(contact_type=Contact.ContactType.VENDOR),
        'properties': property_dropdown_queryset(),
    })


def _list_redirect(request):
    """Send the browser back to the tickets list, preserving whatever
    status/role filter it was viewing (see the hidden `next_qs` field each
    inline-edit row-form carries) instead of always resetting to 'All'."""
    qs = request.POST.get('next_qs', '')
    url = reverse('ticket_list')
    return redirect(f'{url}?{qs}' if qs else url)


@login_required
def ticket_set_department(request, pk):
    """Inline department edit from the tickets list."""
    ticket = get_object_or_404(Ticket, pk=pk)
    if request.method == 'POST':
        role = request.POST.get('assigned_role', '')
        if role == '' or role in StaffProfile.Role.values:
            ticket.assigned_role = role
            ticket.save(update_fields=['assigned_role'])
    return _list_redirect(request)


@login_required
def ticket_set_assignee(request, pk):
    """Inline assignee edit from the tickets list — a single dropdown mixing
    staff and vendor contacts, submitted as e.g. 'staff-3' / 'contact-7'."""
    ticket = get_object_or_404(Ticket, pk=pk)
    if request.method == 'POST':
        kind, _, raw_id = request.POST.get('assignee', '').partition('-')
        if kind == 'staff' and raw_id.isdigit():
            ticket.assigned_staff_id = int(raw_id)
            ticket.assigned_contact = None
        elif kind == 'contact' and raw_id.isdigit():
            ticket.assigned_contact_id = int(raw_id)
            ticket.assigned_staff = None
        else:
            ticket.assigned_staff = None
            ticket.assigned_contact = None
        if ticket.status == Ticket.Status.OPEN and (ticket.assigned_staff_id or ticket.assigned_contact_id):
            ticket.status = Ticket.Status.ASSIGNED
        ticket.save()
    return _list_redirect(request)


@login_required
def ticket_set_due_date(request, pk):
    """Inline due-date edit — from the tickets list (next_qs present) or
    from a department dashboard's "needs a due date" box (next_role
    present, since that's not a ticket_list request at all)."""
    ticket = get_object_or_404(Ticket, pk=pk)
    if request.method == 'POST':
        raw = request.POST.get('due_date', '')
        if raw:
            parsed = parse_date(raw)
            if parsed:
                ticket.due_date = timezone.make_aware(datetime.combine(parsed, datetime.min.time()))
        else:
            ticket.due_date = None
        ticket.save(update_fields=['due_date'])
    if 'next_qs' in request.POST:
        return _list_redirect(request)
    next_role = request.POST.get('next_role')
    if next_role in StaffProfile.Role.values:
        return redirect('department_dashboard', role=next_role)
    return redirect('dashboard')


@login_required
def ticket_delete(request, pk):
    """Permanently removes a ticket — unlike a status change to Cancelled
    (which keeps the record for the audit trail), this is for genuinely
    wrong/duplicate/junk entries staff want gone entirely."""
    ticket = get_object_or_404(Ticket, pk=pk)
    if request.method == 'POST':
        title = ticket.title
        ticket.delete()
        messages.success(request, f'Permanently deleted "{title}".')
    return _list_redirect(request)


def _followup_parties(ticket):
    """Every real person attached to this ticket, for the Follow-Up modal's
    bubble pools — the reporter/cc/other TicketContact links plus the
    assigned vendor contact if set (a contractor is a party too),
    deduped by contact id."""
    parties = {}
    for tc in ticket.ticket_contacts.select_related('contact').all():
        parties[tc.contact_id] = tc.contact
    if ticket.assigned_contact_id:
        parties[ticket.assigned_contact_id] = ticket.assigned_contact
    return list(parties.values())


def _group_followups(followups):
    """One entry per batch_id (everything created by a single Follow-Up
    "Send" click) — followups is already ordered -sent_at, and every row
    in one batch is created back-to-back in the same request, so rows for
    a batch are always contiguous in that ordering."""
    batches, order = {}, []
    for log in followups:
        if log.batch_id not in batches:
            batches[log.batch_id] = []
            order.append(log.batch_id)
        batches[log.batch_id].append(log)
    result = []
    for batch_id in order:
        logs = batches[batch_id]
        first = logs[0]
        result.append({
            'logs': logs,
            'channel': first.channel,
            'sent_at': first.sent_at,
            'sent_by': first.sent_by,
            'subject': first.subject,
            'body': first.body,
            'is_group': first.is_group,
            'recipients': [log.contact.name if log.contact else log.sent_to for log in logs],
            'all_success': all(log.success for log in logs),
            'any_success': any(log.success for log in logs),
        })
    return result


@login_required
def ticket_detail(request, pk):
    ticket = get_object_or_404(
        Ticket.objects.select_related(
            'property', 'assigned_staff__user', 'assigned_contact', 'created_from_template',
            'template_occurrence', 'package_run__package',
        ),
        pk=pk,
    )
    reassign_form = ReassignForm(initial={
        'assigned_role': ticket.assigned_role,
        'assigned_staff': ticket.assigned_staff_id,
        'assigned_contact': ticket.assigned_contact_id,
    })
    followup_parties = _followup_parties(ticket)

    package_siblings = []
    blocking_step_label = ''
    if ticket.package_run_id:
        package_siblings = list(
            ticket.package_run.tickets.select_related('property', 'created_from_template')
            .order_by('created_from_template__title')
        )
        if ticket.status == Ticket.Status.BLOCKED and ticket.created_from_template_id:
            this_step = TaskPackageTemplate.objects.filter(
                package=ticket.package_run.package_id, template=ticket.created_from_template_id,
            ).select_related('depends_on__template').first()
            if this_step and this_step.depends_on_id:
                blocking_step_label = this_step.depends_on.template.title

    occurrence_siblings = []
    if ticket.template_occurrence_id:
        occurrence_siblings = list(
            ticket.template_occurrence.tickets.select_related('property').order_by('property__name')
        )

    can_approve = bool(
        ticket.created_from_template_id and ticket.created_from_template.requires_approval
        and getattr(getattr(request.user, 'staff_profile', None), 'role', None)
        == ticket.created_from_template.approval_role
    )

    return render(request, 'tickets/ticket_detail.html', {
        'ticket': ticket,
        'reassign_form': reassign_form,
        'followup_text_parties': [c for c in followup_parties if c.phone],
        'followup_email_parties': [c for c in followup_parties if c.email],
        'attachments': ticket.attachments.all().order_by('-created_at'),
        'ticket_contacts': ticket.ticket_contacts.select_related('contact').all(),
        'assignment_logs': ticket.assignment_logs.all()[:10],
        'followup_batches': _group_followups(ticket.followups.select_related('contact')[:30]),
        'checklist_items': ticket.checklist_items.all(),
        'package_siblings': package_siblings,
        'blocking_step_label': blocking_step_label,
        'occurrence_siblings': occurrence_siblings,
        'can_approve': can_approve,
        'vendor_link': request.build_absolute_uri(
            f'/vendor/t/{ticket.completion_token}/'
        ) if ticket.assigned_contact_id else None,
        'status_choices': Ticket.Status.choices,
        'properties': property_dropdown_queryset(),
        'now': timezone.now(),
    })


def _due_date_presets(today):
    """Concrete (label, ISO date) pairs for the New Ticket due-date bubbles
    — computed server-side off the business's local calendar day so no
    client-side date math (and no naive-UTC timezone bug) is needed at
    all; the "Custom" bubble is the only one requiring any JS."""
    presets = [('Today', 0), ('Tomorrow', 1)]
    presets += [(f'{n} days', n) for n in (3, 4, 5, 6)]
    presets += [('1 week', 7), ('2 weeks', 14), ('1 month', 30)]
    return [{'label': label, 'value': (today + timedelta(days=n)).isoformat()} for label, n in presets]


def _properties_by_type():
    """Property picker data for the New Ticket bubble UI, grouped by type in
    the same order as property_dropdown_queryset(). Each type also carries a
    city breakdown for the (currently dormant, given real property counts
    all being well under 50) capacity-aware drill-down: a type's properties
    only get grouped by city once there are more than 50 of them, and a
    city only gets a text filter once IT has more than 50."""
    buckets = {}
    for p in property_dropdown_queryset():
        buckets.setdefault(p.property_type, []).append(p)

    result = []
    for value, label in Property.Type.choices:
        props = buckets.get(value, [])
        entry = {'type_key': value, 'type_label': label, 'needs_city_tier': len(props) > 50}
        if entry['needs_city_tier']:
            city_buckets = {}
            for p in props:
                city_buckets.setdefault(p.city or 'Unspecified', []).append(p)
            entry['cities'] = [
                {
                    'city': city,
                    'properties': [{'id': p.id, 'name': p.name} for p in city_props],
                    'needs_filter': len(city_props) > 50,
                }
                for city, city_props in sorted(city_buckets.items())
            ]
        else:
            entry['properties'] = [{'id': p.id, 'name': p.name} for p in props]
        result.append(entry)
    return result


@login_required
def ticket_create(request):
    if request.method == 'POST':
        data = request.POST.copy()
        # "Add new" on the Contractor/Reporter ghost-text filter fields
        # submits alongside the ticket on the same POST (no separate
        # request/AJAX in this app) — create the Contact first, then feed
        # its id into the real field the rest of TicketForm expects.
        for role, default_type in (('contractor', Contact.ContactType.VENDOR), ('reporter', None)):
            name = data.get(f'new_contact__name__{role}', '').strip()
            if name:
                contact, _ = Contact.objects.get_or_create(
                    name=name,
                    phone=data.get(f'new_contact__phone__{role}', '').strip(),
                    email=data.get(f'new_contact__email__{role}', '').strip(),
                    defaults={
                        'contact_type': default_type or Contact.ContactType.OTHER,
                        'trade': data.get(f'new_contact__trade__{role}', '').strip(),
                    },
                )
                data['assigned_contact' if role == 'contractor' else 'reporter_contact'] = str(contact.pk)

        form = TicketForm(data)
        if form.is_valid():
            ticket = form.save(commit=False)
            ticket.source = Ticket.Source.MANUAL
            raw_due_date = form.cleaned_data.get('due_date')
            # due_date is a plain (day-only) DateField on the form — combine
            # to a timezone-aware midnight explicitly rather than relying on
            # DateTimeField's implicit naive-datetime fallback (which warns
            # and is fragile around DST), matching ticket_set_due_date.
            ticket.due_date = (
                timezone.make_aware(datetime.combine(raw_due_date, datetime.min.time()))
                if raw_due_date else None
            )
            if ticket.assigned_staff_id or ticket.assigned_contact_id:
                ticket.status = Ticket.Status.ASSIGNED
            ticket.full_clean()
            ticket.save()
            reporter = form.cleaned_data.get('reporter_contact')
            if reporter:
                TicketContact.objects.get_or_create(
                    ticket=ticket, contact=reporter, role=TicketContact.Role.REPORTER,
                )
            messages.success(request, 'Ticket created.')
            return redirect('ticket_detail', pk=ticket.pk)
    else:
        form = TicketForm()

    vendor_contacts = [
        {'id': c.id, 'label': str(c)}
        for c in Contact.objects.filter(contact_type=Contact.ContactType.VENDOR)
    ]
    all_contacts = [{'id': c.id, 'label': str(c)} for c in Contact.objects.all()]
    today = timezone.localdate()

    def contact_label(field_name):
        # Repopulates the ghost-text filter's visible text (not just its
        # hidden id) on a validation-error re-render — the hidden input
        # already round-trips the id for free via form['...'].value().
        contact_id = form[field_name].value()
        if not contact_id:
            return ''
        try:
            return str(Contact.objects.get(pk=contact_id))
        except (Contact.DoesNotExist, ValueError, TypeError):
            return ''

    return render(request, 'tickets/ticket_form.html', {
        'form': form,
        'today': today.isoformat(),
        'due_date_presets': _due_date_presets(today),
        'properties_by_type': _properties_by_type(),
        'vendor_contacts_json': json.dumps(vendor_contacts),
        'all_contacts_json': json.dumps(all_contacts),
        'selected_contractor_label': contact_label('assigned_contact'),
        'selected_reporter_label': contact_label('reporter_contact'),
    })


@login_required
def ticket_reassign(request, pk):
    ticket = get_object_or_404(Ticket, pk=pk)
    if request.method == 'POST':
        form = ReassignForm(request.POST)
        if form.is_valid():
            TicketAssignmentLog.objects.create(
                ticket=ticket,
                from_staff=ticket.assigned_staff, from_contact=ticket.assigned_contact,
                to_staff=form.cleaned_data.get('assigned_staff'),
                to_contact=form.cleaned_data.get('assigned_contact'),
                changed_by=request.user,
                note=form.cleaned_data.get('note', ''),
            )
            ticket.assigned_staff = form.cleaned_data.get('assigned_staff')
            new_contact = form.cleaned_data.get('assigned_contact')
            if new_contact and new_contact != ticket.assigned_contact:
                ticket.rotate_completion_token()
            ticket.assigned_contact = new_contact
            ticket.assigned_role = form.cleaned_data['assigned_role']
            if ticket.status == Ticket.Status.OPEN:
                ticket.status = Ticket.Status.ASSIGNED
            ticket.full_clean()
            ticket.save()
            messages.success(request, 'Ticket reassigned.')
        else:
            messages.error(request, 'Could not reassign: check the form.')
    return redirect('ticket_detail', pk=ticket.pk)


@login_required
def ticket_set_property(request, pk):
    """Also used as the tickets list's inline Property edit (next_qs
    present) — see _list_redirect. Allows clearing the property back to
    none (moves it back into the pending-triage screen), not just setting
    one, since that's a real inline action once a select is on the list."""
    ticket = get_object_or_404(Ticket, pk=pk)
    if request.method == 'POST':
        property_id = request.POST.get('property_id')
        if property_id:
            ticket.property_id = property_id
            ticket.save(update_fields=['property'])
            messages.success(request, f'Property set to {ticket.property.name} — moved into the {ticket.get_assigned_role_display() if ticket.assigned_role else "unassigned"} queue.')
        elif 'next_qs' in request.POST:
            ticket.property = None
            ticket.save(update_fields=['property'])
    if 'next_qs' in request.POST:
        return _list_redirect(request)
    if request.POST.get('next') == 'pending':
        return redirect('ticket_pending')
    return redirect('ticket_detail', pk=ticket.pk)


@login_required
def ticket_set_title(request, pk):
    """Inline title edit from the tickets list."""
    ticket = get_object_or_404(Ticket, pk=pk)
    if request.method == 'POST':
        title = request.POST.get('title', '').strip()
        if title:
            ticket.title = title
            ticket.save(update_fields=['title'])
    return _list_redirect(request)


@login_required
def ticket_set_status(request, pk):
    ticket = get_object_or_404(Ticket, pk=pk)
    if request.method == 'POST':
        new_status = request.POST.get('status')
        if new_status in Ticket.Status.values:
            status_reason = request.POST.get('status_reason', '').strip()
            if new_status in Ticket.REASON_REQUIRED_STATUSES and not status_reason:
                messages.error(
                    request,
                    f'{dict(Ticket.Status.choices)[new_status]} needs a reason — nothing was changed.',
                )
                if 'next_qs' in request.POST:
                    return _list_redirect(request)
                return redirect('ticket_detail', pk=ticket.pk)

            template = ticket.created_from_template
            if new_status == Ticket.Status.VERIFIED and template and template.requires_approval:
                user_role = getattr(getattr(request.user, 'staff_profile', None), 'role', None)
                if user_role != template.approval_role:
                    messages.error(
                        request,
                        f'Only {dict(StaffProfile.Role.choices).get(template.approval_role, template.approval_role)} '
                        'can approve this — nothing was changed.',
                    )
                    if 'next_qs' in request.POST:
                        return _list_redirect(request)
                    return redirect('ticket_detail', pk=ticket.pk)

            ticket.status = new_status
            ticket.status_reason = status_reason
            if new_status == Ticket.Status.COMPLETED:
                ticket.completed_at = timezone.now()
            if new_status == Ticket.Status.CANCELLED:
                ticket.cancelled_at = timezone.now()
                ticket.cancelled_reason = request.POST.get('cancelled_reason', '')
            resolution_notes = request.POST.get('resolution_notes')
            if resolution_notes:
                ticket.resolution_notes = resolution_notes
            ticket.save()
            if new_status in Ticket.DEPENDENCY_SATISFYING_STATUSES:
                unblock_dependents(ticket)
            messages.success(request, f'Status updated to {ticket.get_status_display()}.')
    if 'next_qs' in request.POST:
        return _list_redirect(request)
    return redirect('ticket_detail', pk=ticket.pk)


@login_required
def ticket_checklist_toggle(request, pk):
    """Toggles one TicketChecklistItem's checked state from the ticket
    detail page's checklist card — self-submitting, onchange="this.form.submit()"."""
    item = get_object_or_404(TicketChecklistItem, pk=pk)
    if request.method == 'POST':
        item.is_checked = not item.is_checked
        item.checked_at = timezone.now() if item.is_checked else None
        item.checked_by = request.user if item.is_checked else None
        item.save(update_fields=['is_checked', 'checked_at', 'checked_by'])
    return redirect('ticket_detail', pk=item.ticket_id)


@login_required
def ticket_close_no_followup(request, pk):
    """The department dashboard's daily-checklist "Close No Follow-Up"
    action — completes a ticket without messaging the reporter. Stays
    visible (struck through, sorted last) in today's list for the rest of
    the day as a done-confirmation — see department_dashboard's query,
    which includes anything completed today regardless of status filter."""
    ticket = get_object_or_404(Ticket, pk=pk)
    if request.method == 'POST':
        ticket.status = Ticket.Status.COMPLETED
        ticket.completed_at = timezone.now()
        ticket.save()
    if ticket.assigned_role in StaffProfile.Role.values:
        return redirect('department_dashboard', role=ticket.assigned_role)
    return redirect('dashboard')


def _followup_result_message(request, logs, recipient_noun):
    succeeded = sum(1 for log in logs if log.success)
    failed = len(logs) - succeeded
    if not logs:
        messages.error(request, 'Nothing sent — no eligible recipient was selected.')
    elif failed == 0:
        messages.success(request, f'Sent to {succeeded} {recipient_noun}.')
    elif succeeded == 0:
        messages.error(request, f'Failed to send to all {failed} {recipient_noun}.')
    else:
        messages.warning(request, f'Sent to {succeeded} {recipient_noun}, failed for {failed}.')


@login_required
def ticket_followup_sms(request, pk):
    ticket = get_object_or_404(Ticket, pk=pk)
    if request.method == 'POST':
        contact_ids = request.POST.getlist('contact_ids')
        body = request.POST.get('body', '').strip()
        if contact_ids and body:
            logs = send_followup_bulk(
                ticket, FollowUpLog.Channel.SMS, contact_ids, body, user=request.user,
            )
            _followup_result_message(request, logs, 'recipient(s) by text')
        else:
            messages.error(request, 'Choose at least one recipient and write a message first.')
    return redirect('ticket_detail', pk=ticket.pk)


@login_required
def ticket_followup_email(request, pk):
    ticket = get_object_or_404(Ticket, pk=pk)
    if request.method == 'POST':
        contact_ids = request.POST.getlist('contact_ids')
        subject = request.POST.get('subject', '').strip()
        body = request.POST.get('body', '').strip()
        group = request.POST.get('group') == '1'
        if contact_ids and body:
            logs = send_followup_bulk(
                ticket, FollowUpLog.Channel.EMAIL, contact_ids, body, subject=subject,
                group=group, user=request.user,
            )
            _followup_result_message(request, logs, 'recipient(s) by email')
        else:
            messages.error(request, 'Choose at least one recipient and write a message first.')
    return redirect('ticket_detail', pk=ticket.pk)
