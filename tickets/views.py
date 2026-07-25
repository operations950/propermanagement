import json
import zlib
from datetime import date, datetime, timedelta
from itertools import groupby

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.management import call_command
from django.db.models import Count, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from core.google_calendar import get_upcoming_events, is_configured as calendar_is_configured
from core.models import (
    Contact, Property, PropertyAttribute, StaffProfile, is_valid_phone, properties_by_type,
    property_dropdown_queryset,
)
from messaging.services import _followup_result_message, _group_followups, fetch_quo_conversation, send_followup_bulk

from .forms import ReassignForm, TicketForm, TicketTemplateForm
from .models import (
    FollowUpLog, TaskPackageTemplate, Ticket, TicketAssignmentLog, TicketChecklistItem, TicketContact,
    TicketTemplate,
)
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
    """Sort key for a department dashboard's Today list — plain urgency
    order. A ticket closed via "Close No Follow-Up" is struck through in
    place (client-side, see _dashboard_item.html's fetch handler) without
    moving or re-sorting; it drops out of every bucket entirely on the
    next full page load since department_dashboard's query only ever
    includes OPEN_STATUSES."""
    return _ticket_urgency_key(ticket, now)


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

    gmail_inbox_token = None
    if request.user.is_superuser:
        from intake.models import GmailInboxToken
        gmail_inbox_token = GmailInboxToken.objects.first()

    return render(request, 'tickets/dashboard.html', {
        'boxes': boxes,
        'now': now,
        'pending_property_count': pending_property_count,
        'no_role_count': no_role_count,
        'awaiting_verification': awaiting_verification,
        'gmail_inbox_token': gmail_inbox_token,
    })


TIMELINE_PX_PER_HOUR = 40

# Per-calendar dashboard shading (see _calendar_color) — deliberately mixes
# the brand's blue/slate ramp with --accent-contrast (the one non-blue/gray
# hue in the palette, added specifically so multiple pulled-in calendars
# can look genuinely different from each other, not just lighter/darker
# versions of the same blue) plus light/dark color-mix variants of both, so
# a handful of calendars each get a visually distinct shade.
CALENDAR_COLOR_ROTATION = [
    'var(--brand-primary)',
    'var(--accent-contrast)',
    'var(--brand-slate)',
    'color-mix(in srgb, var(--accent-contrast) 55%, white)',
    'color-mix(in srgb, var(--brand-primary) 55%, black)',
    'color-mix(in srgb, var(--accent-contrast) 55%, black)',
]


def _calendar_color(calendar_id):
    """Stable (same calendar always gets the same shade across page loads)
    but otherwise arbitrary assignment from CALENDAR_COLOR_ROTATION."""
    idx = zlib.crc32((calendar_id or '').encode()) % len(CALENDAR_COLOR_ROTATION)
    return CALENDAR_COLOR_ROTATION[idx]


def _hour_label(hour):
    hour = hour % 24
    hour12 = hour % 12 or 12
    return f"{hour12} {'AM' if hour < 12 else 'PM'}"


def _layout_timeline(timed_events):
    """Positions a day's timed events on a Google-Calendar-day-view-style
    hour grid: top/height as percentages of the visible hour range,
    side-by-side columns for events that overlap in time (a simple
    greedy column-packing sweep — good enough for a personal calendar's
    handful of same-day meetings, not trying to match Google's own
    optimal-width algorithm). Returns (events_with_position, hours,
    height_px, now_top_pct)."""
    if not timed_events:
        range_start, range_end = 8, 18
    else:
        earliest = min(t['start'].hour for t in timed_events)
        latest = max(t['end'].hour + (1 if t['end'].minute else 0) for t in timed_events)
        range_start = min(8, earliest)
        range_end = max(18, latest)
    range_start = max(0, range_start)
    range_end = min(24, max(range_end, range_start + 1))
    total_minutes = (range_end - range_start) * 60

    columns = []
    for ev in timed_events:
        placed = False
        for col in columns:
            if col[-1]['end'] <= ev['start']:
                col.append(ev)
                ev['_col'] = columns.index(col)
                placed = True
                break
        if not placed:
            ev['_col'] = len(columns)
            columns.append([ev])
    total_cols = len(columns) or 1

    for ev in timed_events:
        start_min = max(0, (ev['start'].hour * 60 + ev['start'].minute) - range_start * 60)
        end_min = (ev['end'].hour * 60 + ev['end'].minute) - range_start * 60
        end_min = max(end_min, start_min + 20)  # minimum visible height for very short events
        ev['top_pct'] = round(start_min / total_minutes * 100, 2)
        ev['height_pct'] = round(min(total_minutes, end_min - start_min) / total_minutes * 100, 2)
        ev['left_pct'] = round(ev['_col'] / total_cols * 100, 2)
        ev['width_pct'] = round(100 / total_cols - 1, 2)

    hours = [
        {'label': _hour_label(h), 'top_pct': round((h - range_start) * 60 / total_minutes * 100, 2)}
        for h in range(range_start, range_end + 1)
    ]

    now = timezone.localtime(timezone.now())
    now_min = now.hour * 60 + now.minute - range_start * 60
    now_top_pct = round(now_min / total_minutes * 100, 2) if 0 <= now_min <= total_minutes else None

    return timed_events, hours, (range_end - range_start) * TIMELINE_PX_PER_HOUR, now_top_pct


