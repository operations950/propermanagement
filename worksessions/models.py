"""Session-based recurring office work — replaces the per-property-per-
occurrence system in tickets/models.py (TicketTemplate + Ticket(source=
'recurring')) for routine work that isn't naturally one ticket per instance.
See the "Recurring work overhaul — sessions" build brief.

A SessionTemplate opens a Session on a cadence. A Session holds SessionLines
— one per unit of work (a bank account, a property, a fixed checklist item)
— that get marked done/skipped/not_applicable and submitted together.
Fan-out moves from tickets to lines: twenty properties produce twenty lines
inside one Session, not twenty Tickets. Sessions don't create tickets by
default; a line that goes wrong is *promoted* to a real Ticket (see
sessions/services/lifecycle.py::promote_to_ticket), mirroring how onsite's
VisitIssue becomes a Ticket on submit.

Deliberately parallel to — and sharing zero code with — the onsite app's
Visit/VisitChecklistItem shape (materialize-once, submit-time gate,
promote-on-exception). No shared abstraction is extracted at this stage:
onsite's Visit carries property/booking/cleaner-auth semantics this app has
none of, and merging would drag those into office work that doesn't need
them.

Explicit departures from the old recurring system, each carried over from
findings in this app's own investigation report:
- No skip_missed-style flag. Sessions are period-scoped; an unsubmitted
  August session is still a real obligation when September opens, so
  generation always creates the missed period's Session rather than
  fast-forwarding past it (see sessions/services/generation.py).
- Targeting (property_types/required_attributes) lives on SessionTemplate
  alone, with no group-level override chain layered on top of it the way
  TaskGroup.property_types used to silently override TicketTemplate's own
  target_type — one place decides which lines a template produces.
- SessionLine.skip_reason is enforced by a real CheckConstraint, not just a
  form-layer convention — Ticket.status_reason/REASON_REQUIRED_STATUSES was
  declared but never actually read by ticket_set_status, and that gap is
  exactly what let a skipped instance be indistinguishable from "the rule
  is noise" versus "the property was vacant."
"""
from django.core.exceptions import ValidationError
from django.db import models

from core.models import Property, PropertyAttribute, StaffProfile, Unit


class Frequency(models.TextChoices):
    DAILY = 'daily', 'Daily'
    WEEKLY = 'weekly', 'Weekly'
    BIWEEKLY = 'biweekly', 'Bi-weekly'
    MONTHLY = 'monthly', 'Monthly'
    MONTHLY_WORKDAY = 'monthly_workday', 'Monthly (by working day)'
    QUARTERLY = 'quarterly', 'Quarterly'
    YEARLY = 'yearly', 'Yearly'


