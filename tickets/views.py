from datetime import date, datetime, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from core.google_calendar import get_upcoming_events, is_configured as calendar_is_configured
from core.models import Contact, StaffProfile, property_dropdown_queryset
from messaging.services import send_followup as send_followup_message

from .forms import FollowUpForm, ReassignForm, TicketForm
from .models import FollowUpLog, Ticket, TicketAssignmentLog, TicketContact

OPEN_STATUSES = [
    Ticket.Status.OPEN, Ticket.Status.ASSIGNED, Ticket.Status.IN_PROGRESS, Ticket.Status.BLOCKED,
]

# The two buckets staff actually think in: still-active work, and done work
# kept only for the record. Completed/Verified/Cancelled tickets are noise
# on a day-to-day list — the tickets screen defaults to hiding them (see
# ticket_list below) and only shows them when explicitly asked for.
COMPLETE_STATUSES = [Ticket.Status.COMPLETED, Ticket.Status.VERIFIED, Ticket.Status.CANCELLED]

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
    overdue_first = 0 if (ticket.due_date and ticket.due_date < now) else 1
    priority_rank = PRIORITY_RANK.get(ticket.priority, 2)
    due = ticket.due_date or datetime.max.replace(tzinfo=timezone.get_current_timezone())
    return (overdue_first, priority_rank, due)


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
            'overdue_count': sum(1 for t in role_tickets if t.due_date and t.due_date < now),
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
    looking at). Each of Tickets/Tasks is further split into what's due
    today (including anything with no due date yet — untriaged isn't
    "safely later"), what's coming in the next couple days, and a
    collapsed count of everything further out.
    """
    if role not in StaffProfile.Role.values:
        raise Http404
    now = timezone.now()
    today = timezone.localdate()
    soon_cutoff = today + timedelta(days=2)

    qs = (
        Ticket.objects.filter(assigned_role=role, status__in=OPEN_STATUSES, property__isnull=False)
        .select_related('property', 'assigned_staff__user', 'assigned_contact', 'created_from_template')
    )

    today_tickets, soon_tickets = [], []
    today_tasks, soon_tasks = [], []
    later_ticket_count = later_task_count = 0
    for t in qs:
        is_task = t.source == Ticket.Source.RECURRING
        today_bucket = today_tasks if is_task else today_tickets
        soon_bucket = soon_tasks if is_task else soon_tickets

        if t.due_date:
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
            # No due date yet isn't "safely later" — it means nobody's
            # triaged it, which belongs on today's radar, not buried.
            today_bucket.append(t)

    for bucket in (today_tickets, soon_tickets, today_tasks, soon_tasks):
        bucket.sort(key=lambda t: _ticket_urgency_key(t, now))

    staff_profile = getattr(request.user, 'staff_profile', None)
    calendar_token = getattr(staff_profile, 'google_calendar_token', None) if staff_profile else None
    calendar_events = _format_calendar_events(get_upcoming_events(calendar_token)) if calendar_token else []

    return render(request, 'tickets/department_dashboard.html', {
        'role': role,
        'role_label': dict(StaffProfile.Role.choices).get(role),
        'today_tickets': today_tickets,
        'soon_tickets': soon_tickets,
        'later_ticket_count': later_ticket_count,
        'ticket_total': len(today_tickets) + len(soon_tickets) + later_ticket_count,
        'today_tasks': today_tasks,
        'soon_tasks': soon_tasks,
        'later_task_count': later_task_count,
        'task_total': len(today_tasks) + len(soon_tasks) + later_task_count,
        'role_list_url': f"{reverse('ticket_list')}?role={role}",
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
    return render(request, 'tickets/ticket_list.html', {
        'tickets': qs,
        'now': timezone.now(),
        'status_choices': Ticket.Status.choices,
        'role_choices': StaffProfile.Role.choices,
        'selected_status': status,
        'selected_role': role,
        'selected_role_label': dict(StaffProfile.Role.choices).get(role) if role else None,
        'staff_list': StaffProfile.objects.select_related('user'),
        'vendor_list': Contact.objects.filter(contact_type=Contact.ContactType.VENDOR),
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
    """Inline due-date edit from the tickets list."""
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
    return _list_redirect(request)


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


@login_required
def ticket_detail(request, pk):
    ticket = get_object_or_404(
        Ticket.objects.select_related('property', 'assigned_staff__user', 'assigned_contact'), pk=pk,
    )
    reassign_form = ReassignForm(initial={
        'assigned_role': ticket.assigned_role,
        'assigned_staff': ticket.assigned_staff_id,
        'assigned_contact': ticket.assigned_contact_id,
    })
    followup_form = FollowUpForm()
    return render(request, 'tickets/ticket_detail.html', {
        'ticket': ticket,
        'reassign_form': reassign_form,
        'followup_form': followup_form,
        'attachments': ticket.attachments.all().order_by('-created_at'),
        'ticket_contacts': ticket.ticket_contacts.select_related('contact').all(),
        'assignment_logs': ticket.assignment_logs.all()[:10],
        'followups': ticket.followups.all()[:10],
        'vendor_link': request.build_absolute_uri(
            f'/vendor/t/{ticket.completion_token}/'
        ) if ticket.assigned_contact_id else None,
        'status_choices': Ticket.Status.choices,
        'properties': property_dropdown_queryset(),
    })


@login_required
def ticket_create(request):
    if request.method == 'POST':
        form = TicketForm(request.POST)
        if form.is_valid():
            ticket = form.save(commit=False)
            ticket.source = Ticket.Source.MANUAL
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
    return render(request, 'tickets/ticket_form.html', {'form': form})


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
    ticket = get_object_or_404(Ticket, pk=pk)
    if request.method == 'POST':
        property_id = request.POST.get('property_id')
        if property_id:
            ticket.property_id = property_id
            ticket.save(update_fields=['property'])
            messages.success(request, f'Property set to {ticket.property.name} — moved into the {ticket.get_assigned_role_display() if ticket.assigned_role else "unassigned"} queue.')
    if request.POST.get('next') == 'pending':
        return redirect('ticket_pending')
    return redirect('ticket_detail', pk=ticket.pk)


@login_required
def ticket_set_status(request, pk):
    ticket = get_object_or_404(Ticket, pk=pk)
    if request.method == 'POST':
        new_status = request.POST.get('status')
        if new_status in Ticket.Status.values:
            ticket.status = new_status
            if new_status == Ticket.Status.COMPLETED:
                ticket.completed_at = timezone.now()
            if new_status == Ticket.Status.CANCELLED:
                ticket.cancelled_at = timezone.now()
                ticket.cancelled_reason = request.POST.get('cancelled_reason', '')
            resolution_notes = request.POST.get('resolution_notes')
            if resolution_notes:
                ticket.resolution_notes = resolution_notes
            ticket.save()
            messages.success(request, f'Status updated to {ticket.get_status_display()}.')
    if 'next_qs' in request.POST:
        return _list_redirect(request)
    return redirect('ticket_detail', pk=ticket.pk)


@login_required
def ticket_followup(request, pk):
    ticket = get_object_or_404(Ticket, pk=pk)
    if request.method == 'POST':
        form = FollowUpForm(request.POST)
        if form.is_valid():
            log = send_followup_message(
                ticket, form.cleaned_data['channel'],
                to_override=form.cleaned_data.get('to_override') or None,
                user=request.user,
            )
            if log.success:
                messages.success(request, f'Follow-up sent via {log.get_channel_display()} to {log.sent_to}.')
            else:
                messages.error(request, f'Follow-up failed: {log.error_message}')
    return redirect('ticket_detail', pk=ticket.pk)