def _format_calendar_events(events, days_ahead=2):
    """Google Calendar API event dicts -> one box per day (today plus
    `days_ahead` more), each split into all-day events (shown first,
    every day) and timed events. Today additionally gets a Google-
    Calendar-day-view-style hour timeline (see _layout_timeline); the
    other days are just a simple chronological list — see
    _dashboard_calendar.html."""
    today = timezone.localdate()
    days = [today + timedelta(days=i) for i in range(days_ahead + 1)]
    by_day = {d: {'all_day': [], 'timed': []} for d in days}

    for e in events:
        title = e.get('summary') or '(no title)'
        color = _calendar_color(e.get('_calendar_id'))
        start = e.get('start', {})
        end = e.get('end', {})
        if 'date' in start:
            start_date = date.fromisoformat(start['date'])
            end_date = date.fromisoformat(end['date']) if end.get('date') else start_date
            for d in days:
                if start_date <= d < end_date:
                    by_day[d]['all_day'].append({'title': title, 'color': color})
            continue

        start_dt = parse_datetime(start.get('dateTime', ''))
        if not start_dt:
            continue
        if timezone.is_naive(start_dt):
            start_dt = timezone.make_aware(start_dt)
        start_dt = timezone.localtime(start_dt)

        end_dt = parse_datetime(end.get('dateTime', '')) or start_dt
        if timezone.is_naive(end_dt):
            end_dt = timezone.make_aware(end_dt)
        end_dt = timezone.localtime(end_dt)

        d = start_dt.date()
        if d not in by_day:
            continue
        by_day[d]['timed'].append({
            'title': title, 'start': start_dt, 'end': end_dt, 'color': color,
            'start_label': start_dt.strftime('%I:%M %p').lstrip('0'),
            'end_label': end_dt.strftime('%I:%M %p').lstrip('0'),
        })

    day_boxes = []
    for i, d in enumerate(days):
        timed = sorted(by_day[d]['timed'], key=lambda t: t['start'])
        if i == 0:
            timed, hours, height_px, now_top_pct = _layout_timeline(timed)
        else:
            hours, height_px, now_top_pct = None, None, None
        day_boxes.append({
            'date': d,
            'label': 'Today' if i == 0 else ('Tomorrow' if i == 1 else d.strftime('%A')),
            'date_label': f'{d.strftime("%b")} {d.day}',
            'is_today': i == 0,
            'all_day': by_day[d]['all_day'],
            'timed': timed,
            'timeline_hours': hours,
            'timeline_height_px': height_px,
            'now_top_pct': now_top_pct,
        })
    return day_boxes


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
    - Today: due today or overdue. Closing one via "Close No Follow-Up"
      strikes it through in place client-side (see _dashboard_item.html)
      for immediate confirmation, but it never reappears on a fresh load
      of this page — the query only ever pulls OPEN_STATUSES.
    - Next 2 days, and a collapsed count of everything further out.
    """
    if role not in StaffProfile.Role.values:
        raise Http404
    now = timezone.now()
    today = timezone.localdate()
    soon_cutoff = today + timedelta(days=2)

    qs = (
        Ticket.objects.filter(assigned_role=role, property__isnull=False, status__in=OPEN_STATUSES)
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
            needs_date_bucket.append(t)

    for bucket in (today_tickets, today_tasks):
        bucket.sort(key=lambda t: _daily_checklist_key(t, now))
    for bucket in (soon_tickets, soon_tasks):
        bucket.sort(key=lambda t: _ticket_urgency_key(t, now))
    for bucket in (needs_date_tickets, needs_date_tasks):
        bucket.sort(key=lambda t: (PRIORITY_RANK.get(t.priority, 2), t.title))

    staff_profile = getattr(request.user, 'staff_profile', None)
    calendar_token = getattr(staff_profile, 'google_calendar_token', None) if staff_profile else None
    calendar_days = []
    available_calendars = []
    enabled_calendar_ids = ''
    if calendar_token:
        raw_events, available_calendars = get_upcoming_events(calendar_token)
        calendar_days = _format_calendar_events(raw_events)
        primary_id = next((c['id'] for c in available_calendars if c['is_primary']), 'primary')
        enabled_ids = [c for c in (calendar_token.enabled_calendar_ids or [primary_id])]
        for c in available_calendars:
            c['color'] = _calendar_color(c['id'])
        enabled_calendar_ids = ','.join(enabled_ids)

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
        'calendar_days': calendar_days,
        'available_calendars': available_calendars,
        'enabled_calendar_ids': enabled_calendar_ids,
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
        'tickets': tickets, 'properties_by_type': properties_by_type(), 'now': timezone.now(),
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

    template_id = request.GET.get('template')
    selected_template = None
    if template_id:
        qs = qs.filter(created_from_template_id=template_id)
        selected_template = TicketTemplate.objects.filter(pk=template_id).first()

    scheduled_for = parse_date(request.GET.get('scheduled_for', '') or '')
    if scheduled_for:
        qs = qs.filter(scheduled_for=scheduled_for)

    property_id = request.GET.get('property')
    selected_property = None
    if property_id:
        qs = qs.filter(property_id=property_id)
        selected_property = Property.objects.filter(pk=property_id).first()

    return render(request, 'tickets/ticket_list.html', {
        'tickets': qs,
        'now': timezone.now(),
        'status_choices': Ticket.Status.choices,
        'role_choices': StaffProfile.Role.choices,
        'selected_status': status,
        'selected_role': role,
        'selected_role_label': dict(StaffProfile.Role.choices).get(role) if role else None,
        'selected_source': source,
        'selected_template_id': template_id,
        'selected_template': selected_template,
        'selected_scheduled_for': scheduled_for,
        'selected_property': selected_property,
        'staff_list': StaffProfile.objects.select_related('user'),
        'vendor_list': Contact.objects.filter(contact_type=Contact.ContactType.VENDOR),
        'properties_by_type': properties_by_type(),
    })


def _list_redirect(request):
    """Send the browser back to the tickets list, preserving whatever
    status/role filter it was viewing (see the hidden `next_qs` field each
    inline-edit row-form carries) instead of always resetting to 'All'."""
    qs = request.POST.get('next_qs', '')
    url = reverse('ticket_list')
    return redirect(f'{url}?{qs}' if qs else url)


@login_required
def ticket_quick_edit(request, pk):
    """The tickets list's single-pencil, whole-row editor — one combined
    save for title/property/due date/department/status/assignee at once,
    replacing the old one-endpoint-per-field inline-edit pencils (which
    are now dead: ticket_set_title/_department/_assignee are gone, this
    is their sole successor). ticket_set_property/_status/_due_date stay
    — they're also used by ticket_detail.html and the department
    dashboard respectively."""
    ticket = get_object_or_404(Ticket, pk=pk)
    if request.method == 'POST':
        title = request.POST.get('title', '').strip()
        if title:
            ticket.title = title

        property_id = request.POST.get('property_id')
        ticket.property_id = property_id or None

        raw_due = request.POST.get('due_date', '').strip()
        parsed_due = parse_date(raw_due) if raw_due else None
        ticket.due_date = (
            timezone.make_aware(datetime.combine(parsed_due, datetime.min.time())) if parsed_due else None
        )

        role = request.POST.get('assigned_role', '')
        if role == '' or role in StaffProfile.Role.values:
            ticket.assigned_role = role

        status = request.POST.get('status')
        if status in Ticket.Status.values and status != Ticket.Status.COMPLETED:
            ticket.status = status

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

        ticket.full_clean()
        ticket.save()
        messages.success(request, 'Ticket updated.')
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


def _related_contact_pools(ticket, linked_ticket_contacts):
    """Per-column suggested bubbles for Related contacts' Owner /
    Contractor / Additional columns — contacts of the matching type
    already linked to this ticket's property, or to any property of the
    same type (Contractors also include vendors with no property link at
    all, since most serve many properties rather than being tied to one).
    Whatever's already linked under that role is folded in too even if it
    wouldn't otherwise qualify, so the bubble picker always has something
    to find-and-lock on load — see bubble-picker.js's rehydration."""
    linked_by_role = {}
    for tc in linked_ticket_contacts:
        linked_by_role.setdefault(tc.role, []).append(tc.contact)

    if ticket.property_id:
        same_type_ids = Property.objects.filter(
            property_type=ticket.property.property_type,
        ).values_list('pk', flat=True)
        property_filter = Q(properties__in=same_type_ids)
    else:
        property_filter = Q(pk__in=[])  # no property context — suggest nothing, search still works

    def _column(type_filter, role, also_unlinked=False):
        filt = property_filter | Q(properties__isnull=True) if also_unlinked else property_filter
        pool = {c.pk: c for c in Contact.objects.filter(type_filter).filter(filt).distinct()}
        for c in linked_by_role.get(role, []):
            pool[c.pk] = c
        return sorted(pool.values(), key=lambda c: c.name)

    owner_contacts = _column(
        Q(contact_type__in=[
            Contact.ContactType.OWNER, Contact.ContactType.BOARD_MEMBER,
            Contact.ContactType.ASSOCIATION_MEMBER, Contact.ContactType.TENANT,
        ]),
        TicketContact.Role.OWNER,
    )
    contractor_contacts = _column(
        Q(contact_type=Contact.ContactType.VENDOR), TicketContact.Role.CONTRACTOR, also_unlinked=True,
    )
    # Whoever's assigned via Reassign is clearly the contractor on this job
    # — surface them here too (one click to also track them as a related
    # contact) even if they wouldn't otherwise match the type/property rule.
    if ticket.assigned_contact_id and ticket.assigned_contact_id not in {c.pk for c in contractor_contacts}:
        contractor_contacts = sorted(contractor_contacts + [ticket.assigned_contact], key=lambda c: c.name)
    additional_contacts = _column(
        ~Q(contact_type__in=[Contact.ContactType.OWNER, Contact.ContactType.VENDOR]), TicketContact.Role.OTHER,
    )

    def _ids(role):
        return ','.join(str(c.pk) for c in linked_by_role.get(role, []))

    return {
        'owner_contacts': owner_contacts, 'owner_ids': _ids(TicketContact.Role.OWNER),
        'contractor_contacts': contractor_contacts, 'contractor_ids': _ids(TicketContact.Role.CONTRACTOR),
        'additional_contacts': additional_contacts, 'additional_ids': _ids(TicketContact.Role.OTHER),
    }


