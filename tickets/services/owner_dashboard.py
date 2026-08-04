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
from datetime import timedelta

from django.conf import settings
from django.db.models import Exists, Max, OuterRef, Subquery
from django.db.models.functions import Coalesce, Greatest
from django.utils import timezone

from ..models import Priority, Ticket, TicketAssignmentLog, TicketStatusNote, TicketTemplate, TicketView

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
        Booking.objects.filter(check_out__date=tomorrow, status=Booking.Status.ACTIVE)
        .select_related('property')
        .prefetch_related('visits')
    )
    unassigned_tomorrow_checkouts = [
        b for b in tomorrow_checkouts
        if not any(v.status not in (Visit.Status.CANCELLED, Visit.Status.SKIPPED) for v in b.visits.all())
    ]

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

    return {
        'unassigned_tomorrow_checkouts': unassigned_tomorrow_checkouts,
        'todays_visits': todays_visits,
        'feed_health': feed_rows,
    }


def recurring_rules_drifting(lookback=5):
    """Panel 3 — the unit of attention is the rule (TicketTemplate), not
    the instance; a healthy rule (every recent run completed on time)
    doesn't appear at all. One query for every relevant ticket across
    every active template, grouped in Python afterward, rather than one
    query per template — the latter is exactly the per-row-loop pattern
    the brief's perf warning is about."""
    today = timezone.localdate()
    templates = list(TicketTemplate.objects.filter(is_active=True))
    template_ids = [t.pk for t in templates]

    recent_by_template = {}
    tickets = (
        Ticket.objects.filter(created_from_template_id__in=template_ids)
        .select_related('property')
        .order_by('created_from_template_id', '-scheduled_for')
    )
    for t in tickets:
        bucket = recent_by_template.setdefault(t.created_from_template_id, [])
        if len(bucket) < lookback:
            bucket.append(t)

    rows = []
    for template in templates:
        recent = recent_by_template.get(template.pk, [])
        if not recent:
            rows.append({'template': template, 'runs': [], 'status': 'never_completed'})
            continue

        runs = []
        for t in recent:
            if t.status in (Ticket.Status.COMPLETED, Ticket.Status.VERIFIED):
                late = bool(t.completed_at and t.due_date and t.completed_at > t.due_date)
                outcome = 'late' if late else 'completed'
            elif t.status in (Ticket.Status.SKIPPED, Ticket.Status.CANCELLED, Ticket.Status.NOT_APPLICABLE):
                outcome = 'skipped'
            elif t.due_date and timezone.localtime(t.due_date).date() < today:
                outcome = 'late'
            else:
                outcome = 'pending'
            runs.append({'ticket': t, 'outcome': outcome})

        outcomes = [r['outcome'] for r in runs]
        if 'skipped' in outcomes:
            status = 'skipping'
        elif not any(o == 'completed' for o in outcomes):
            status = 'never_completed'
        elif 'late' in outcomes:
            status = 'late'
        else:
            status = 'healthy'

        if status != 'healthy':
            rows.append({'template': template, 'runs': runs, 'status': status})

    return rows


def movement_today():
    """Panel 5 — updated notes (actual text + author, not just "something
    changed") and closed-today counted separately by reactive vs.
    recurring, so a morning of ticked recurring checkboxes doesn't inflate
    the one number meant to prove real reactive work got done."""
    today = timezone.localdate()

    updated_notes = list(
        TicketStatusNote.objects.filter(created_at__date=today)
        .select_related('ticket', 'ticket__property', 'created_by')
        .order_by('-created_at')
    )

    closed_today = (
        Ticket.objects.filter(status__in=Ticket.TRUE_COMPLETION_STATUSES, completed_at__date=today)
        .select_related('property')
    )
    closed_reactive = [t for t in closed_today if t.source != Ticket.Source.RECURRING]
    closed_recurring = [t for t in closed_today if t.source == Ticket.Source.RECURRING]

    return {
        'updated_notes': updated_notes,
        'closed_reactive': closed_reactive,
        'closed_recurring': closed_recurring,
    }


def gone_quiet(now=None):
    """Panel 6 — last on the page, never urgent. Four conditions, each
    computed in the database (Subquery/Exists), not per-row queries in a
    Python loop:
    - no activity (max of updated_at / latest status note / latest
      reassignment) in OWNER_DASHBOARD_QUIET_DAYS, excluding statuses
      that are SUPPOSED to sit (upcoming, deferred) and already-closed work
    - blocked beyond OWNER_DASHBOARD_BLOCKED_QUIET_DAYS — its own bucket
      since a normally-blocked ticket sitting 30+ days is the worst thing
      on the board and appears nowhere else
    - auto-assigned and the assignee has never opened it (TicketView)
    - no due date and no recent activity
    A ticket can match more than one condition; every match is kept so the
    template can show all of them."""
    now = now or timezone.now()
    quiet_cutoff = now - timedelta(days=settings.OWNER_DASHBOARD_QUIET_DAYS)
    blocked_cutoff = now - timedelta(days=settings.OWNER_DASHBOARD_BLOCKED_QUIET_DAYS)

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
        elif t.last_activity_at <= quiet_cutoff:
            reasons.append({'reason': 'no_activity', 'since': t.last_activity_at})

        if t.assignment_source == Ticket.AssignmentSource.AUTO and t.assigned_staff_id and not t.viewed_by_assignee:
            reasons.append({'reason': 'auto_untouched', 'since': None})

        if not t.due_date and t.last_activity_at <= quiet_cutoff:
            reasons.append({'reason': 'no_due_no_activity', 'since': t.last_activity_at})

        if reasons:
            rows.append({'ticket': t, 'reasons': reasons})

    return rows
