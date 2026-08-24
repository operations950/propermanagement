import json
import zlib
from urllib.parse import urlsplit
from datetime import date, datetime, timedelta
from itertools import groupby

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.management import call_command
from django.db.models import Count, F, Max, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateformat import format as format_date
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.dateparse import parse_date, parse_datetime

from core.google_calendar import get_upcoming_events, is_configured as calendar_is_configured
from core.models import (
    Contact, Property, PropertyAttribute, StaffProfile, Unit, group_vendors_by_trade, is_valid_phone,
    properties_by_type, property_dropdown_queryset,
)
from messaging.services import _followup_result_message, _group_followups, fetch_quo_conversation, send_followup_bulk
from processes.forms import ProcessAttachmentUploadForm
from processes.models import ProcessTemplate

from .forms import AssignContractorForm, FunctionForm, ReassignForm, TaskGroupForm, TicketForm, TicketTemplateForm
from .models import (
    FollowUpLog, Priority, TaskGroup, TaskPackage, TaskPackageTemplate, TemplateChecklistItem, Ticket,
    TicketAssignmentLog, TicketAttachment, TicketChecklistItem, TicketClosingNote, TicketContact, TicketStatusNote,
    TicketTemplate,
    TicketTemplateDocument, TicketView,
)
from .services import owner_dashboard as owner_dashboard_queries
from .services.package_engine import unblock_dependents
from .services.process_gate import incomplete_process_instances, process_gate_error_message