def _parse_quo_timestamp(iso_str):
    try:
        dt = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        return None
    return timezone.localtime(dt) if timezone.is_aware(dt) else timezone.make_aware(dt)


def _relevant_message_ids(ticket, messages):
    """Which of this bound conversation's QuoMessages Claude judges
    on-topic for THIS ticket — a Quo conversation is per contact, not per
    issue, so one contractor's thread can carry several concurrent or
    long-past jobs mixed together (see intake/relevance_classifier.py).
    Returns None (never hide anything) when there's too little history to
    bother, the API key isn't configured, or the call fails. Cached per
    (ticket, latest message id) so the 20s Contractor Communication poll
    doesn't re-run Claude on every tick — only when a new message arrives."""
    from intake.relevance_classifier import MIN_MESSAGES, classify_message_relevance

    if len(messages) < MIN_MESSAGES:
        return None

    from django.core.cache import cache

    cache_key = f'ticket_msg_relevance:{ticket.pk}:{messages[-1].pk}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    verdict = classify_message_relevance(ticket, messages)
    if verdict is None:
        return None
    cache.set(cache_key, verdict.related_ids, 60 * 30)
    return verdict.related_ids


def _contractor_thread(ticket):
    """Chronological, merged view of Quo messages with this ticket's
    assigned contact plus this app's own logged SMS sends to them, for the
    Contractor Communication card. None if no contact is assigned (the
    card doesn't render at all then). has_quo_thread distinguishes "no
    Quo conversation has ever been linked to this contact's phone" from
    "linked, but no messages yet" — different empty-state copy.

    Once a ticket is bound to a specific conversation (Ticket.source_reference
    — set the first time a message sends/arrives for it, see
    messaging.services.send_via_quo and intake/views.py's webhook handler),
    this reads straight from the local QuoMessage table the webhook keeps
    live — instant, no Quo API call, and correctly scoped to *this*
    conversation even if the contact has others going. Before that binding
    exists yet, falls back to the older live-fetch-by-phone-number path."""
    contact = ticket.assigned_contact
    if not contact:
        return None

    entries = []
    if ticket.source_reference:
        # Every message sent/received once a ticket is bound already lands
        # in QuoMessage (send_via_quo echoes our own sends immediately; the
        # webhook writes replies) — the FollowUpLog SMS rows below would
        # double-count our own sends (FollowUpLog is written unconditionally
        # as the audit trail, same as QuoMessage), so skip them here.
        from intake.models import QuoMessage

        has_quo_thread = True
        messages_qs = list(
            QuoMessage.objects.filter(conversation_id=ticket.source_reference).order_by('quo_created_at')
        )
        related_ids = _relevant_message_ids(ticket, messages_qs)
        for m in messages_qs:
            if m.quo_created_at:
                entries.append({
                    'direction': m.direction, 'body': m.body, 'at': timezone.localtime(m.quo_created_at),
                    'related': True if related_ids is None else (m.pk in related_ids),
                })
    else:
        quo_messages = fetch_quo_conversation(contact)
        has_quo_thread = quo_messages is not None
        for m in (quo_messages or []):
            at = _parse_quo_timestamp(m.get('at', ''))
            if at:
                entries.append({'direction': m['direction'], 'body': m['body'], 'at': at, 'related': True})

        # Not bound yet — QuoMessage has nothing for this contact, so the
        # only record of our own sends is the FollowUpLog audit trail.
        for log in ticket.followups.filter(contact=contact, channel=FollowUpLog.Channel.SMS):
            entries.append({'direction': 'out', 'body': log.body, 'at': timezone.localtime(log.sent_at), 'related': True})

    entries.sort(key=lambda e: e['at'])
    return {'entries': entries, 'has_quo_thread': has_quo_thread}