class SessionTemplate(models.Model):
    """The rule: what opens, how often, who owns it, and what lines it
    produces. See sessions/services/generation.py for how it turns into
    actual Session rows."""

    class LineSource(models.TextChoices):
        STATIC = 'static', 'Static list'
        QUERY = 'query', 'Property query'

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)

    owner = models.ForeignKey(
        StaffProfile, on_delete=models.PROTECT, related_name='session_templates',
        help_text='The person this rule\'s sessions belong to — snapshotted onto Session.owner at '
                   'creation, so reassigning the template later only affects future sessions.',
    )
    department = models.CharField(
        max_length=20, choices=StaffProfile.Role.choices,
        help_text='Which queue this rule\'s sessions file under for filtering/dashboards — independent '
                   'of owner (a person\'s own StaffProfile.role may differ from the department their '
                   'session work should be grouped under).',
    )

    # --- Cadence ---
    frequency = models.CharField(max_length=20, choices=Frequency.choices)
    workday_of_month = models.PositiveSmallIntegerField(
        null=True, blank=True,
        help_text='Only used when frequency is "Monthly (by working day)" — e.g. 3 means the 3rd '
                   'Mon-Fri business day of the month. Weekends are skipped; holidays are not '
                   'currently accounted for (no holiday calendar configured) — a rule using this '
                   'frequency can land on a recognized holiday.',
    )
    next_open_date = models.DateField(help_text='The next date a session should open for this rule.')
    due_offset_days = models.PositiveSmallIntegerField(
        default=0, help_text='How many days after opening a session is due — 0 means due the same day.',
    )
    active_from = models.DateField(
        null=True, blank=True, help_text='No sessions open before this date. Blank = no start limit.',
    )
    active_until = models.DateField(
        null=True, blank=True, help_text='No sessions open after this date. Blank = no end limit — '
                                          'use this for seasonal rules (e.g. snowbird season) instead '
                                          'of deactivating and reactivating the rule by hand.',
    )
    is_active = models.BooleanField(default=True)

    # --- Lines (see sessions/services/generation.py::materialize_lines) ---
    # Targeting lives here and only here — no group-level override layered on
    # top, unlike the old TicketTemplate/TaskGroup.property_types chain.
    line_source = models.CharField(max_length=10, choices=LineSource.choices, default=LineSource.STATIC)
    property_types = models.JSONField(
        default=list, blank=True,
        help_text='Only used when Line source is "Property query" — Property.Type codes a property '
                   'must match (e.g. ["str", "commercial"]). Empty = every active property type.',
    )
    required_attributes = models.ManyToManyField(
        PropertyAttribute, blank=True, related_name='required_by_session_templates',
        help_text='Only used when Line source is "Property query" — a matching property must have '
                   'ALL of these tags. Empty = no constraint.',
    )
    query_by_unit = models.BooleanField(
        default=False,
        help_text='Only used when Line source is "Property query" — when a matching property has '
                   'Units, generate one line per Unit instead of one line for the whole property (a '
                   'matching property with no units still gets a single property-level line either '
                   'way). Off by default so existing property-query rules keep their exact current '
                   'behavior unchanged.',
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'{self.name} ({self.get_frequency_display()})'


class SessionTemplateLine(models.Model):
    """One fixed line for a Line source="static" template (e.g. one bank
    account in a monthly bookkeeping session). Materialized onto each new
    Session's SessionLine rows at creation — never re-read afterward, so
    editing this list only ever affects sessions opened after the edit."""
    template = models.ForeignKey(SessionTemplate, on_delete=models.CASCADE, related_name='static_lines')
    label = models.CharField(max_length=200)
    display_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['display_order', 'id']

    def __str__(self):
        return self.label


class Session(models.Model):
    """One opened instance of a SessionTemplate for one period — the
    container staff actually work in. Lines are materialized once, at
    creation (see sessions/services/generation.py); editing the template
    afterward never touches an already-open or already-submitted Session."""

    class Status(models.TextChoices):
        OPEN = 'open', 'Open'
        SUBMITTED = 'submitted', 'Submitted'

    template = models.ForeignKey(SessionTemplate, on_delete=models.PROTECT, related_name='sessions')
    owner = models.ForeignKey(
        StaffProfile, on_delete=models.SET_NULL, null=True, related_name='sessions',
        help_text='Snapshotted from template.owner at creation.',
    )
    department = models.CharField(
        max_length=20, choices=StaffProfile.Role.choices, blank=True,
        help_text='Snapshotted from template.department at creation.',
    )

    period_label = models.CharField(
        max_length=100, help_text='Human display, e.g. "August 2026" or "Tue 4 Aug" — never parsed.',
    )
    period_key = models.DateField(
        help_text='The occurrence date this session represents — the real identity of "which period," '
                   'compared for uniqueness below. period_label is just its display form.',
    )
    opens_at = models.DateField()
    due_at = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    submitted_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-opens_at']
        constraints = [
            # The idempotency backbone for generation — a real, always-non-null
            # pair (unlike the old uniq_template_scheduled_for_property, whose
            # nullable `property` column meant NULL != NULL silently defeated
            # the constraint for COMPANY-target rows). This one is enforced by
            # Postgres for every row, regardless of how many processes are
            # racing to create it.
            models.UniqueConstraint(fields=['template', 'period_key'], name='uniq_session_template_period'),
        ]

    def __str__(self):
        return f'{self.template.name} — {self.period_label}'

    def progress(self):
        """(done_or_resolved, total) — reads self.lines, no extra query if
        already prefetched."""
        lines = list(self.lines.all())
        if not lines:
            return 0, 0
        resolved = sum(1 for line in lines if line.state != SessionLine.State.PENDING)
        return resolved, len(lines)

    def is_overdue(self, today=None):
        from django.utils import timezone
        today = today or timezone.localdate()
        return self.status == self.Status.OPEN and bool(self.due_at) and self.due_at < today


class SessionLine(models.Model):
    """One unit of work inside a Session. No assignee of its own — every
    line is owned by the session's owner; work that genuinely needs
    splitting across people is two sessions, not a per-line assignee (see
    the build brief's "Do not" list)."""

    class State(models.TextChoices):
        PENDING = 'pending', 'Pending'
        DONE = 'done', 'Done'
        SKIPPED = 'skipped', 'Skipped'
        NOT_APPLICABLE = 'not_applicable', 'Not applicable'

    RESOLVED_STATES = [State.DONE, State.SKIPPED, State.NOT_APPLICABLE]
    REASON_REQUIRED_STATES = [State.SKIPPED, State.NOT_APPLICABLE]

    session = models.ForeignKey(Session, on_delete=models.CASCADE, related_name='lines')
    label = models.CharField(max_length=200)
    property = models.ForeignKey(
        Property, on_delete=models.SET_NULL, null=True, blank=True, related_name='session_lines',
        help_text='Set only for a query-driven line — the property it was generated for.',
    )
    unit = models.ForeignKey(
        Unit, on_delete=models.SET_NULL, null=True, blank=True, related_name='session_lines',
        help_text='Set only for a query-driven line generated per-unit rather than per-property — see '
                   'SessionTemplate.line_source and generation.py::matching_targets.',
    )
    display_order = models.PositiveSmallIntegerField(default=0)

    state = models.CharField(max_length=20, choices=State.choices, default=State.PENDING)
    completed_at = models.DateTimeField(null=True, blank=True)
    skip_reason = models.CharField(
        max_length=300, blank=True,
        help_text='Required whenever state is Skipped or Not applicable — without one, a skipped line '
                   'can\'t be told apart from "the rule is noise" versus "the property was vacant," '
                   'and every drift signal downstream depends on that distinction.',
    )
    notes = models.TextField(blank=True)

    promoted_ticket = models.ForeignKey(
        'tickets.Ticket', on_delete=models.SET_NULL, null=True, blank=True, related_name='promoted_from_session_line',
        help_text='Set when this line was promoted to a real Ticket because something went wrong — '
                   'the exception path, mirroring onsite.VisitIssue.created_ticket. Sessions do not '
                   'create tickets by default.',
    )

    class Meta:
        ordering = ['display_order', 'id']
        constraints = [
            # Enforced at the database level, not just the form — the exact
            # thing Ticket.status_reason/REASON_REQUIRED_STATUSES failed to
            # do (declared as required, never actually checked by
            # ticket_set_status or rendered in the template). No code path —
            # admin, bulk update, a future view nobody remembers to gate —
            # can create a reason-less skip here.
            models.CheckConstraint(
                condition=(
                    ~models.Q(state__in=['skipped', 'not_applicable']) | ~models.Q(skip_reason='')
                ),
                name='session_line_skip_reason_required',
            ),
        ]

    def __str__(self):
        return self.label

    def clean(self):
        if self.state in self.REASON_REQUIRED_STATES and not self.skip_reason.strip():
            raise ValidationError({'skip_reason': 'A reason is required to skip or mark a line not applicable.'})