OPEN_STATUSES = [
    Ticket.Status.OPEN, Ticket.Status.ASSIGNED, Ticket.Status.IN_PROGRESS, Ticket.Status.BLOCKED,
    Ticket.Status.UPCOMING, Ticket.Status.DEFERRED, Ticket.Status.VENDOR_COMPLETE,
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


def _apply_due_date_change(ticket, new_due_date):
    """Sets ticket.due_date and updates the delayed/previous_due_date pair
    to match — shared by ticket_set_due_date and the Deferred status
    change (see ticket_set_status), since both are "the due date got
    pushed" from the user's point of view and should flag the same way.
    Does NOT save() — caller decides which fields to persist."""
    old_due_date = ticket.due_date
    ticket.due_date = new_due_date
    if old_due_date and new_due_date and new_due_date > old_due_date:
        ticket.delayed = True
        ticket.previous_due_date = old_due_date
    elif ticket.delayed and new_due_date and ticket.previous_due_date and new_due_date <= ticket.previous_due_date:
        ticket.delayed = False
        ticket.previous_due_date = None


def _daily_checklist_key(ticket, now):
    """Sort key for a department dashboard's Today list — plain urgency
    order. A ticket closed via "Close No Follow-Up" is struck through in
    place (client-side, see _dashboard_item.html's fetch handler) without
    moving or re-sorting; it drops out of every bucket entirely on the
    next full page load since department_dashboard's query only ever
    includes OPEN_STATUSES."""
    return _ticket_urgency_key(ticket, now)


class _TaskGroupRow:
    """A dashboard-only stand-in for one PackageRun's sibling step tickets —
    a "Task Group" per the Function/Task Group/Task model (see TaskPackage's
    docstring). Exposes just enough of a Ticket's interface (.due_date,
    .priority, .status, .delayed) that the existing urgency-sort key
    functions and is_overdue/is_due_today template filters work on it
    unchanged, so it can sit in the same sorted bucket as ungrouped Tickets
    and collapse to one row in _dashboard_item.html with its real steps
    nested inside, instead of every step cluttering the dashboard as its
    own line (see #11's "single line item as a container" ask)."""
    is_task_group = True
    source = Ticket.Source.RECURRING
    status = ''  # never 'completed' — the header's own badges read the aggregates below, not this
    delayed = False

    def __init__(self, run, members):
        members = sorted(members, key=lambda m: m.title)
        self.pk = f'run-{run.pk}'
        self.run = run
        self.members = members
        self.title = run.package.title
        self.property = run.property
        self.due_date = members[0].due_date
        self.priority = min(members, key=lambda m: PRIORITY_RANK.get(m.priority, 2)).priority
        self.delayed = any(m.delayed for m in members)
        self.done_count = sum(1 for m in members if m.status in Ticket.DEPENDENCY_SATISFYING_STATUSES)
        self.total_count = len(members)


def _group_task_rows(tickets, sort_key):
    """Collapses tickets sharing a package_run into one _TaskGroupRow, then
    sorts the mixed (group + ungrouped-ticket) list with `sort_key` — the
    same _daily_checklist_key/_ticket_urgency_key/needs-date key already
    used for plain tickets, since _TaskGroupRow exposes the same
    .due_date/.priority attributes those read."""
    solo, by_run = [], {}
    for t in tickets:
        if t.package_run_id:
            by_run.setdefault(t.package_run_id, []).append(t)
        else:
            solo.append(t)
    rows = solo + [_TaskGroupRow(members[0].package_run, members) for members in by_run.values()]
    rows.sort(key=sort_key)
    return rows


def _department_boxes(open_tickets, now):
    """The 6 role queues, each with its top preview rows and counts — the
    core of the standard dashboard, also reused as-is (without the ticket
    previews) for the Departments box on the Owner Dashboard."""
    boxes = []
    for role in DASHBOARD_ROLE_ORDER:
        role_tickets = [t for t in open_tickets if t.assigned_role == role]
        # Due-date-only ordering (nulls last) — pairs with the due_urgency_style
        # fade in dashboard.html, so the most pressing items are both first
        # in the list and least faded, trailing off together.
        role_tickets.sort(key=lambda t: t.due_date or datetime.max.replace(tzinfo=timezone.get_current_timezone()))
        boxes.append({
            'role': role,
            'label': dict(StaffProfile.Role.choices)[role],
            'top': role_tickets[:BOX_PREVIEW_SIZE],
            'total': len(role_tickets),
            'overdue_count': sum(
                1 for t in role_tickets
                if t.due_date and timezone.localtime(t.due_date).date() < timezone.localtime(now).date()
            ),
            'delayed_count': sum(1 for t in role_tickets if t.delayed),
        })
    return boxes


@login_required
def dashboard(request):
    staff_profile = getattr(request.user, 'staff_profile', None)
    if staff_profile and staff_profile.is_company_admin:
        return _owner_dashboard(request)

    now = timezone.now()
    # A ticket only enters a role's queue once it has a property — reactive
    # intake (the only source that ever left one without a property) is
    # gone, so in practice every manually/recurring-created ticket already
    # has one.
    open_tickets = list(
        Ticket.objects.filter(status__in=OPEN_STATUSES, property__isnull=False)
        .select_related('property', 'assigned_staff__user', 'assigned_contact')
    )

    boxes = _department_boxes(open_tickets, now)
    no_role_count = sum(1 for t in open_tickets if not t.assigned_role)
    awaiting_verification = Ticket.objects.filter(status=Ticket.Status.COMPLETED).select_related('property')

    return render(request, 'tickets/dashboard.html', {
        'boxes': boxes,
        'now': now,
        'no_role_count': no_role_count,
        'awaiting_verification': awaiting_verification,
    })


def _owner_dashboard(request):
    """The Company Admin dashboard — see dashboard()'s branch above.
    Rebuilt around exceptions and recent activity rather than totals and
    percentages: a count you can't act on is noise, a list you can click
    into is signal. Three time orientations, one panel each: reactive
    tickets look backward (off_track_tickets), on-site work looks forward
    (onsite_next_48h), recurring looks at drift at the RULE level, not the
    instance (session_templates_drifting — the sessions app fully replaced
    the old TicketTemplate-based recurring system, see the "Recurring work
    overhaul — sessions" build brief) — plus departments/calendar (kept
    close to what existed), today's movement, and a never-urgent "gone
    quiet" panel last. All five panel queries live in
    tickets/services/owner_dashboard.py, not inline here — see that
    module for why each is shaped the way it is."""
    now = timezone.now()

    open_tickets = list(
        Ticket.objects.filter(status__in=OPEN_STATUSES, property__isnull=False)
        .select_related('property', 'assigned_staff__user', 'assigned_contact')
    )
    department_boxes = _department_boxes(open_tickets, now)

    off_track = owner_dashboard_queries.off_track_tickets(now)
    onsite = owner_dashboard_queries.onsite_next_48h(now)
    session_drift = owner_dashboard_queries.session_templates_drifting()
    movement = owner_dashboard_queries.movement_today()
    quiet = owner_dashboard_queries.gone_quiet(now)

    staff_profile = request.user.staff_profile
    calendar_token = getattr(staff_profile, 'google_calendar_token', None)
    calendar_days, available_calendars, enabled_calendar_ids = [], [], ''
    if calendar_token:
        raw_events, available_calendars = get_upcoming_events(calendar_token, days_ahead=0)
        calendar_days = _format_calendar_events(raw_events, days_ahead=0)
        primary_id = next((c['id'] for c in available_calendars if c['is_primary']), 'primary')
        enabled_ids = [c for c in (calendar_token.enabled_calendar_ids or [primary_id])]
        for c in available_calendars:
            c['color'] = _calendar_color(c['id'])
        enabled_calendar_ids = ','.join(enabled_ids)

    from core.models import QuickBooksToken
    from core.quickbooks import is_configured as quickbooks_is_configured
    quickbooks_token = QuickBooksToken.objects.first()

    return render(request, 'tickets/owner_dashboard.html', {
        'now': now,
        'department_boxes': department_boxes,
        'off_track': off_track,
        'onsite': onsite,
        'session_drift': session_drift,
        'movement': movement,
        'quiet': quiet,
        'calendar_configured': calendar_is_configured(),
        'calendar_token': calendar_token,
        'calendar_days': calendar_days,
        'available_calendars': available_calendars,
        'enabled_calendar_ids': enabled_calendar_ids,
        'current_timezone': getattr(staff_profile, 'timezone', ''),
        'timezone_choices': StaffProfile.Timezone.choices,
        'quickbooks_token': quickbooks_token,
        'quickbooks_configured': quickbooks_is_configured(),
        'office_latitude': settings.OFFICE_LATITUDE,
        'office_longitude': settings.OFFICE_LONGITUDE,
        'office_location_name': settings.OFFICE_LOCATION_NAME,
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
        # enumerate() rather than columns.index(col) — index() matches by
        # value equality, so two columns holding equal-content event dicts
        # could resolve to the wrong column's position, mis-assigning
        # _col and overlapping unrelated events' clickable boxes on top of
        # each other on screen.
        for idx, col in enumerate(columns):
            if col[-1]['end'] <= ev['start']:
                col.append(ev)
                ev['_col'] = idx
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
                    by_day[d]['all_day'].append({
                        'title': title, 'color': color, 'all_day': True,
                        'event_id': e.get('id', ''), 'calendar_id': e.get('_calendar_id', ''),
                        'date': start_date,
                    })
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
            'title': title, 'start': start_dt, 'end': end_dt, 'color': color, 'all_day': False,
            'start_label': start_dt.strftime('%I:%M %p').lstrip('0'),
            'end_label': end_dt.strftime('%I:%M %p').lstrip('0'),
            'event_id': e.get('id', ''), 'calendar_id': e.get('_calendar_id', ''),
            'date': start_dt.date(), 'start_time': start_dt.strftime('%H:%M'), 'end_time': end_dt.strftime('%H:%M'),
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


def _tickets_with_new_activity(tickets, user):
    """Ticket ids (from `tickets`) where a vendor communication (a Quo
    message or a logged SMS) landed after `user` last opened this
    ticket's detail page. Only tickets this user has actually opened
    before are eligible — a ticket nobody's looked at yet isn't "new
    since last viewed," it's just unread, a different concept the
    dashboard already surfaces elsewhere (needs-a-due-date, Today, etc)."""
    from intake.models import QuoMessage

    candidate_ids = [t.pk for t in tickets if t.assigned_contact_id]
    if not candidate_ids:
        return set()

    viewed_at = dict(
        TicketView.objects.filter(ticket_id__in=candidate_ids, user=user)
        .values_list('ticket_id', 'last_viewed_at')
    )
    if not viewed_at:
        return set()

    by_id = {t.pk: t for t in tickets if t.pk in viewed_at}
    conversation_ids = [t.source_reference for t in by_id.values() if t.source_reference]
    quo_latest = dict(
        QuoMessage.objects.filter(conversation_id__in=conversation_ids)
        .values('conversation_id').annotate(latest=Max('quo_created_at'))
        .values_list('conversation_id', 'latest')
    )
    followup_latest = dict(
        FollowUpLog.objects.filter(ticket_id__in=by_id.keys(), channel=FollowUpLog.Channel.SMS)
        .values('ticket_id').annotate(latest=Max('sent_at')).values_list('ticket_id', 'latest')
    )

    updated = set()
    for ticket_id, last_viewed in viewed_at.items():
        t = by_id.get(ticket_id)
        if not t:
            continue
        latest = quo_latest.get(t.source_reference) if t.source_reference else None
        fu = followup_latest.get(ticket_id)
        if fu and (latest is None or fu > latest):
            latest = fu
        if latest and latest > last_viewed:
            updated.add(ticket_id)
    return updated


@login_required
def department_dashboard(request, role):
    """A department's own front page, split into the things staff actually
    distinguish: reactive Tickets, this department's open recurring
    Sessions (see the worksessions app — replaced the old source=recurring
    Ticket "Tasks" column entirely, see the "Recurring work overhaul —
    sessions" build brief), and the logged-in viewer's own Google Calendar
    (about their day, not the team's, so it's the same regardless of which
    department they're looking at).

    Tickets are split into three groups:
    - Needs a due date: nobody's triaged these yet, so they're not
      "Today's" work until someone assigns one — shown first, as a
      to-do, not folded into Today where they'd get lost among real
      due-today items.
    - Today: due today or overdue. Closing one via "Close No Follow-Up"
      strikes it through in place client-side (see _dashboard_item.html)
      for immediate confirmation, but it never reappears on a fresh load
      of this page — the query only ever pulls OPEN_STATUSES.
    - Next 2 days, and a collapsed count of everything further out.

    Sessions aren't bucketed the same way (they're not a per-item triage
    queue) — just every open Session for this department, soonest due
    first, mirroring "My Sessions"' own "not a ticket queue" philosophy.
    """
    if role not in StaffProfile.Role.values:
        raise Http404
    now = timezone.now()
    today = timezone.localdate()
    soon_cutoff = today + timedelta(days=2)

    # source=recurring is retired (see worksessions) — excluded defensively
    # in case any pre-decommission row is still lingering, but nothing
    # creates new ones anymore.
    qs = list(
        Ticket.objects.filter(assigned_role=role, property__isnull=False, status__in=OPEN_STATUSES)
        .exclude(source=Ticket.Source.RECURRING)
        .select_related(
            'property', 'assigned_staff__user', 'assigned_contact', 'created_from_template', 'package_run__package',
        )
        .prefetch_related('checklist_items')
    )
    updated_ticket_ids = _tickets_with_new_activity(qs, request.user)

    needs_date_tickets = []
    today_tickets, soon_tickets = [], []
    later_ticket_count = 0
    for t in qs:
        if t.due_date:
            d = timezone.localtime(t.due_date).date()
            if d <= today:
                today_tickets.append(t)
            elif d <= soon_cutoff:
                soon_tickets.append(t)
            else:
                later_ticket_count += 1
        else:
            needs_date_tickets.append(t)

    today_tickets.sort(key=lambda t: _daily_checklist_key(t, now))
    soon_tickets.sort(key=lambda t: _ticket_urgency_key(t, now))
    needs_date_tickets.sort(key=lambda t: (PRIORITY_RANK.get(t.priority, 2), t.title))

    from worksessions.models import Session as _Session
    department_sessions = list(
        _Session.objects.filter(department=role, status=_Session.Status.OPEN)
        .select_related('template').prefetch_related('lines')
        .order_by('due_at', 'opens_at')
    )
    for s in department_sessions:
        s.done_count, s.total_count = s.progress()
        s.overdue = s.is_overdue(today)

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
        'updated_ticket_ids': updated_ticket_ids,
        'timezone_choices': StaffProfile.Timezone.choices,
        'current_timezone': getattr(staff_profile, 'timezone', ''),
        'needs_date_tickets': needs_date_tickets,
        'today_tickets': today_tickets,
        'soon_tickets': soon_tickets,
        'later_ticket_count': later_ticket_count,
        'ticket_total': len(needs_date_tickets) + len(today_tickets) + len(soon_tickets) + later_ticket_count,
        'department_sessions': department_sessions,
        'ticket_list_url': f"{reverse('ticket_list')}?role={role}&source=reactive",
        'now': now,
        'calendar_configured': calendar_is_configured(),
        'calendar_token': calendar_token,
        'calendar_days': calendar_days,
        'available_calendars': available_calendars,
        'enabled_calendar_ids': enabled_calendar_ids,
    })


@login_required
def ticket_pending(request):
    """Decommissioned along with reactive/AI ticket intake (Gmail/Quo/
    calendar/Airbnb/VRBO polling no longer creates tickets — see
    proptasks/scheduler.py) — nothing populates these review queues
    anymore, so the screen just sends staff back to the dashboard. Kept as
    a redirect rather than deleted so any bookmarked/old link still goes
    somewhere sensible instead of 404ing."""
    return redirect('dashboard')


@login_required
def ticket_duplicate_dismiss(request, pk):
    """"Possible duplicate" row's "No — keep as separate ticket" — clears
    the flag so the ticket falls through to whichever of the other pending
    buckets applies next (or straight into the normal ticket flow if it
    already has a due date and department)."""
    ticket = get_object_or_404(Ticket, pk=pk, possible_duplicate_of__isnull=False)
    if request.method == 'POST':
        title = request.POST.get('title', '').strip()
        if title:
            ticket.title = title
        ticket.possible_duplicate_of = None
        ticket.duplicate_reasoning = ''
        ticket.save()
        messages.success(request, f'"{ticket.title}" kept as a separate ticket.')
    return redirect('ticket_pending')


@login_required
def ticket_duplicate_confirm(request, pk):
    """"Possible duplicate" row's "Yes — this is a duplicate" — cancels the
    new ticket with a reason pointing at the original rather than deleting
    it outright, so the record (and its raw_context) stays for reference."""
    ticket = get_object_or_404(Ticket, pk=pk, possible_duplicate_of__isnull=False)
    if request.method == 'POST':
        original = ticket.possible_duplicate_of
        ticket.status = Ticket.Status.CANCELLED
        ticket.cancelled_at = timezone.now()
        ticket.cancelled_reason = f'Duplicate of ticket #{original.pk} "{original.title}" — confirmed by staff'[:300]
        ticket.save()
        messages.success(request, f'Cancelled as a duplicate of "{original.title}".')
    return redirect('ticket_pending')


@login_required
def ticket_ai_match_save(request, pk):
    """AI Property Match row's Save — sets whichever of property (if
    Claude couldn't guess it)/due date/department the staff member just
    picked. No explicit "accept" step: the ticket simply stops matching
    the AI Property Match queryset (and starts behaving like any other
    ticket) once both due_date and assigned_role are set."""
    ticket = get_object_or_404(
        Ticket, pk=pk, source='email', due_date__isnull=True, assigned_role='',
    )
    if request.method == 'POST':
        title = request.POST.get('title', '').strip()
        if title:
            ticket.title = title
        description = request.POST.get('description')
        if description is not None:
            ticket.description = description.strip()
        property_id = request.POST.get('property_id')
        if property_id and not ticket.property_id:
            ticket.property_id = property_id
        raw_due_date = request.POST.get('due_date', '')
        if raw_due_date:
            parsed = parse_date(raw_due_date)
            if parsed:
                ticket.due_date = timezone.make_aware(datetime.combine(parsed, datetime.min.time()))
        assigned_role = request.POST.get('assigned_role')
        if assigned_role in StaffProfile.Role.values:
            ticket.assigned_role = assigned_role
        ticket.save()
        if ticket.due_date and ticket.assigned_role:
            messages.success(request, f'"{ticket.title}" is ready — moved to {ticket.get_assigned_role_display()}.')
        else:
            messages.success(request, 'Saved.')
    return redirect('ticket_pending')


@login_required
def ticket_needs_date_save(request, pk):
    """"Needs a due date" row's Save — property and department are already
    set here, so this only ever sets due_date (plus an optional
    description edit). Graduates out of the pending screen automatically
    once due_date is set, same no-explicit-accept pattern as AI Property
    Match."""
    ticket = get_object_or_404(Ticket, pk=pk, due_date__isnull=True, property__isnull=False)
    if request.method == 'POST':
        title = request.POST.get('title', '').strip()
        if title:
            ticket.title = title
        description = request.POST.get('description')
        if description is not None:
            ticket.description = description.strip()
        raw_due_date = request.POST.get('due_date', '')
        if raw_due_date:
            parsed = parse_date(raw_due_date)
            if parsed:
                ticket.due_date = timezone.make_aware(datetime.combine(parsed, datetime.min.time()))
        ticket.save()
        if ticket.due_date:
            messages.success(request, f'"{ticket.title}" now has a due date.')
        else:
            messages.success(request, 'Saved.')
    return redirect('ticket_pending')


@login_required
def ticket_needs_date_delete(request, pk):
    ticket = get_object_or_404(Ticket, pk=pk, due_date__isnull=True, property__isnull=False)
    if request.method == 'POST':
        title = ticket.title
        ticket.delete()
        messages.success(request, f'Deleted "{title}".')
    return redirect('ticket_pending')


@login_required
def ticket_ai_match_delete(request, pk):
    ticket = get_object_or_404(
        Ticket, pk=pk, source='email', due_date__isnull=True, assigned_role='',
    )
    if request.method == 'POST':
        title = ticket.title
        ticket.delete()
        messages.success(request, f'Deleted "{title}".')
    return redirect('ticket_pending')


@login_required
def ticket_pending_save(request, pk):
    """Pending items are unconfirmed candidates, not finished tickets yet —
    this is where staff clean up the description and either assign it a
    property (which moves it into its department's real queue) or leave
    the property blank to keep refining it later."""
    ticket = get_object_or_404(Ticket, pk=pk, property__isnull=True)
    if request.method == 'POST':
        title = request.POST.get('title', '').strip()
        if title:
            ticket.title = title
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


# Column key -> ORM field, in the same order the table renders them. Used by
# both _ticket_sort_order (build the .order_by()) and _ticket_sort_columns
# (build each header's click-to-sort link) so the two never drift apart.
TICKET_SORT_FIELDS = [
    ('department', 'assigned_role', 'Department'),
    ('property', 'property__name', 'Property'),
    ('issue', 'title', 'Issue'),
    ('due', 'due_date', 'Due'),
    ('status', 'status', 'Status'),
    ('staff', 'assigned_staff__user__first_name', 'Assigned Staff'),
    ('contractor', 'assigned_contact__name', 'Assigned Contractor'),
]

# The list always opens sorted by due date, soonest first — not just "most
# recently created" — until staff picks a different column.
DEFAULT_TICKET_SORT = 'due'


def _ticket_sort_order(sort_param):
    """A `sort=` GET value like 'due' (ascending) or '-due' (descending) ->
    the .order_by() args. Falls back to DEFAULT_TICKET_SORT when unset/
    invalid, and always appends '-created_at' as a stable secondary key so
    rows with equal values (e.g. every 'Assigned' status, or every blank
    due_date) don't visibly shuffle between requests."""
    sort_param = sort_param or DEFAULT_TICKET_SORT
    key = sort_param.lstrip('-')
    is_desc = sort_param.startswith('-')
    fields = {field_key: field for field_key, field, _ in TICKET_SORT_FIELDS}
    if key not in fields:
        key, is_desc = DEFAULT_TICKET_SORT, False
    field = fields[key]
    if key == 'due':
        # due_date is nullable — nulls last regardless of direction, so a
        # ticket with no due date never jumps to the front of an ascending
        # sort just because SQL treats NULL as the lowest value.
        primary = F(field).desc(nulls_last=True) if is_desc else F(field).asc(nulls_last=True)
    else:
        primary = f'-{field}' if is_desc else field
    return [primary, '-created_at']


def _ticket_sort_columns(sort_param):
    """One entry per sortable column for the template's header row: the
    single GET value clicking that header's label should link to (toggles
    asc/desc if it's already the active column, else defaults to
    ascending), and whether it's the currently active sort (for the arrow
    indicator)."""
    active = sort_param or DEFAULT_TICKET_SORT
    key = active.lstrip('-')
    is_desc = active.startswith('-')
    columns = []
    for field_key, _, label in TICKET_SORT_FIELDS:
        is_active = key == field_key
        next_sort = f'-{field_key}' if (is_active and not is_desc) else field_key
        columns.append({
            'key': field_key,
            'label': label,
            'next_sort': next_sort,
            'is_active': is_active,
            'is_desc': is_active and is_desc,
        })
    return columns


@login_required
def ticket_list(request):
    """Defaults to the active bucket (open/assigned/in_progress/blocked) —
    completed/verified/cancelled tickets are only noise day-to-day, so they
    stay hidden unless staff explicitly ask for them via the status filter
    ('complete' for the whole historical bucket, or a specific status like
    'cancelled' to drill into just one)."""
    qs = Ticket.objects.select_related('property', 'unit', 'assigned_staff__user', 'assigned_contact').all()
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

    assigned_staff_id = request.GET.get('assigned_staff')
    selected_assigned_staff = None
    if assigned_staff_id == 'none':
        qs = qs.filter(assigned_staff__isnull=True)
    elif assigned_staff_id:
        qs = qs.filter(assigned_staff_id=assigned_staff_id)
        selected_assigned_staff = StaffProfile.objects.select_related('user').filter(pk=assigned_staff_id).first()

    if request.GET.get('delayed'):
        qs = qs.filter(delayed=True)

    completed_since = request.GET.get('completed_since')
    if completed_since and completed_since.isdigit():
        qs = qs.filter(completed_at__gte=timezone.now() - timedelta(days=int(completed_since)))

    assigned_contact_id = request.GET.get('assigned_contact')
    selected_assigned_contact = None
    if assigned_contact_id:
        qs = qs.filter(assigned_contact_id=assigned_contact_id)
        selected_assigned_contact = Contact.objects.filter(pk=assigned_contact_id).first()

    today = timezone.localdate()
    due = request.GET.get('due', '')
    due_on = parse_date(request.GET.get('due_on', '') or '')
    if due == 'overdue':
        qs = qs.filter(due_date__lt=today)
    elif due == 'today':
        qs = qs.filter(due_date=today)
    elif due == 'tomorrow':
        qs = qs.filter(due_date=today + timedelta(days=1))
    elif due == 'week':
        qs = qs.filter(due_date__gte=today, due_date__lte=today + timedelta(days=7))
    elif due == 'month':
        qs = qs.filter(due_date__gte=today, due_date__lte=today + timedelta(days=30))
    elif due == 'none':
        qs = qs.filter(due_date__isnull=True)
    elif due == 'custom' and due_on:
        qs = qs.filter(due_date=due_on)
    else:
        due = ''
    due_labels = {
        'overdue': 'Overdue', 'today': 'Today', 'tomorrow': 'Tomorrow',
        'week': 'Next 7 days', 'month': 'Next 30 days', 'none': 'No due date',
        # format_date (Django's own dateformat, not C strftime) — %-d isn't
        # portable: it's a glibc extension, absent on Windows and on
        # musl-libc Linux images, so it crashed outright wherever that flag
        # actually reaches an unsupported strftime() with a real ValueError,
        # not a silently-wrong date. 'M j, Y' is the format-letter
        # equivalent of "%b %-d, %Y" (no leading zero on the day) and
        # doesn't touch the C library at all.
        'custom': format_date(due_on, 'M j, Y') if due_on else 'Custom date',
    }

    q = request.GET.get('q', '').strip()
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(property__name__icontains=q))

    qs = qs.order_by(*_ticket_sort_order(request.GET.get('sort', '')))

    return render(request, 'tickets/ticket_list.html', {
        'tickets': qs,
        'now': timezone.now(),
        'status_choices': Ticket.Status.choices,
        'role_choices': StaffProfile.Role.choices,
        'priority_choices': Priority.choices,
        'selected_status': status,
        'selected_status_label': dict(Ticket.Status.choices).get(status),
        'selected_role': role,
        'selected_role_label': dict(StaffProfile.Role.choices).get(role) if role else None,
        'selected_source': source,
        'selected_template_id': template_id,
        'selected_template': selected_template,
        'selected_scheduled_for': scheduled_for,
        'selected_property': selected_property,
        'selected_assigned_staff': selected_assigned_staff,
        'selected_assigned_contact': selected_assigned_contact,
        'selected_due': due,
        'selected_due_on': due_on.isoformat() if due_on else '',
        'selected_due_label': due_labels.get(due),
        'q': q,
        'staff_list': StaffProfile.objects.select_related('user'),
        'vendor_groups': group_vendors_by_trade(
            Contact.objects.filter(contact_type=Contact.ContactType.VENDOR).order_by('name')
        ),
        'properties_by_type': properties_by_type(),
        'sort_columns': _ticket_sort_columns(request.GET.get('sort', '')),
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

        priority = request.POST.get('priority')
        if priority in Priority.values:
            ticket.priority = priority

        status = request.POST.get('status')
        if status in Ticket.Status.values and status != Ticket.Status.COMPLETED:
            ticket.status = status

        kind, _, raw_id = request.POST.get('assignee', '').partition('-')
        if kind == 'staff' and raw_id.isdigit():
            ticket.assigned_staff_id = int(raw_id)
            ticket.assigned_contact = None
            ticket.assignment_source = Ticket.AssignmentSource.MANUAL
        elif kind == 'contact' and raw_id.isdigit():
            ticket.assigned_contact_id = int(raw_id)
            ticket.assigned_staff = None
            ticket.assignment_source = Ticket.AssignmentSource.MANUAL
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
    """Inline due-date edit — from the tickets list (next_qs present), a
    department dashboard's "needs a due date" box (next_role present), or
    ticket detail's Edit Due Date bubble-lock control (next_ticket_detail
    present). Pushing an already-set due_date later flags the ticket
    delayed and keeps the old value in previous_due_date for the
    translucent/struck-through display (see ticket_detail.html) — moving
    it back to or before that value clears the flag. Assigning a first
    due_date (old_due_date was None) is never itself a delay."""
    ticket = get_object_or_404(Ticket, pk=pk)
    if request.method == 'POST':
        raw = request.POST.get('due_date', '')
        new_due_date = None
        if raw:
            parsed = parse_date(raw)
            if parsed:
                new_due_date = timezone.make_aware(datetime.combine(parsed, datetime.min.time()))
        _apply_due_date_change(ticket, new_due_date)
        ticket.save(update_fields=['due_date', 'delayed', 'previous_due_date'])
    if 'next_qs' in request.POST:
        return _list_redirect(request)
    if 'next_ticket_detail' in request.POST:
        return redirect('ticket_detail', pk=pk)
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
    """Per-column suggested bubbles for Related contacts' Owner/Contractor/
    Additional columns — scoped tightly to THIS property (not "same
    property type," which used to surface every contact tied to any
    property of the same type and made the suggestion pool unusably big):

    - Owner: contacts of an owner-ish type directly linked to this
      property (Contact.properties).
    - Contractor: vendors who've actually worked this property before —
      linked as a Related Contact (TicketContact) or set as Assign
      Contractor's assigned_contact on ANY ticket for this property,
      historically. That history is already fully captured by those two
      existing relationships; this just reads it across every ticket for
      the property instead of only this one, rather than needing a new
      tracking model.
    - Additional: no contact_type restriction at all — any contact
      (including a vendor or owner already covered by the other two
      columns) can be tracked as an "additional" related contact, scoped
      to contacts already linked to this property.

    Whatever's already linked to THIS ticket under a role is folded in
    even if it wouldn't otherwise qualify, so the bubble picker always has
    something to find-and-lock on load — see bubble-picker.js's
    rehydration."""
    linked_by_role = {}
    for tc in linked_ticket_contacts:
        linked_by_role.setdefault(tc.role, []).append(tc.contact)

    def _pool(qs, role):
        pool = {c.pk: c for c in qs}
        for c in linked_by_role.get(role, []):
            pool[c.pk] = c
        return sorted(pool.values(), key=lambda c: c.name)

    if ticket.property_id:
        owner_contacts = _pool(
            Contact.objects.filter(
                contact_type__in=[
                    Contact.ContactType.OWNER, Contact.ContactType.BOARD_MEMBER,
                    Contact.ContactType.ASSOCIATION_MEMBER, Contact.ContactType.TENANT,
                ],
                properties=ticket.property_id,
            ).distinct(),
            TicketContact.Role.OWNER,
        )

        worked_here_ids = set(
            TicketContact.objects.filter(
                role=TicketContact.Role.CONTRACTOR, ticket__property_id=ticket.property_id,
            ).values_list('contact_id', flat=True)
        ) | set(
            Ticket.objects.filter(property_id=ticket.property_id, assigned_contact__isnull=False)
            .values_list('assigned_contact_id', flat=True)
        )
        contractor_contacts = _pool(
            Contact.objects.filter(pk__in=worked_here_ids, contact_type=Contact.ContactType.VENDOR),
            TicketContact.Role.CONTRACTOR,
        )

        additional_contacts = _pool(
            Contact.objects.filter(properties=ticket.property_id).distinct(), TicketContact.Role.OTHER,
        )
    else:
        # No property context yet — nothing to suggest, search still works.
        owner_contacts = _pool(Contact.objects.none(), TicketContact.Role.OWNER)
        contractor_contacts = _pool(Contact.objects.none(), TicketContact.Role.CONTRACTOR)
        additional_contacts = _pool(Contact.objects.none(), TicketContact.Role.OTHER)

    # Whoever's assigned via Reassign is clearly the contractor on this job
    # — surface them here too (one click to also track them as a related
    # contact) even on the very first ticket for this property.
    if ticket.assigned_contact_id and ticket.assigned_contact_id not in {c.pk for c in contractor_contacts}:
        contractor_contacts = sorted(contractor_contacts + [ticket.assigned_contact], key=lambda c: c.name)

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
        quo_out_bodies = set()
        for m in (quo_messages or []):
            at = _parse_quo_timestamp(m.get('at', ''))
            if at:
                entries.append({'direction': m['direction'], 'body': m['body'], 'at': at, 'related': True})
                if m['direction'] == 'out':
                    quo_out_bodies.add(m['body'].strip())

        # Not bound yet — fall back to the FollowUpLog audit trail for our
        # own sends, but skip any whose text already showed up in the live
        # Quo fetch above: send_via_quo's own send already puts the message
        # in Quo's conversation history immediately, so once this contact
        # has ANY Quo thread (even one started after this exact message),
        # counting both sources renders the same outbound text twice.
        for log in ticket.followups.filter(contact=contact, channel=FollowUpLog.Channel.SMS):
            if log.body.strip() in quo_out_bodies:
                continue
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
def _safe_back_url(request, exclude_path=None, fallback_view='ticket_list'):
    """Wherever the browser actually navigated from (dashboard, a filtered
    ticket list, a property page, the pending screen, ...), so the ticket
    detail's back button returns to the real point of entry instead of a
    fixed destination that loses whatever filter/scroll position the user
    had. Falls back to fallback_view if there's no referrer, it points
    off-site (open-redirect guard, same check Django's login view uses),
    or it's the ticket detail page's own URL — every in-page action here
    (status change, reassign, ...) POSTs to its own endpoint and redirects
    right back to this page, so a same-day-old referrer would otherwise
    make "back" a no-op loop."""
    referer = request.META.get('HTTP_REFERER', '')
    if not referer or not url_has_allowed_host_and_scheme(referer, allowed_hosts={request.get_host()}):
        return reverse(fallback_view)
    if exclude_path and urlsplit(referer).path == exclude_path:
        return reverse(fallback_view)
    return referer


def ticket_detail(request, pk):
    ticket = get_object_or_404(
        Ticket.objects.select_related(
            'property', 'unit', 'assigned_staff__user', 'assigned_contact', 'created_from_template',
            'template_occurrence', 'package_run__package',
        ),
        pk=pk,
    )
    # Opening this page IS "having seen it" — resets this user's own "new
    # vendor activity" indicator on the department dashboard (see
    # department_dashboard's _tickets_with_new_activity) going forward.
    TicketView.objects.update_or_create(
        ticket=ticket, user=request.user, defaults={'last_viewed_at': timezone.now()},
    )
    reassign_form = ReassignForm(initial={
        'assigned_role': ticket.assigned_role,
        'assigned_staff': ticket.assigned_staff_id,
    })
    assign_contractor_form = AssignContractorForm(initial={'assigned_contact': ticket.assigned_contact_id})
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

    vendor_link_cooldown_hours_left = 0
    if ticket.vendor_link_sent_at:
        elapsed = timezone.now() - ticket.vendor_link_sent_at
        if elapsed < timedelta(hours=24):
            vendor_link_cooldown_hours_left = max(1, round(24 - elapsed.total_seconds() / 3600))

    back_url = _safe_back_url(request, exclude_path=request.path)
    all_attachments = list(ticket.attachments.all().order_by('-created_at'))

    return render(request, 'tickets/ticket_detail.html', {
        'ticket': ticket,
        'back_url': back_url,
        'reassign_form': reassign_form,
        'assign_contractor_form': assign_contractor_form,
        'assignment_logs': ticket.assignment_logs.select_related(
            'from_staff__user', 'to_staff__user', 'from_contact', 'to_contact',
        )[:10],
        'followup_text_parties': [c for c in followup_parties if c.phone],
        'followup_email_parties': [c for c in followup_parties if c.email],
        'attachments': all_attachments,
        'media_attachments': [a for a in all_attachments if a.is_image or a.is_video],
        'photo_attachments': [a for a in all_attachments if a.is_image],
        'document_attachments': [a for a in all_attachments if a.is_document],
        'ticket_contacts': linked_ticket_contacts,
        'owner_contacts': contact_pools['owner_contacts'],
        'owner_ids': contact_pools['owner_ids'],
        'contractor_contacts': contact_pools['contractor_contacts'],
        'contractor_ids': contact_pools['contractor_ids'],
        'additional_contacts': contact_pools['additional_contacts'],
        'additional_ids': contact_pools['additional_ids'],
        'owner_contacts_json': json.dumps([
            {'id': c.id, 'label': str(c), 'has_phone': bool(c.phone), 'has_email': bool(c.email)}
            for c in Contact.objects.filter(contact_type__in=[
                Contact.ContactType.OWNER, Contact.ContactType.BOARD_MEMBER,
                Contact.ContactType.ASSOCIATION_MEMBER, Contact.ContactType.TENANT,
            ])
        ]),
        'contractor_search_json': json.dumps([
            {'id': c.id, 'label': str(c), 'has_phone': bool(c.phone), 'has_email': bool(c.email)}
            for c in Contact.objects.filter(contact_type=Contact.ContactType.VENDOR)
        ]),
        # Additional contacts has no type restriction on who can be added —
        # search finds anyone, including a vendor or owner also tracked
        # elsewhere on the ticket.
        'additional_contacts_json': json.dumps([
            {'id': c.id, 'label': str(c), 'has_phone': bool(c.phone), 'has_email': bool(c.email)}
            for c in Contact.objects.all()
        ]),
        'followup_batches': _group_followups(ticket.followups.select_related('contact')[:30]),
        'checklist_items': ticket.checklist_items.all(),
        'process_runs': ticket.process_runs.select_related('process_template').prefetch_related('steps__attachments'),
        'attachable_process_templates': ProcessTemplate.objects.filter(is_active=True),
        'process_upload_form': ProcessAttachmentUploadForm(),
        'package_siblings': package_siblings,
        'blocking_step_label': blocking_step_label,
        'occurrence_siblings': occurrence_siblings,
        'can_approve': can_approve,
        'priority_choices': Priority.choices,
        'status_choices': Ticket.Status.choices,
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
        'vendor_link_cooldown_hours_left': vendor_link_cooldown_hours_left,
        'status_notes': ticket.status_notes.select_related('created_by'),
        'closing_notes': ticket.closing_notes.select_related('created_by'),
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
        initial = {}
        # "New ticket" from a contact's own page/row (see contact_list.html,
        # contact_edit.html) — pre-attaches them as the basis for the ticket
        # without staff having to search for them again. Vendors go in as
        # the contractor (that's what a vendor contact IS for); everyone
        # else (owner/tenant/guest/board member/...) as the reporter, since
        # they're the one who'd be reporting an issue.
        contact_id = request.GET.get('contact')
        if contact_id:
            contact = Contact.objects.filter(pk=contact_id).first()
            if contact:
                field = 'assigned_contact' if contact.contact_type == Contact.ContactType.VENDOR else 'reporter_contact'
                initial[field] = contact.pk
        form = TicketForm(initial=initial)

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
        'units_by_property_json': _units_by_property_json(),
    })


def _units_by_property_json():
    """{property_id: [{id, label}, ...]} for every property that has at
    least one active Unit — fed into ticket_form.html's Unit bubble picker,
    which shows/repopulates itself client-side off this map as soon as a
    property with units is selected. Whole-map, not per-request-filtered:
    the number of multi-unit properties is small enough that this is
    cheaper than a per-property AJAX round trip."""
    grouped = {}
    for unit in Unit.objects.filter(is_active=True).select_related('property').order_by('label'):
        grouped.setdefault(str(unit.property_id), []).append({'id': unit.pk, 'label': unit.label})
    return json.dumps(grouped)


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
def function_list(request):
    """Functions (TaskPackage) are the main-nav "Functions" landing page —
    the primary way staff organize recurring work now: start a Function,
    optionally add Task Group(s) under it, then add Recurring Tasks either
    directly on the Function or inside one of its groups. A TicketTemplate
    never attached to any Function stays reachable via the older flat
    ticket_template_list ("All recurring task rules")."""
    packages = TaskPackage.objects.prefetch_related('task_groups', 'steps').order_by('title')
    q = request.GET.get('q', '').strip()
    if q:
        packages = packages.filter(title__icontains=q)
    packages = list(packages)
    for pkg in packages:
        pkg.step_count = len(pkg.steps.all())
        pkg.group_count = len(pkg.task_groups.all())
    return render(request, 'tickets/function_list.html', {'packages': packages, 'q': q})


@login_required
def function_create(request):
    """Retired — see the "Recurring work overhaul — sessions" build brief.
    The old TicketTemplate/TaskPackage system's automatic generation is
    already gone (removed from proptasks/scheduler.py); this closes the
    other half of "don't run both systems in parallel" by refusing to let
    anyone create a NEW Function, which would just sit inert forever (no
    scheduler job left to ever generate anything from it). function_list/
    function_detail/function_edit stay reachable, unchanged, for whatever
    old rows still exist to be reviewed or cleaned up — only creation of
    new ones is blocked."""
    messages.info(request, 'Create your recurring rule here instead.')
    return redirect('session_template_create')


@login_required
def function_edit(request, pk):
    function = get_object_or_404(TaskPackage, pk=pk)
    if request.method == 'POST':
        form = FunctionForm(request.POST, instance=function)
        if form.is_valid():
            form.save()
            messages.success(request, f'Function "{function.title}" saved.')
            return redirect('function_detail', pk=function.pk)
    else:
        form = FunctionForm(instance=function)
    return render(request, 'tickets/function_form.html', {'form': form, 'is_new': False, 'function': function})


@login_required
def function_delete(request, pk):
    function = get_object_or_404(TaskPackage, pk=pk)
    if request.method == 'POST':
        title = function.title
        function.delete()
        messages.success(request, f'Deleted Function "{title}".')
        return redirect('function_list')
    return redirect('function_detail', pk=pk)


@login_required
def function_detail(request, pk):
    function = get_object_or_404(TaskPackage, pk=pk)
    task_groups = list(function.task_groups.prefetch_related('steps__template').order_by('sequence_order'))
    ungrouped_steps = list(
        function.steps.filter(task_group__isnull=True).select_related('template').order_by('sequence_order')
    )
    for group in task_groups:
        group.target_summary = _group_target_summary(group)
        group.edit_form = TaskGroupForm(instance=group)
        for step in group.steps.all():
            # A grouped step with the group's broad targeting set no longer
            # consults its own Target section — see applicability.py — so
            # its per-step summary is only shown when the group hasn't set
            # one, keeping the table honest about what's actually in effect.
            step.template.target_summary = None if group.target_summary else _target_summary(step.template)
    for step in ungrouped_steps:
        step.template.target_summary = _target_summary(step.template)
    return render(request, 'tickets/function_detail.html', {
        'function': function,
        'task_groups': task_groups,
        'ungrouped_steps': ungrouped_steps,
        'assigned_property_count': function.property_assignments.count(),
        'property_type_choices': Property.Type.choices,
    })


@login_required
def task_group_create(request, package_pk):
    function = get_object_or_404(TaskPackage, pk=package_pk)
    if request.method == 'POST':
        form = TaskGroupForm(request.POST)
        if form.is_valid():
            group = form.save(commit=False)
            group.package = function
            group.sequence_order = function.task_groups.count()
            group.save()
            messages.success(request, f'Task Group "{group.title}" added.')
        else:
            messages.error(request, 'Give the Task Group a title.')
    return redirect('function_detail', pk=package_pk)


@login_required
def task_group_edit(request, pk):
    group = get_object_or_404(TaskGroup, pk=pk)
    if request.method == 'POST':
        form = TaskGroupForm(request.POST, instance=group)
        if form.is_valid():
            form.save()
            messages.success(request, 'Saved.')
        else:
            messages.error(request, 'Give the Task Group a title.')
    return redirect('function_detail', pk=group.package_id)


@login_required
def task_group_delete(request, pk):
    group = get_object_or_404(TaskGroup, pk=pk)
    package_pk = group.package_id
    if request.method == 'POST':
        title = group.title
        group.delete()
        messages.success(request, f'Deleted Task Group "{title}" — its tasks stay on the Function, ungrouped.')
    return redirect('function_detail', pk=package_pk)


@login_required
def ticket_template_create(request):
    """Retired — same reasoning as function_create above. A new
    TicketTemplate created here would never fire (the scheduler no longer
    runs generate_recurring_tickets on a timer); redirect straight to
    where a new recurring rule actually belongs now."""
    messages.info(request, 'Create your recurring rule here instead.')
    return redirect('session_template_create')


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


@login_required
def task_step_duplicate(request, step_pk):
    """Copies a Task (TicketTemplate) and its membership in the Function/
    Task Group it lives in, landing on the copy's edit form so the details
    that should differ (title, date, target) can be adjusted right away
    rather than duplicating in place and leaving two identical tasks."""
    if request.method != 'POST':
        return redirect('dashboard')
    step = get_object_or_404(TaskPackageTemplate, pk=step_pk)
    original = step.template

    new_template = TicketTemplate.objects.get(pk=original.pk)
    new_template.pk = None
    new_template.title = f'{original.title} (copy)'
    new_template.save()
    new_template.required_attributes.set(original.required_attributes.all())
    for item in original.checklist_items.all():
        TemplateChecklistItem.objects.create(
            template=new_template, text=item.text, sequence_order=item.sequence_order, is_required=item.is_required,
        )
    TaskPackageTemplate.objects.create(
        package=step.package, task_group=step.task_group, template=new_template,
        sequence_order=step.package.steps.count(),
    )
    messages.success(request, f'Duplicated "{original.title}" — now editing the copy.')
    return redirect('ticket_template_edit', pk=new_template.pk)


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


def _group_target_summary(group):
    """The broad property category a Task Group applies to, or None when
    the group hasn't set one — in which case its steps fall back to their
    own individual Target settings (see applicability.py::
    template_applies_to_property)."""
    if not group.property_types:
        return None
    type_labels = dict(Property.Type.choices)
    return ', '.join(type_labels.get(t, t) for t in group.property_types)


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
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'add_document':
            name = request.POST.get('name', '').strip()
            file = request.FILES.get('file')
            if name and file:
                TicketTemplateDocument.objects.create(template=template, name=name, file=file, uploaded_by=request.user)
                messages.success(request, 'Document added.')
            else:
                messages.error(request, 'A name and a file are both required.')
        elif action == 'delete_document':
            TicketTemplateDocument.objects.filter(pk=request.POST.get('document_id'), template=template).delete()
            messages.success(request, 'Removed.')
        return redirect('ticket_template_detail', pk=template.pk)
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
        'documents': template.documents.all(),
    })