@login_required
def ticket_contractor_thread_refresh(request, pk):
    """Polled by ticket_detail.html every 20s while the page is open (see
    its script block) so a webhook-delivered reply shows up without a full
    reload — cheap now that _contractor_thread reads from our own
    webhook-populated QuoMessage table once a ticket is bound to a
    conversation, rather than a live Quo API call every tick."""
    ticket = get_object_or_404(Ticket, pk=pk)
    thread = _contractor_thread(ticket)
    if thread is None:
        return HttpResponse('')
    return render(request, 'tickets/_contractor_thread_entries.html', thread)


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
    linked_ticket_contacts = list(ticket.ticket_contacts.select_related('contact').all())
    contact_pools = _related_contact_pools(ticket, linked_ticket_contacts)

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
        'ticket_contacts': linked_ticket_contacts,
        'owner_contacts': contact_pools['owner_contacts'],
        'owner_ids': contact_pools['owner_ids'],
        'contractor_contacts': contact_pools['contractor_contacts'],
        'contractor_ids': contact_pools['contractor_ids'],
        'additional_contacts': contact_pools['additional_contacts'],
        'additional_ids': contact_pools['additional_ids'],
        'owner_contacts_json': json.dumps([
            {'id': c.id, 'label': str(c)} for c in Contact.objects.filter(contact_type__in=[
                Contact.ContactType.OWNER, Contact.ContactType.BOARD_MEMBER,
                Contact.ContactType.ASSOCIATION_MEMBER, Contact.ContactType.TENANT,
            ])
        ]),
        'contractor_search_json': json.dumps([
            {'id': c.id, 'label': str(c)} for c in Contact.objects.filter(contact_type=Contact.ContactType.VENDOR)
        ]),
        'additional_contacts_json': json.dumps([
            {'id': c.id, 'label': str(c)}
            for c in Contact.objects.exclude(contact_type__in=[Contact.ContactType.OWNER, Contact.ContactType.VENDOR])
        ]),
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
        'reason_required_statuses': Ticket.REASON_REQUIRED_STATUSES,
        # Completed is a hard status, deliberately excluded from the casual
        # bubble picker — the "Mark Complete" button below is the one path
        # to it. Still included when the ticket is *already* completed, so
        # the bubble correctly rehydrates and displays that current value
        # instead of the picker misleadingly showing "Choose a status".
        'status_bubble_choices': [
            (v, l) for v, l in Ticket.Status.choices
            if v != Ticket.Status.COMPLETED or ticket.status == Ticket.Status.COMPLETED
        ],
        'properties_by_type': properties_by_type(),
        'vendor_contacts_json': json.dumps([
            {'id': c.id, 'label': str(c)} for c in Contact.objects.filter(contact_type=Contact.ContactType.VENDOR)
        ]),
        'selected_contractor_label': str(ticket.assigned_contact) if ticket.assigned_contact_id else '',
        'contractor_thread': _contractor_thread(ticket),
        'quo_default_from_number': settings.QUO_DEFAULT_FROM_NUMBER,
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


