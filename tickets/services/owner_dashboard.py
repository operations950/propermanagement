"""Query layer for the Owner Dashboard (tickets/views.py::_owner_dashboard,
tickets/templates/tickets/owner_dashboard.html) — one function per panel,
each returning plain data the template renders. Nothing here talks to the
template directly; nothing in the view builds a queryset directly. See
ONSITE_DESIGN.md-adjacent build brief for the panel-by-panel rationale
("a count you can't act on is noise; a list you can click into is signal").

OPEN_STATUSES/COMPLETE_STATUSES are deliberately re-declared here rather
than imported from tickets.views — that module will import THIS one (the
view calls these query functions), so importing the other way would be
circular. If a status is ever added to Ticket.Status, both copies need
updating; there are only two, and it's cheaper than restructuring
views.py's many existing call sites for a redesign that doesn't need to
touch them."""
from datetime import datetime, time, timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Exists, Max, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce, Greatest
from django.utils import timezone

from ..models import Priority, Ticket, TicketAssignmentLog, TicketClosingNote, TicketStatusNote, TicketView

OPEN_STATUSES = [
    Ticket.Status.OPEN, Ticket.Status.ASSIGNED, Ticket.Status.IN_PROGRESS, Ticket.Status.BLOCKED,
    Ticket.Status.UPCOMING, Ticket.Status.DEFERRED, Ticket.Status.VENDOR_COMPLETE,
]
COMPLETE_STATUSES = [
    Ticket.Status.COMPLETED, Ticket.Status.VERIFIED, Ticket.Status.CANCELLED,
    Ticket.Status.SKIPPED, Ticket.Status.NOT_APPLICABLE,
]

# Row-classification priority for off_track_tickets — most severe first,
# so a ticket matching more than one condition (e.g. blocked AND overdue
# has its own bucket, but "urgent AND delayed" needs a tiebreak) sorts
# where it matters most.
_OFF_TRACK_RANK = {'blocked_overdue': 0, 'overdue': 1, 'blocked': 2, 'delayed': 3, 'urgent': 4}


def off_track_tickets(now=None):
    """Panel 1 — reactive tickets only (source != recurring; recurring
    drift is panel 3's job, at the rule level, not the instance level).
    One query, then a single Python pass to classify — the established
    pattern in this codebase (see the pre-redesign _owner_dashboard) and
    not what the brief's "aggregate in the database" warning is about
    (that's aimed at N+1 per-property/per-department loops, not a flat
    single pass over one already-fetched queryset)."""
    now = now or timezone.now()
    today = timezone.localdate()

    qs = (
        Ticket.objects.filter(status__in=OPEN_STATUSES, property__isnull=False)
        .exclude(source=Ticket.Source.RECURRING)
        .select_related('property', 'assigned_staff__user', 'assigned_contact')
    )

    rows = []
    for t in qs:
        is_overdue = bool(t.due_date and timezone.localtime(t.due_date).date() < today)
        is_blocked = t.status == Ticket.Status.BLOCKED
        if is_blocked and is_overdue:
            reason, since = 'blocked_overdue', t.due_date
        elif is_overdue:
            reason, since = 'overdue', t.due_date
        elif is_blocked:
            reason, since = 'blocked', t.status_changed_at or t.created_at
        elif t.delayed:
            reason, since = 'delayed', t.status_changed_at or t.updated_at
        elif t.priority == Priority.URGENT:
            reason, since = 'urgent', None
        else:
            continue
        days = (now - since).days if since else None
        rows.append({'ticket': t, 'reason': reason, 'since': since, 'days': days})

    rows.sort(key=lambda r: (_OFF_TRACK_RANK[r['reason']], -(r['days'] or 0)))
    return rows