@login_required
def ticket_reassign(request, pk):
    """Internal Reassign — department/staff only. Setting a specific staff
    member clears any assigned contractor (a ticket goes to Staff or a
    Contractor, not both — see AssignContractorForm/ticket_assign_contractor
    for the reverse direction)."""
    ticket = get_object_or_404(Ticket, pk=pk)
    if request.method == 'POST':
        form = ReassignForm(request.POST)
        if form.is_valid():
            new_staff = form.cleaned_data.get('assigned_staff')
            TicketAssignmentLog.objects.create(
                ticket=ticket,
                from_staff=ticket.assigned_staff, from_contact=ticket.assigned_contact,
                to_staff=new_staff,
                to_contact=None if new_staff else ticket.assigned_contact,
                changed_by=request.user,
            )
            ticket.assigned_staff = new_staff
            if new_staff:
                ticket.assigned_contact = None
                ticket.assignment_source = Ticket.AssignmentSource.MANUAL
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
def ticket_assign_contractor(request, pk):
    """Assign/change the ticket's single contractor (Ticket.assigned_contact
    — drives the vendor portal link and the Contractor Communication card).
    Separate from Internal Reassign per the same staff-XOR-contractor rule,
    just from the other direction: assigning a contractor clears any
    assigned staff member.

    Changing to a different contractor resets Ticket.source_reference (the
    bound Quo conversation) rather than leaving it pointed at the OLD
    contractor's thread — Quo conversations are per phone number, not per
    ticket, so without this the new contractor's card would silently show
    the previous contractor's messages. The old conversation id is kept on
    the TicketAssignmentLog entry (previous_conversation_id) so it's still
    reachable from the audit trail instead of just disappearing."""
    ticket = get_object_or_404(Ticket, pk=pk)
    if request.method == 'POST':
        data = request.POST.copy()
        name = data.get('new_contact__name__contractor', '').strip()
        phone_error = False
        if name:
            phone = data.get('new_contact__phone__contractor', '').strip()
            if not is_valid_phone(phone):
                messages.error(request, 'Phone must be in XXX-XXX-XXXX format — nothing was assigned.')
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
        form = AssignContractorForm(data)
        if not phone_error and form.is_valid():
            new_contact = form.cleaned_data.get('assigned_contact')
            old_contact = ticket.assigned_contact
            contact_changed = new_contact != old_contact
            old_conversation_id = ''
            if contact_changed and ticket.source_reference:
                old_conversation_id = ticket.source_reference
                ticket.source_reference = ''
            TicketAssignmentLog.objects.create(
                ticket=ticket,
                from_staff=ticket.assigned_staff, from_contact=old_contact,
                to_staff=None, to_contact=new_contact,
                changed_by=request.user,
                previous_conversation_id=old_conversation_id,
            )
            if new_contact:
                ticket.assigned_staff = None
                ticket.assignment_source = Ticket.AssignmentSource.MANUAL
            if contact_changed:
                ticket.rotate_completion_token()
            ticket.assigned_contact = new_contact
            if new_contact and ticket.status == Ticket.Status.OPEN:
                ticket.status = Ticket.Status.ASSIGNED
            ticket.full_clean()
            ticket.save()
            messages.success(request, 'Contractor updated.')
        elif not phone_error:
            messages.error(request, 'Could not assign contractor: check the form.')
    return redirect('ticket_detail', pk=ticket.pk)