@login_required
def ticket_create(request):
    if request.method == 'POST':
        data = request.POST.copy()
        # "Add new" on the Contractor/Reporter ghost-text filter fields
        # submits alongside the ticket on the same POST (no separate
        # request/AJAX in this app) — create the Contact first, then feed
        # its id into the real field the rest of TicketForm expects.
        phone_error = False
        for role, default_type in (('contractor', Contact.ContactType.VENDOR), ('reporter', None)):
            name = data.get(f'new_contact__name__{role}', '').strip()
            if name:
                phone = data.get(f'new_contact__phone__{role}', '').strip()
                if not is_valid_phone(phone):
                    messages.error(request, 'Phone must be in XXX-XXX-XXXX format — nothing was saved.')
                    phone_error = True
                    continue
                contact, _ = Contact.objects.get_or_create(
                    name=name,
                    phone=phone,
                    email=data.get(f'new_contact__email__{role}', '').strip(),
                    defaults={
                        'contact_type': default_type or Contact.ContactType.OTHER,
                        'trade': data.get(f'new_contact__trade__{role}', '').strip(),
                    },
                )
                data['assigned_contact' if role == 'contractor' else 'reporter_contact'] = str(contact.pk)

        form = TicketForm(data)
        if not phone_error and form.is_valid():
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
        'properties_by_type': properties_by_type(),
        'vendor_contacts_json': json.dumps(vendor_contacts),
        'all_contacts_json': json.dumps(all_contacts),
        'selected_contractor_label': contact_label('assigned_contact'),
        'selected_reporter_label': contact_label('reporter_contact'),
    })