def onsite_next_48h(now=None):
    """Panel 2 — unassigned tomorrow-checkouts first, then today's
    cleanings with live status, then feed health for every known source
    (see BookingFeedHealth) regardless of whether it has ever imported
    anything. That last part is deliberate, not an oversight: a broken
    VRBO feed produces an empty "problems" list that looks identical to a
    genuinely quiet day unless the feed strip itself is always visible."""
    from onsite.models import Booking, BookingFeedHealth, ImportBatch, Visit
    from onsite.views import _visit_status
    from supplies.services import flagged_orders

    now = now or timezone.now()
    today = timezone.localdate()
    tomorrow = today + timedelta(days=1)

    visits = list(
        Visit.objects.filter(scheduled_date__in=[today, tomorrow])
        .exclude(status__in=[Visit.Status.CANCELLED, Visit.Status.SKIPPED])
        .select_related('property', 'visit_type', 'assigned_staff__user', 'assigned_contact')
        .order_by('scheduled_date', 'ready_by')
    )
    for v in visits:
        v.derived_status = _visit_status(v, now)
    todays_visits = [v for v in visits if v.scheduled_date == today]

    tomorrow_checkouts = (
        Booking.objects.filter(check_out__date=tomorrow, status=Booking.Status.ACTIVE, property__is_general=False)
        .select_related('property')
        .prefetch_related('visits')
    )
    from onsite.services.checkouts import without_visit
    from onsite.services.coverage import operational
    unassigned_tomorrow_checkouts = without_visit(operational(tomorrow_checkouts))

    # A file not landing for 2 days running is the "upload agent is
    # broken" alarm condition from BookingFeedHealth's own docstring —
    # not a setting (unlike the gone-quiet thresholds), since it's not
    # asked to be one and picking a sensible fixed value here doesn't
    # carry the same "gets it wrong in a way nobody can see" risk.
    stale_cutoff = now - timedelta(hours=48)
    feed_health = {h.source: h for h in BookingFeedHealth.objects.all()}
    feed_rows = []
    for source, _label in ImportBatch.Source.choices:
        health = feed_health.get(source) or BookingFeedHealth(source=source)  # unsaved stand-in: never imported yet
        health.is_stale = not health.last_upload_at or health.last_upload_at <= stale_cutoff
        feed_rows.append(health)

    from onsite.services.feeds import coverage_report
    coverage = coverage_report()

    return {
        'calendar_gaps': coverage['attention'],
        'calendar_lines_total': coverage['total'],
        'unassigned_tomorrow_checkouts': unassigned_tomorrow_checkouts,
        'todays_visits': todays_visits,
        'feed_health': feed_rows,
        # A supply order still reading below par past the expected delivery
        # window is a delivery failure, not a supply request — see
        # supplies.services.flagged_orders. Never a Ticket (build brief).
        'flagged_supply_orders': flagged_orders(),
    }


def session_templates_drifting(lookback=5):
    """Panel 3 — the unit of attention is the rule (SessionTemplate), not
    the instance; a healthy rule (every recent session submitted on time,
    no repeated skip pattern) doesn't appear at all. Replaces the old
    TicketTemplate-based recurring_rules_drifting(), which was removed once
    the sessions app fully took over and the old system was wiped from
    production — see the "Recurring work overhaul — sessions" build brief.

    Adds one dimension the old panel had no equivalent for: the same line
    skipped repeatedly across sessions, grouped by skip reason — which is
    what makes the skip actionable rather than just a count."""
    from worksessions.models import Session, SessionLine, SessionTemplate

    today = timezone.localdate()
    templates = list(SessionTemplate.objects.filter(is_active=True))
    template_ids = [t.pk for t in templates]

    recent_by_template = {}
    for s in Session.objects.filter(template_id__in=template_ids).order_by('template_id', '-opens_at'):
        bucket = recent_by_template.setdefault(s.template_id, [])
        if len(bucket) < lookback:
            bucket.append(s)
    session_ids = [s.pk for bucket in recent_by_template.values() for s in bucket]

    skip_counts = {}  # (template_id, label) -> {reason: count}
    skip_rows = (
        SessionLine.objects.filter(
            session_id__in=session_ids, state__in=SessionLine.REASON_REQUIRED_STATES,
        )
        .values('session__template_id', 'label', 'skip_reason')
    )
    for row in skip_rows:
        key = (row['session__template_id'], row['label'])
        bucket = skip_counts.setdefault(key, {})
        reason = row['skip_reason'] or '(no reason given)'
        bucket[reason] = bucket.get(reason, 0) + 1

    rows = []
    for template in templates:
        recent = recent_by_template.get(template.pk, [])
        if not recent:
            rows.append({'template': template, 'sessions': [], 'repeated_skips': [], 'status': 'never_opened'})
            continue

        runs = []
        for s in recent:
            if s.status == Session.Status.SUBMITTED:
                late = bool(s.due_at and s.submitted_at and s.submitted_at.date() > s.due_at)
                outcome = 'late' if late else 'submitted'
            elif s.due_at and s.due_at < today:
                outcome = 'overdue'
            else:
                outcome = 'open'
            runs.append({'session': s, 'outcome': outcome})

        repeated_skips = [
            {'label': label, 'reasons': reasons}
            for (tid, label), reasons in skip_counts.items()
            if tid == template.pk and sum(reasons.values()) >= 2
        ]

        outcomes = [r['outcome'] for r in runs]
        if repeated_skips:
            status = 'skipping'
        elif not any(o == 'submitted' for o in outcomes):
            status = 'never_submitted'
        elif 'overdue' in outcomes or 'late' in outcomes:
            status = 'late'
        else:
            status = 'healthy'

        if status != 'healthy':
            rows.append({'template': template, 'sessions': runs, 'repeated_skips': repeated_skips, 'status': status})

    return rows