@login_required
def ticket_previous_conversation(request, pk, log_id):
    """Read-only view of a conversation this ticket was bound to before a
    contractor change reset Ticket.source_reference — see
    ticket_assign_contractor's docstring. The messages themselves were
    never deleted, this just gives the audit trail's "view previous
    conversation" link somewhere to point."""
    from intake.models import QuoMessage

    ticket = get_object_or_404(Ticket, pk=pk)
    log = get_object_or_404(TicketAssignmentLog, pk=log_id, ticket=ticket)
    entries = []
    if log.previous_conversation_id:
        for m in QuoMessage.objects.filter(conversation_id=log.previous_conversation_id).order_by('quo_created_at'):
            if m.quo_created_at:
                entries.append({'direction': m.direction, 'body': m.body, 'at': timezone.localtime(m.quo_created_at)})
    return render(request, 'tickets/_previous_conversation.html', {
        'ticket': ticket, 'log': log, 'entries': entries,
    })


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
def ticket_set_priority(request, pk):
    """Inline priority edit — same pencil-toggle-reveals-a-form pattern as
    Edit Due Date on ticket_detail.html, and also usable as the tickets
    list's inline Priority edit (next_qs present, see _list_redirect) for
    parity with the other already-inline-editable columns there."""
    ticket = get_object_or_404(Ticket, pk=pk)
    if request.method == 'POST':
        new_priority = request.POST.get('priority')
        if new_priority in Priority.values:
            ticket.priority = new_priority
            ticket.save(update_fields=['priority'])
            messages.success(request, f'Priority set to {ticket.get_priority_display()}.')
    if 'next_qs' in request.POST:
        return _list_redirect(request)
    return redirect('ticket_detail', pk=ticket.pk)