def _attributes_by_category():
    """PropertyAttribute.objects, grouped for the New Recurring Task
    screen's Required attributes bubble pool — mirrors the Staff/Vendors
    labeled-section split on ticket_list's Assignee picker. Relies on
    PropertyAttribute.Meta.ordering (category, then label) already
    sorting the queryset the way groupby needs."""
    attrs = PropertyAttribute.objects.filter(is_active=True)
    category_labels = dict(PropertyAttribute.Category.choices)
    return [
        {'category_label': category_labels[category], 'attributes': list(group)}
        for category, group in groupby(attrs, key=lambda a: a.category)
    ]


def _rule_target_contacts_json():
    return json.dumps([
        {'id': c.id, 'label': str(c)} for c in Contact.objects.filter(
            contact_type__in=[
                Contact.ContactType.OWNER, Contact.ContactType.BOARD_MEMBER, Contact.ContactType.ASSOCIATION_MEMBER,
            ],
        )
    ])


def _ticket_template_form_context(form, today):
    return {
        'form': form,
        'today': today.isoformat(),
        'due_date_presets': _due_date_presets(today),
        'properties_by_type': properties_by_type(),
        'attributes_by_category': _attributes_by_category(),
        'target_type_choices': TicketTemplate.TargetType.choices,
        'rule_target_contacts_json': _rule_target_contacts_json(),
    }


@login_required
def ticket_template_create(request):
    today = timezone.localdate()
    if request.method == 'POST':
        form = TicketTemplateForm(request.POST)
        if form.is_valid():
            template = form.save()
            # The scheduler only runs generate_recurring_tickets every
            # RECURRING_TICKET_INTERVAL_MINUTES (default 30) — without this,
            # a template due today wouldn't produce a visible ticket for up
            # to half an hour. Idempotent (get_or_create per occurrence), so
            # running it here doesn't risk double-generating anything, for
            # this template or any other.
            call_command('generate_recurring_tickets')
            messages.success(request, f'Recurring task rule "{template.title}" created.')
            return redirect('ticket_template_detail', pk=template.pk)
    else:
        # A plain ISO string, not a date object — {{ }} auto-formats a raw
        # date/datetime object into a locale-formatted string ("July 23,
        # 2026"), which would silently break the hidden bubble-input's
        # value match against the ISO-stringed date_presets below.
        form = TicketTemplateForm(initial={'next_run_date': today.isoformat()})

    return render(request, 'tickets/ticket_template_form.html', _ticket_template_form_context(form, today))


@login_required
def ticket_template_edit(request, pk):
    template = get_object_or_404(TicketTemplate, pk=pk)
    today = timezone.localdate()
    if request.method == 'POST':
        form = TicketTemplateForm(request.POST, instance=template)
        if form.is_valid():
            template = form.save()
            call_command('generate_recurring_tickets')
            messages.success(request, f'Recurring task rule "{template.title}" saved.')
            return redirect('ticket_template_detail', pk=template.pk)
    else:
        form = TicketTemplateForm(instance=template, initial={'next_run_date': template.next_run_date.isoformat()})

    context = _ticket_template_form_context(form, today)
    context['template'] = template
    return render(request, 'tickets/ticket_template_form.html', context)