def movement_today():
    """Panel 5 — updated notes (actual text + author, not just "something
    changed") and closed-today counted separately by reactive vs.
    recurring, so a morning of ticked recurring checkboxes doesn't inflate
    the one number meant to prove real reactive work got done. Each closed
    ticket also carries its own closing_note — the required why-this-is-
    the-final-status text from TicketClosingNote (see that model's own
    docstring) — as row.closing_note, mirroring updated_notes' own
    note.body/note.created_by shape so the template can render both the
    same way."""
    today = timezone.localdate()

    updated_notes = list(
        TicketStatusNote.objects.filter(created_at__date=today)
        .select_related('ticket', 'ticket__property', 'created_by')
        .order_by('-created_at')
    )

    closed_today = list(
        Ticket.objects.filter(status__in=Ticket.TRUE_COMPLETION_STATUSES, completed_at__date=today)
        .select_related('property')
    )
    # One extra query total, not one per ticket: ordered so the FIRST note
    # seen per ticket_id in the loop below is that ticket's most recent —
    # a ticket reopened and closed again keeps every past TicketClosingNote
    # (see that model's own docstring), so "most recent" is what actually
    # explains the CURRENT status, not an arbitrary one.
    latest_closing_note_by_ticket = {}
    for note in (
        TicketClosingNote.objects.filter(ticket_id__in=[t.pk for t in closed_today])
        .select_related('created_by').order_by('ticket_id', '-created_at')
    ):
        latest_closing_note_by_ticket.setdefault(note.ticket_id, note)
    for t in closed_today:
        t.closing_note = latest_closing_note_by_ticket.get(t.pk)

    closed_reactive = [t for t in closed_today if t.source != Ticket.Source.RECURRING]
    closed_recurring = [t for t in closed_today if t.source == Ticket.Source.RECURRING]

    return {
        'updated_notes': updated_notes,
        'closed_reactive': closed_reactive,
        'closed_recurring': closed_recurring,
    }


AUTOMATIC = 'Automatic (email, calendar, booking feeds)'
NOT_RECORDED = 'Not recorded (vendor link or automatic)'