@login_required
def ticket_set_contacts(request, pk):
    """The 3-column Related contacts picker's auto-save — every bubble
    lock/unlock in any of the Owner/Contractor/Additional columns submits
    this immediately (see the page-local script in ticket_detail.html), so
    there's no separate Save button. Each column is synced independently
    to TicketContact links under its own role (add missing, remove absent
    — Contact.properties' lock-to-add/unlock-to-remove convention, just
    three of them side by side), and each column's inline add-new-contact
    sub-form is handled the same way ticket_create's contractor/reporter
    fields are.

    AJAX-only in practice (the page-local script always sends
    X-Requested-With — see its own comment on why a plain form.submit()
    was wrong here): every bubble lock/unlock would otherwise navigate the
    whole page, resetting scroll position and any other in-progress edits
    on it. The non-AJAX branch is kept only as a plain-POST fallback."""
    ticket = get_object_or_404(Ticket, pk=pk)
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    if request.method == 'POST':
        data = request.POST.copy()
        columns = (
            ('owner', TicketContact.Role.OWNER, Contact.ContactType.OWNER),
            ('contractor', TicketContact.Role.CONTRACTOR, Contact.ContactType.VENDOR),
            ('additional', TicketContact.Role.OTHER, Contact.ContactType.OTHER),
        )
        phone_error = False
        new_contacts = []
        for prefix, role, default_type in columns:
            name = data.get(f'new_contact__name__{prefix}', '').strip()
            if name:
                phone = data.get(f'new_contact__phone__{prefix}', '').strip()
                if not is_valid_phone(phone):
                    if is_ajax:
                        return JsonResponse({
                            'success': False, 'prefix': prefix,
                            'error': 'Phone must be in XXX-XXX-XXXX format — nothing was saved.',
                        })
                    messages.error(request, 'Phone must be in XXX-XXX-XXXX format — nothing was saved.')
                    phone_error = True
                    continue
                contact, _ = Contact.objects.get_or_create(
                    name=name, phone=phone,
                    email=data.get(f'new_contact__email__{prefix}', '').strip(),
                    defaults={'contact_type': default_type},
                )
                data.setlist(f'{prefix}_contact_ids', data.getlist(f'{prefix}_contact_ids') + [str(contact.pk)])
                new_contacts.append({'prefix': prefix, 'id': contact.pk, 'label': str(contact)})

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

        if is_ajax:
            return JsonResponse({'success': True, 'new_contacts': new_contacts})
    return redirect('ticket_detail', pk=ticket.pk)