_TARGET_TYPE_ORDER = ['company', 'every_property', 'property_category', 'contact', 'property']


def _target_summary(template):
    """One-line "applies to" description for a Task Rule row — the display
    counterpart to TicketTemplate.TargetType dispatch in applicability.py."""
    if template.target_type == TicketTemplate.TargetType.COMPANY:
        return 'Company-wide'
    if template.target_type == TicketTemplate.TargetType.PROPERTY:
        return template.property.name if template.property_id else 'No property set'
    if template.target_type == TicketTemplate.TargetType.CONTACT:
        return f'Properties linked to {template.contact}' if template.contact_id else 'No contact set'
    if template.target_type == TicketTemplate.TargetType.PROPERTY_CATEGORY:
        type_labels = dict(Property.Type.choices)
        labels = [type_labels.get(t, t) for t in template.property_types]
        return ', '.join(labels) if labels else 'Every property'
    return 'Every property'


@login_required
def ticket_template_list(request):
    """The main-nav "Recurring Tasks" landing page — every active/inactive
    Task Rule, grouped by target_type (broadest-impact rules first, since
    those are the ones worth double-checking at a glance), each linking
    through to the Tasks/Task Groups it has generated."""
    templates = list(
        TicketTemplate.objects.select_related('property', 'contact', 'default_assigned_staff')
        .prefetch_related('required_attributes').order_by('title')
    )
    for t in templates:
        t.target_summary = _target_summary(t)
    target_labels = dict(TicketTemplate.TargetType.choices)
    groups = []
    for target_type in _TARGET_TYPE_ORDER:
        group_templates = [t for t in templates if t.target_type == target_type]
        if group_templates:
            groups.append({
                'target_type': target_type, 'label': target_labels[target_type], 'templates': group_templates,
            })
    return render(request, 'tickets/ticket_template_list.html', {'groups': groups})


@login_required
def ticket_template_detail(request, pk):
    template = get_object_or_404(
        TicketTemplate.objects.select_related('property', 'contact', 'default_assigned_staff'), pk=pk,
    )
    template.target_summary = _target_summary(template)
    occurrences = (
        template.occurrences.order_by('-scheduled_for')
        .annotate(ticket_count=Count('tickets'), done_count=Count('tickets', filter=Q(
            tickets__status__in=Ticket.DEPENDENCY_SATISFYING_STATUSES,
        )))[:26]
    )
    return render(request, 'tickets/ticket_template_detail.html', {
        'template': template,
        'occurrences': occurrences,
        'all_tasks_url': f"{reverse('ticket_list')}?source=recurring&template={template.pk}",
    })