def ticket_flow(days=30, now=None):
    """How tickets have been opening and closing, for the owner dashboard: who opened and who closed how many in the last `days` days, and the number
    of tickets still open at the end of each of those days (the line that shows work piling up or being cleared).

    Opened by = Ticket.created_by (blank for anything an email, calendar, booking feed or a cleaner's own link made: shown as one automatic row).
    Closed by = the person on the ticket's latest closing note when it reached a final status (completed, verified, cancelled, skipped, not
    applicable); with no closing note on record (a vendor's completion link, or an automatic cancel) it is "not recorded". A ticket counts as closed on
    the day of that closing note, else its completed/cancelled time. Tickets made by the recurring schedules or sessions, and ones still "upcoming", are left out:
    this is about work people actually opened and closed. A ticket reopened after it was closed counts only as open from then (the history of the
    reopening is not kept), so the line is accurate for today and close to right for the days before."""
    now = now or timezone.now()
    today = timezone.localdate(now)
    first = today - timedelta(days=days - 1)
    start = timezone.make_aware(datetime.combine(first, time.min))
    latest_note = TicketClosingNote.objects.filter(ticket=OuterRef('pk'), status__in=COMPLETE_STATUSES).order_by('-created_at')
    rows = (
        Ticket.objects.exclude(source__in=[Ticket.Source.RECURRING, Ticket.Source.SESSION]).exclude(status=Ticket.Status.UPCOMING)
        .annotate(note_at=Subquery(latest_note.values('created_at')[:1]), note_by=Subquery(latest_note.values('created_by')[:1]))
        .annotate(closed_ts=Coalesce('note_at', 'completed_at', 'cancelled_at', 'status_changed_at', 'updated_at'))
        .filter(created_at__lte=now)
        .filter(Q(status__in=OPEN_STATUSES) | Q(closed_ts__gte=start))
        .values_list('created_at', 'status', 'closed_ts', 'created_by_id', 'note_by')
    )
    delta = [0] * (days + 1)
    opened_day, closed_day = [0] * days, [0] * days
    opened_by, closed_by = {}, {}
    local = timezone.localtime
    for created, status, closed_ts, creator, closer in rows:
        is_closed = status in COMPLETE_STATUSES
        si = (local(created).date() - first).days
        if created >= start:
            opened_by[creator] = opened_by.get(creator, 0) + 1
            if si < days:
                opened_day[max(si, 0)] += 1
        ci = None
        if is_closed:
            ci = (local(closed_ts).date() - first).days
            if closed_ts >= start and closed_ts <= now:
                closed_by[closer] = closed_by.get(closer, 0) + 1
                closed_day[min(max(ci, 0), days - 1)] += 1
        s = max(si, 0)
        e = days if ci is None else min(ci, days)
        if s < days and e > s:
            delta[s] += 1
            delta[e] -= 1
    series, running = [], 0
    for i in range(days):
        running += delta[i]
        day = first + timedelta(days=i)
        series.append({'date': day, 'open': running, 'opened': opened_day[i], 'closed': closed_day[i]})

    User = get_user_model()
    names = {u.pk: (u.get_full_name() or u.username) for u in User.objects.filter(pk__in=[k for k in set(opened_by) | set(closed_by) if k])}
    named = sorted(
        ({'name': names[key], 'opened': opened_by.get(key, 0), 'closed': closed_by.get(key, 0)} for key in (set(opened_by) | set(closed_by)) if key in names),
        key=lambda p: (-(p['opened'] + p['closed']), p['name'].lower()),
    )
    tail = []
    if opened_by.get(None):
        tail.append({'name': AUTOMATIC, 'opened': opened_by[None], 'closed': 0})
    if closed_by.get(None):
        tail.append({'name': NOT_RECORDED, 'opened': 0, 'closed': closed_by[None]})
    people = named + tail

    top = max([p['open'] for p in series] + [1])
    ymax = max(5, ((top * 11 // 10) // 5 + 1) * 5)
    width, height, left, right, upper, lower = 720, 230, 42, 14, 14, 30
    plot_w, plot_h = width - left - right, height - upper - lower
    step = plot_w / max(days - 1, 1)
    xy = lambda i, v: (round(left + i * step, 1), round(upper + plot_h - (v / ymax) * plot_h, 1))
    points = [xy(i, p['open']) for i, p in enumerate(series)]
    chart = {
        'width': width, 'height': height, 'left': left, 'right': width - right, 'bottom': upper + plot_h, 'top': upper,
        'line': ' '.join(f'{x},{y}' for x, y in points),
        'dots': [{'x': x, 'y': y, 'open': p['open'], 'date': p['date'], 'opened': p['opened'], 'closed': p['closed']} for (x, y), p in zip(points, series)],
        'xticks': [{'x': points[i][0], 'label': series[i]['date'].strftime('%b ') + str(series[i]['date'].day)} for i in range(0, days, 5)] + [{'x': points[-1][0], 'label': 'Today'}],
        'yticks': [{'y': xy(0, v)[1], 'label': v} for v in sorted({0, ymax // 2, ymax})],
    }
    return {
        'days': days, 'series': series, 'people': people, 'chart': chart,
        'opened': sum(opened_by.values()), 'closed': sum(closed_by.values()), 'open_now': series[-1]['open'] if series else 0,
        'open_then': series[0]['open'] if series else 0,
    }


def gone_quiet(now=None):
    """Panel 6 — last on the page, never urgent. Four conditions, each
    computed in the database (Subquery/Exists), not per-row queries in a
    Python loop:
    - no activity (max of updated_at / latest status note / latest
      reassignment) in OWNER_DASHBOARD_QUIET_DAYS, excluding statuses
      that are SUPPOSED to sit (upcoming, deferred) and already-closed work
      — AND due (or overdue) within OWNER_DASHBOARD_QUIET_DUE_WITHIN_DAYS.
      Reported directly: a ticket genuinely not due for two months sitting
      untouched for a week is normal, not quiet — nothing is "supposed to
      be worked on" yet, so staleness alone isn't a signal for it. A
      ticket with no due date at all is handled by its own separate
      condition below instead, never this one.
    - blocked beyond OWNER_DASHBOARD_BLOCKED_QUIET_DAYS — its own bucket
      since a normally-blocked ticket sitting 30+ days is the worst thing
      on the board and appears nowhere else (unconditional on due_date —
      a blocked ticket is actively supposed to be moving regardless of
      how far off its own due date is)
    - auto-assigned and the assignee has never opened it (TicketView)
    - no due date and no recent activity
    A ticket can match more than one condition; every match is kept so the
    template can show all of them."""
    now = now or timezone.now()
    quiet_cutoff = now - timedelta(days=settings.OWNER_DASHBOARD_QUIET_DAYS)
    blocked_cutoff = now - timedelta(days=settings.OWNER_DASHBOARD_BLOCKED_QUIET_DAYS)
    quiet_due_within_cutoff = now + timedelta(days=settings.OWNER_DASHBOARD_QUIET_DUE_WITHIN_DAYS)

    last_note_sq = (
        TicketStatusNote.objects.filter(ticket=OuterRef('pk'))
        .order_by().values('ticket').annotate(m=Max('created_at')).values('m')
    )
    last_assignment_sq = (
        TicketAssignmentLog.objects.filter(ticket=OuterRef('pk'))
        .order_by().values('ticket').annotate(m=Max('changed_at')).values('m')
    )
    viewed_by_assignee_sq = TicketView.objects.filter(ticket=OuterRef('pk'), user_id=OuterRef('assigned_staff__user_id'))

    qs = (
        Ticket.objects.filter(property__isnull=False)
        .exclude(status__in=[Ticket.Status.UPCOMING, Ticket.Status.DEFERRED])
        .exclude(status__in=COMPLETE_STATUSES)
        .annotate(
            last_note_at=Subquery(last_note_sq),
            last_assignment_at=Subquery(last_assignment_sq),
            viewed_by_assignee=Exists(viewed_by_assignee_sq),
        )
        .annotate(
            last_activity_at=Greatest(
                Coalesce('last_note_at', 'updated_at'),
                Coalesce('last_assignment_at', 'updated_at'),
                'updated_at',
            ),
        )
        .select_related('property', 'assigned_staff__user', 'assigned_contact')
    )

    rows = []
    for t in qs:
        reasons = []
        if t.status == Ticket.Status.BLOCKED:
            since = t.status_changed_at or t.created_at
            if since <= blocked_cutoff:
                reasons.append({'reason': 'blocked_long', 'since': since})
        elif t.last_activity_at <= quiet_cutoff and t.due_date and t.due_date <= quiet_due_within_cutoff:
            reasons.append({'reason': 'no_activity', 'since': t.last_activity_at})

        if t.assignment_source == Ticket.AssignmentSource.AUTO and t.assigned_staff_id and not t.viewed_by_assignee:
            reasons.append({'reason': 'auto_untouched', 'since': None})

        if not t.due_date and t.last_activity_at <= quiet_cutoff:
            reasons.append({'reason': 'no_due_no_activity', 'since': t.last_activity_at})

        if reasons:
            rows.append({'ticket': t, 'reasons': reasons})

    return rows