def _link_vendor_to_property(ticket):
    """A vendor/contractor who completes a job at a property becomes
    formally associated with it going forward — Contact.properties, the
    same M2M every other "belongs to this property" contact uses, so they
    show up in that property's Contacts card and Related Contacts
    suggestions from then on. add() is a no-op if already linked."""
    if (
        ticket.property_id
        and ticket.assigned_contact_id
        and ticket.assigned_contact.contact_type == Contact.ContactType.VENDOR
    ):
        ticket.assigned_contact.properties.add(ticket.property_id)


@login_required
def ticket_set_status(request, pk):
    ticket = get_object_or_404(Ticket, pk=pk)
    if request.method == 'POST':
        new_status = request.POST.get('status')
        if new_status in Ticket.Status.values:
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

            if new_status in (Ticket.Status.COMPLETED, Ticket.Status.VERIFIED):
                gate_error = process_gate_error_message(ticket)
                if gate_error:
                    messages.error(request, gate_error)
                    if 'next_qs' in request.POST:
                        return _list_redirect(request)
                    return redirect('ticket_detail', pk=ticket.pk)

            # Closing a ticket (COMPLETE_STATUSES — see its own definition
            # above) requires a stated closing status, collected by a popup
            # on the ticket detail page and enforced here too, not just in
            # the UI — every path that can set one of these statuses posts
            # to this same view, so this one check covers all of them.
            closing_body = ''
            if new_status in COMPLETE_STATUSES:
                closing_body = request.POST.get('closing_note', '').strip()
                if not closing_body:
                    messages.error(request, 'A closing status is required before this ticket can be closed.')
                    if 'next_qs' in request.POST:
                        return _list_redirect(request)
                    return redirect('ticket_detail', pk=ticket.pk)

            ticket.status = new_status
            if new_status == Ticket.Status.DEFERRED:
                _apply_due_date_change(ticket, new_due_date)
            if new_status == Ticket.Status.COMPLETED:
                ticket.completed_at = timezone.now()
            if new_status == Ticket.Status.CANCELLED:
                ticket.cancelled_at = timezone.now()
                ticket.cancelled_reason = closing_body[:300]
            ticket.save()
            if new_status in COMPLETE_STATUSES:
                TicketClosingNote.objects.create(
                    ticket=ticket, status=new_status, body=closing_body, created_by=request.user,
                )
            if new_status == Ticket.Status.COMPLETED:
                _link_vendor_to_property(ticket)
            if new_status in Ticket.DEPENDENCY_SATISFYING_STATUSES:
                unblock_dependents(ticket)
            messages.success(request, f'Status updated to {ticket.get_status_display()}.')

            # Cancelling a ticket is usually "I'm done looking at this" —
            # send them back to wherever they came from (the same back_url
            # ticket_detail's own Back link/GET handler computes, passed
            # through as a hidden field) instead of reloading this page.
            if new_status == Ticket.Status.CANCELLED:
                back_url = request.POST.get('back_url', '')
                if back_url and url_has_allowed_host_and_scheme(back_url, allowed_hosts={request.get_host()}):
                    return redirect(back_url)
                return redirect('ticket_list')
            # Completing a ticket is a "done, move on" moment too — send
            # the user back to their dashboard (company-admin or standard,
            # see dashboard()) rather than leaving them sitting on the
            # now-completed ticket's own detail page.
            if new_status == Ticket.Status.COMPLETED:
                return redirect('dashboard')
    if 'next_qs' in request.POST:
        return _list_redirect(request)
    return redirect('ticket_detail', pk=ticket.pk)