@login_required
def ticket_reassign(request, pk):
    ticket = get_object_or_404(Ticket, pk=pk)
    if request.method == 'POST':
        data = request.POST.copy()
        # Same inline add-new-contact pattern as ticket_create's contractor
        # field — the Reassign form's ghost-text contact filter shares the
        # exact same markup/JS, which unconditionally renders an "add new"
        # row, so this needs to actually work rather than silently no-op.
        name = data.get('new_contact__name__contractor', '').strip()
        phone_error = False
        if name:
            phone = data.get('new_contact__phone__contractor', '').strip()
            if not is_valid_phone(phone):
                messages.error(request, 'Phone must be in XXX-XXX-XXXX format — nothing was reassigned.')
                phone_error = True
            else:
                contact, _ = Contact.objects.get_or_create(
                    name=name,
                    phone=phone,
                    email=data.get('new_contact__email__contractor', '').strip(),
                    defaults={
                        'contact_type': Contact.ContactType.VENDOR,
                        'trade': data.get('new_contact__trade__contractor', '').strip(),
                    },
                )
                data['assigned_contact'] = str(contact.pk)
        form = ReassignForm(data)
        if not phone_error and form.is_valid():
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
def ticket_set_contacts(request, pk):
    """The 3-column Related contacts picker's auto-save — every bubble
    lock/unlock in any of the Owner/Contractor/Additional columns submits
    this form immediately (see the page-local script in ticket_detail.html),
    so there's no separate Save button. Each column is synced independently
    to TicketContact links under its own role (add missing, remove absent
    — Contact.properties' lock-to-add/unlock-to-remove convention, just
    three of them side by side), and each column's inline add-new-contact
    sub-form is handled the same way ticket_create's contractor/reporter
    fields are."""
    ticket = get_object_or_404(Ticket, pk=pk)
    if request.method == 'POST':
        data = request.POST.copy()
        columns = (
            ('owner', TicketContact.Role.OWNER, Contact.ContactType.OWNER),
            ('contractor', TicketContact.Role.CONTRACTOR, Contact.ContactType.VENDOR),
            ('additional', TicketContact.Role.OTHER, Contact.ContactType.OTHER),
        )
        phone_error = False
        for prefix, role, default_type in columns:
            name = data.get(f'new_contact__name__{prefix}', '').strip()
            if name:
                phone = data.get(f'new_contact__phone__{prefix}', '').strip()
                if not is_valid_phone(phone):
                    messages.error(request, 'Phone must be in XXX-XXX-XXXX format — nothing was saved.')
                    phone_error = True
                    continue
                contact, _ = Contact.objects.get_or_create(
                    name=name, phone=phone,
                    email=data.get(f'new_contact__email__{prefix}', '').strip(),
                    defaults={'contact_type': default_type},
                )
                data.setlist(f'{prefix}_contact_ids', data.getlist(f'{prefix}_contact_ids') + [str(contact.pk)])

        if not phone_error:
            for prefix, role, _default_type in columns:
                contact_ids = {int(v) for v in data.getlist(f'{prefix}_contact_ids') if v.isdigit()}
                existing = {tc.contact_id: tc for tc in ticket.ticket_contacts.filter(role=role)}
                for contact_id, tc in existing.items():
                    if contact_id not in contact_ids:
                        tc.delete()
                for contact_id in contact_ids:
                    if contact_id not in existing:
                        TicketContact.objects.get_or_create(ticket=ticket, contact_id=contact_id, role=role)
    return redirect('ticket_detail', pk=ticket.pk)


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

            new_due_date = None
            if new_status == Ticket.Status.DEFERRED:
                raw_due_date = request.POST.get('new_due_date', '').strip()
                parsed_due_date = parse_date(raw_due_date) if raw_due_date else None
                if not parsed_due_date:
                    messages.error(request, 'Deferred needs a new due date — nothing was changed.')
                    if 'next_qs' in request.POST:
                        return _list_redirect(request)
                    return redirect('ticket_detail', pk=ticket.pk)
                new_due_date = timezone.make_aware(datetime.combine(parsed_due_date, datetime.min.time()))

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
            if new_status == Ticket.Status.DEFERRED:
                ticket.due_date = new_due_date
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
    action — completes a ticket without messaging the reporter. The
    dashboard's fetch handler strikes the row through in place on success
    (see _dashboard_item.html) rather than reloading — department_dashboard's
    query only pulls OPEN_STATUSES, so a full page reload naturally drops
    it instead of requiring special same-day-visibility handling."""
    ticket = get_object_or_404(Ticket, pk=pk)
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    if request.method == 'POST':
        ticket.status = Ticket.Status.COMPLETED
        ticket.completed_at = timezone.now()
        ticket.save()
        if is_ajax:
            return JsonResponse({'success': True})
    if ticket.assigned_role in StaffProfile.Role.values:
        return redirect('department_dashboard', role=ticket.assigned_role)
    return redirect('dashboard')


@login_required
def ticket_followup_sms(request, pk):
    ticket = get_object_or_404(Ticket, pk=pk)
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    if request.method == 'POST':
        contact_ids = request.POST.getlist('contact_ids')
        body = request.POST.get('body', '').strip()
        if contact_ids and body:
            logs = send_followup_bulk(
                FollowUpLog.Channel.SMS, contact_ids, body, ticket=ticket, user=request.user,
            )
            if is_ajax:
                # Contractor Communication's compose box: the new bubble in
                # the thread is itself the confirmation the user asked for —
                # no page reload, no top banner. Errors still need to reach
                # the caller, just as JSON instead of a messages-framework
                # banner.
                ok = any(log.success for log in logs)
                return JsonResponse({
                    'success': ok,
                    'error': '' if ok else 'Send failed — check the recipient\'s phone number.',
                })
            _followup_result_message(request, logs, 'recipient(s) by text')
        elif is_ajax:
            return JsonResponse({'success': False, 'error': 'Write a message first.'})
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
                FollowUpLog.Channel.EMAIL, contact_ids, body, ticket=ticket, subject=subject,
                group=group, user=request.user,
            )
            _followup_result_message(request, logs, 'recipient(s) by email')
        else:
            messages.error(request, 'Choose at least one recipient and write a message first.')
    return redirect('ticket_detail', pk=ticket.pk)