@login_required
def ticket_add_status_note(request, pk):
    """The Update Status card's status-update thread — a timestamped,
    reviewable log of free-text notes, independent of the status bubble
    picker above it (posting one doesn't change status, and changing
    status doesn't require one). Replaced the old one-shot resolution_notes/
    status_reason fields, which were never visible as a history."""
    ticket = get_object_or_404(Ticket, pk=pk)
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    if request.method == 'POST':
        body = request.POST.get('body', '').strip()
        if body:
            TicketStatusNote.objects.create(ticket=ticket, body=body, created_by=request.user)
            if is_ajax:
                return JsonResponse({'success': True})
            messages.success(request, 'Update posted.')
        elif is_ajax:
            return JsonResponse({'success': False, 'error': 'Write an update first.'})
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
    """The department dashboard's daily-checklist "Close"/"Close No
    Follow-Up" action — completes a ticket without messaging the
    reporter. The dashboard's fetch handler strikes the row through in
    place on success (see _dashboard_item.html) rather than reloading —
    department_dashboard's query only pulls OPEN_STATUSES, so a full page
    reload naturally drops it instead of requiring special
    same-day-visibility handling. Goes through the same closing_note
    requirement as ticket_set_status (see its own comment) — this is a
    separate view, not a bypass of it, so it has to enforce the same
    rule itself rather than inheriting it for free."""
    ticket = get_object_or_404(Ticket, pk=pk)
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    if request.method == 'POST':
        closing_body = request.POST.get('closing_note', '').strip()
        if not closing_body:
            error = 'A closing status is required before this ticket can be closed.'
            if is_ajax:
                return JsonResponse({'success': False, 'error': error})
            messages.error(request, error)
            if ticket.assigned_role in StaffProfile.Role.values:
                return redirect('department_dashboard', role=ticket.assigned_role)
            return redirect('dashboard')

        gate_error = process_gate_error_message(ticket)
        if gate_error:
            if is_ajax:
                return JsonResponse({'success': False, 'error': gate_error})
            messages.error(request, gate_error)
        else:
            ticket.status = Ticket.Status.COMPLETED
            ticket.completed_at = timezone.now()
            ticket.save()
            TicketClosingNote.objects.create(
                ticket=ticket, status=Ticket.Status.COMPLETED, body=closing_body, created_by=request.user,
            )
            _link_vendor_to_property(ticket)
            if is_ajax:
                return JsonResponse({'success': True})
    if ticket.assigned_role in StaffProfile.Role.values:
        return redirect('department_dashboard', role=ticket.assigned_role)
    return redirect('dashboard')


FOLLOWUP_UPLOAD_ALLOWED_CONTENT_TYPES = ('image/jpeg', 'image/png', 'image/webp', 'image/heic')


def _save_new_followup_attachments(request, ticket):
    """Saves any freshly-uploaded files from the Follow-Up compose's
    `new_files` input as TicketAttachments (same model/storage the
    existing photo-picker already reads from), so they can be sent
    alongside a message the same way an already-existing attachment can.
    Returns (new_pks, error) — on error, nothing was saved and the caller
    should stop before sending anything."""
    new_pks = []
    for f in request.FILES.getlist('new_files'):
        if f.content_type not in FOLLOWUP_UPLOAD_ALLOWED_CONTENT_TYPES:
            return [], f'{f.name}: only photo uploads (JPEG, PNG, WEBP, HEIC) can be attached to a message.'
        if f.size > settings.VENDOR_UPLOAD_MAX_BYTES:
            max_mb = settings.VENDOR_UPLOAD_MAX_BYTES // (1024 * 1024)
            return [], f'{f.name} is too large (max {max_mb}MB).'
        attachment = TicketAttachment.objects.create(ticket=ticket, file=f, uploaded_by_user=request.user)
        new_pks.append(attachment.pk)
    return new_pks, ''


@login_required
def ticket_document_upload(request, pk):
    """The ticket detail Documents card — a general-purpose file upload
    (PDF/Word/Excel/images, not just MMS-able photos) stored as a
    TicketAttachment same as everything else on the ticket; is_document
    (tickets/models.py) keeps it out of the photo/video gallery and the
    Follow-Up compose's picker."""
    ticket = get_object_or_404(Ticket, pk=pk)
    if request.method == 'POST':
        f = request.FILES.get('file')
        if not f:
            messages.error(request, 'Choose a file first.')
        elif f.content_type not in settings.PROCESS_ATTACHMENT_ALLOWED_CONTENT_TYPES:
            messages.error(request, 'That file type isn\'t allowed — photos, PDFs, Word, or Excel files only.')
        elif f.size > settings.PROCESS_ATTACHMENT_MAX_BYTES:
            max_mb = settings.PROCESS_ATTACHMENT_MAX_BYTES // (1024 * 1024)
            messages.error(request, f'File is too large (max {max_mb}MB).')
        else:
            TicketAttachment.objects.create(
                ticket=ticket, file=f, caption=request.POST.get('name', '').strip(), uploaded_by_user=request.user,
            )
            messages.success(request, 'Document added.')
    return redirect('ticket_detail', pk=ticket.pk)


@login_required
def ticket_document_delete(request, pk):
    attachment = get_object_or_404(TicketAttachment, pk=pk)
    ticket_pk = attachment.ticket_id
    if request.method == 'POST':
        attachment.delete()
        messages.success(request, 'Removed.')
    return redirect('ticket_detail', pk=ticket_pk)


@login_required
def ticket_followup_sms(request, pk):
    ticket = get_object_or_404(Ticket, pk=pk)
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    if request.method == 'POST':
        contact_ids = request.POST.getlist('contact_ids')
        body = request.POST.get('body', '').strip()
        new_pks, upload_error = _save_new_followup_attachments(request, ticket)
        if upload_error:
            if is_ajax:
                return JsonResponse({'success': False, 'error': upload_error})
            messages.error(request, upload_error)
            return redirect('ticket_detail', pk=ticket.pk)
        attachment_ids = request.POST.getlist('attachment_ids') + new_pks
        media_urls = [
            request.build_absolute_uri(a.file.url)
            for a in ticket.attachments.filter(pk__in=attachment_ids) if not a.is_video
        ] if attachment_ids else None
        if contact_ids and body:
            logs = send_followup_bulk(
                FollowUpLog.Channel.SMS, contact_ids, body, ticket=ticket, user=request.user,
                media_urls=media_urls,
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
def ticket_send_vendor_link(request, pk):
    """Send Vendor Completion Link button in the Contractor Communication
    card — texts the assigned contractor their vendor-portal completion
    link directly (same token vendor_link in ticket_detail's context
    already uses). Rate-limited to once per 24h via
    Ticket.vendor_link_sent_at so a re-click doesn't spam the same
    contact with repeat asks; enforced server-side (not just a disabled
    button) so it holds across page reloads and multiple staff."""
    ticket = get_object_or_404(Ticket, pk=pk)
    if request.method != 'POST':
        return redirect('ticket_detail', pk=ticket.pk)

    contact = ticket.assigned_contact
    if not contact or not contact.phone:
        return JsonResponse({'success': False, 'error': 'No contractor phone number on file.'})

    if ticket.vendor_link_sent_at and timezone.now() - ticket.vendor_link_sent_at < timedelta(hours=24):
        hours_left = 24 - (timezone.now() - ticket.vendor_link_sent_at).total_seconds() / 3600
        return JsonResponse({
            'success': False,
            'error': f'Already sent — try again in about {max(1, round(hours_left))}h.',
        })

    vendor_link = request.build_absolute_uri(f'/vendor/t/{ticket.completion_token}/')
    prop = ticket.property
    if prop and prop.street and prop.city:
        location = f'{prop.street} in {prop.city}'
    elif prop:
        location = prop.name
    else:
        location = 'your property'
    # Deliberately doesn't name the contractor or reference any internal ticket/company
    # details — this goes straight to the vendor's phone, not staff.
    body = (
        f'Thank You for your help with our recent issue at {location}! To help our '
        'recordkeeping, we kindly ask that you click the following link and give us '
        f'feedback on the job and any related photos or videos: {vendor_link}'
    )
    logs = send_followup_bulk(FollowUpLog.Channel.SMS, [contact.pk], body, ticket=ticket, user=request.user)
    ok = any(log.success for log in logs)
    if ok:
        ticket.vendor_link_sent_at = timezone.now()
        ticket.save(update_fields=['vendor_link_sent_at'])
    return JsonResponse({
        'success': ok,
        'error': '' if ok else 'Send failed — check the contractor\'s phone number.',
        'sent_at': ticket.vendor_link_sent_at.isoformat() if ok else None,
    })


@login_required
def ticket_followup_email(request, pk):
    ticket = get_object_or_404(Ticket, pk=pk)
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    if request.method == 'POST':
        contact_ids = request.POST.getlist('contact_ids')
        subject = request.POST.get('subject', '').strip()
        body = request.POST.get('body', '').strip()
        group = request.POST.get('group') == '1'
        new_pks, upload_error = _save_new_followup_attachments(request, ticket)
        if upload_error:
            if is_ajax:
                return JsonResponse({'success': False, 'error': upload_error})
            messages.error(request, upload_error)
            return redirect('ticket_detail', pk=ticket.pk)
        attachment_ids = request.POST.getlist('attachment_ids') + new_pks
        attachments = (
            [a for a in ticket.attachments.filter(pk__in=attachment_ids) if not a.is_video]
            if attachment_ids else None
        )
        if contact_ids and subject and body:
            logs = send_followup_bulk(
                FollowUpLog.Channel.EMAIL, contact_ids, body, ticket=ticket, subject=subject,
                group=group, user=request.user, attachments=attachments,
            )
            if is_ajax:
                succeeded = sum(1 for log in logs if log.success)
                ok = bool(logs) and succeeded == len(logs)
                if not logs:
                    error = 'Nothing sent — no eligible recipient was selected.'
                elif succeeded == 0:
                    error = 'Send failed — check the recipients\' email addresses.'
                elif not ok:
                    error = f'Sent to {succeeded} of {len(logs)} — check the rest\'s email addresses.'
                else:
                    error = ''
                return JsonResponse({'success': ok, 'sent_count': succeeded, 'error': error})
            _followup_result_message(request, logs, 'recipient(s) by email')
        elif is_ajax:
            return JsonResponse({'success': False, 'error': 'Choose at least one recipient, a subject, and a message.'})
        else:
            messages.error(request, 'Choose at least one recipient and write a message first.')
    return redirect('ticket_detail', pk=ticket.pk)
