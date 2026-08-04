import uuid
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from core.models import Contact, Property, PropertyAttribute, StaffProfile


class Priority(models.TextChoices):
    LOW = 'low', 'Low'
    MEDIUM = 'medium', 'Medium'
    HIGH = 'high', 'High'
    URGENT = 'urgent', 'Urgent'


class Frequency(models.TextChoices):
    DAILY = 'daily', 'Daily'
    WEEKLY = 'weekly', 'Weekly'
    BIWEEKLY = 'biweekly', 'Bi-weekly'
    MONTHLY = 'monthly', 'Monthly'
    MONTHLY_WORKDAY = 'monthly_workday', 'Monthly (by working day)'
    QUARTERLY = 'quarterly', 'Quarterly'
    YEARLY = 'yearly', 'Yearly'


class TicketTemplate(models.Model):
    """Definition for a recurring proactive task.

    Most frequencies step next_run_date forward by a fixed interval
    (relativedelta) — see generate_recurring_tickets. MONTHLY_WORKDAY is
    different: real ops schedules ("Working Day 3 of the month") are
    business-day-of-month, not a fixed date, and the actual calendar date
    shifts every month depending on where weekends fall — so it's computed
    fresh each month via workday_of_month instead of a date increment.
    """

    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    property = models.ForeignKey(
        Property, on_delete=models.CASCADE, null=True, blank=True, related_name='ticket_templates',
        help_text='Only used when Target type is "Specific property".',
    )
    kind = models.CharField(max_length=20, default='generic')
    frequency = models.CharField(max_length=20, choices=Frequency.choices)
    workday_of_month = models.PositiveSmallIntegerField(
        null=True, blank=True,
        help_text='Only used when frequency is "Monthly (by working day)" — e.g. 3 means the 3rd '
                   'Mon–Fri business day of the month (weekends skipped, holidays not currently '
                   'accounted for).',
    )
    next_run_date = models.DateField(help_text='The next date this task should be generated for.')
    default_assigned_role = models.CharField(
        max_length=20, choices=StaffProfile.Role.choices, default=StaffProfile.Role.PROPERTY_MANAGER,
        help_text='Every recurring ticket belongs to a department; a specific person is optional on top of that.',
    )
    default_assigned_staff = models.ForeignKey(
        StaffProfile, on_delete=models.SET_NULL, null=True, blank=True, related_name='ticket_templates',
    )
    default_priority = models.CharField(max_length=20, choices=Priority.choices, default=Priority.MEDIUM)
    is_active = models.BooleanField(default=True)
    skip_missed = models.BooleanField(
        default=False,
        help_text='If the scheduler was down and occurrences were missed, jump straight to the next '
                   'future occurrence instead of backfilling every missed one.',
    )

    # --- Applicability rules (see tickets.services.applicability) ---
    # `target_type` is the authoritative dispatch key for which of the fields
    # below matter — see template_applies_to_property. `property`/
    # `property_types`/`required_attributes`/`contact` are the payload for
    # whichever target_type is selected; changing target_type does not clear
    # the others (so switching back doesn't lose what was entered).
    class TargetType(models.TextChoices):
        EVERY_PROPERTY = 'every_property', 'Every property'
        PROPERTY_CATEGORY = 'property_category', 'Property category'
        PROPERTY = 'property', 'Specific property'
        CONTACT = 'contact', 'Contact'
        COMPANY = 'company', 'Company-wide (no property)'
        # DEPARTMENT: a documented future value — no "which properties does a
        # department own" relationship exists in the schema yet to resolve it
        # against, so it's not implemented until a real use case defines one.

    target_type = models.CharField(
        max_length=20, choices=TargetType.choices, default=TargetType.EVERY_PROPERTY,
        help_text='What this rule applies to.',
    )
    property_types = models.JSONField(
        default=list, blank=True,
        help_text='Only used when Target type is "Property category" — Property.Type codes this '
                   'applies to (e.g. ["str", "commercial"]). JSONField (not ArrayField) so this works '
                   'on SQLite dev and Postgres prod alike.',
    )
    required_attributes = models.ManyToManyField(
        PropertyAttribute, blank=True, related_name='required_by_templates',
        help_text='Property must have ALL of these tags for this template to auto-apply. Empty = no '
                   'constraint. Layered on top of "Every property" or "Property category" targeting.',
    )
    contact = models.ForeignKey(
        Contact, on_delete=models.SET_NULL, null=True, blank=True, related_name='targeted_by_templates',
        help_text='Only used when Target type is "Contact" — generates this task for every property '
                   'currently linked to this contact (Contact.properties).',
    )
    lead_time_days = models.PositiveSmallIntegerField(
        default=0, help_text='Generate the instance this many days before it\'s due, in status Upcoming.',
    )
    requires_approval = models.BooleanField(
        default=False, help_text='Completing an instance moves it to Completed (submitted); a staff '
                                  'member with approval_role must then approve it to reach Verified.',
    )
    approval_role = models.CharField(max_length=20, choices=StaffProfile.Role.choices, blank=True)
    escalation_threshold_days = models.PositiveSmallIntegerField(
        null=True, blank=True,
        help_text='Flag (not reassign) an instance once it is overdue by this many days.',
    )

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.title} ({self.get_frequency_display()})'


class TemplateChecklistItem(models.Model):
    """A checklist item on a template's definition. Copied onto each
    generated Ticket as a TicketChecklistItem — never referenced live — so
    editing a template's checklist later never mutates an already-completed
    historical instance."""
    template = models.ForeignKey(TicketTemplate, on_delete=models.CASCADE, related_name='checklist_items')
    text = models.CharField(max_length=300)
    sequence_order = models.PositiveSmallIntegerField(default=0)
    is_required = models.BooleanField(default=True)

    class Meta:
        ordering = ['sequence_order']

    def __str__(self):
        return self.text


class TicketTemplateDocument(models.Model):
    """A staff-uploaded reference document for a recurring task rule (e.g.
    a vendor contract, a checklist PDF, instructions) — same shape as
    core.models.PropertyDocument/ContactDocument."""
    template = models.ForeignKey(TicketTemplate, on_delete=models.CASCADE, related_name='documents')
    name = models.CharField(max_length=200)
    file = models.FileField(upload_to='ticket_template_documents/%Y/%m/')
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.name} — {self.template}'


class TaskPackage(models.Model):
    """A reusable, admin-authored bundle of TicketTemplates attachable to a
    property (e.g. "STR Base Package") — see PropertyPackage. Steps may
    optionally be dependency-ordered (see TaskPackageTemplate.depends_on),
    which is what makes the same model also cover a "Recurring Process"
    like "Monthly Accounting Close": the only structural difference is
    whether a step has a depends_on set, not a different kind of object."""
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    department = models.CharField(
        max_length=20, choices=StaffProfile.Role.choices, default='',
        help_text='Which single department this Function belongs to — its steps normally all share one '
                   'default_assigned_role already; this makes that explicit rather than implied.',
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['title']

    def __str__(self):
        return self.title


class TaskGroup(models.Model):
    """An optional sub-bucket of steps within a Function (TaskPackage) — a
    Function may organize its recurring tasks into one or more named groups,
    or skip grouping entirely and hang tasks directly off the Function (see
    TaskPackageTemplate.task_group, nullable for exactly that reason).

    property_types is the broad-group targeting set once for the whole
    group ("Short-Term Rentals") instead of on every step individually —
    see tickets.services.applicability.template_applies_to_property, which
    only defers to this when it's non-empty. Left empty, a step's own
    TicketTemplate.target_type still governs it exactly as before, so
    existing groups (created before this field existed) keep behaving
    identically until someone opts in."""
    package = models.ForeignKey(TaskPackage, on_delete=models.CASCADE, related_name='task_groups')
    title = models.CharField(max_length=200)
    property_types = models.JSONField(
        default=list, blank=True,
        help_text='Broad property category this group\'s tasks apply to (e.g. Short-Term Rentals). '
                   'Leave empty to have each task in this group use its own Target settings instead.',
    )
    sequence_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['sequence_order']

    def __str__(self):
        return f'{self.package} — {self.title}'


class TaskPackageTemplate(models.Model):
    package = models.ForeignKey(TaskPackage, on_delete=models.CASCADE, related_name='steps')
    task_group = models.ForeignKey(
        TaskGroup, on_delete=models.SET_NULL, null=True, blank=True, related_name='steps',
        help_text='Optional — leave blank for a task that hangs directly off the Function rather '
                   'than one of its Task Groups. Deleting the group ungroups its tasks rather than '
                   'deleting them.',
    )
    template = models.ForeignKey(TicketTemplate, on_delete=models.CASCADE, related_name='package_memberships')
    sequence_order = models.PositiveSmallIntegerField(default=0)
    depends_on = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True, related_name='dependents',
        help_text='If set, generated instances of this step start Blocked until the referenced '
                   'step\'s instance (same property, same period) reaches a completed-like status.',
    )

    class Meta:
        unique_together = [('package', 'template')]
        ordering = ['sequence_order']

    def __str__(self):
        return f'{self.package} — {self.template}'


class PropertyPackage(models.Model):
    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='packages')
    package = models.ForeignKey(TaskPackage, on_delete=models.CASCADE, related_name='property_assignments')
    assigned_at = models.DateTimeField(auto_now_add=True)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        unique_together = [('property', 'package')]

    def __str__(self):
        return f'{self.property} — {self.package}'


class PropertyTemplateOverride(models.Model):
    """A property-specific exception to a template's normal applicability —
    exclude it, force-include it, and/or change its frequency/role/assignee
    for this one property. One row type covers all three, since a
    modify-only row and an include-and-modify row need identical
    override-application logic (see tickets.services.applicability)."""
    class Action(models.TextChoices):
        EXCLUDE = 'exclude', 'Exclude from this property'
        INCLUDE = 'include', 'Include / adjust for this property'

    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='template_overrides')
    template = models.ForeignKey(TicketTemplate, on_delete=models.CASCADE, related_name='property_overrides')
    action = models.CharField(max_length=10, choices=Action.choices, default=Action.INCLUDE)
    frequency = models.CharField(max_length=20, choices=Frequency.choices, blank=True)
    workday_of_month = models.PositiveSmallIntegerField(null=True, blank=True)
    assigned_role = models.CharField(max_length=20, choices=StaffProfile.Role.choices, blank=True)
    assigned_staff = models.ForeignKey(
        StaffProfile, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    next_run_date = models.DateField(
        null=True, blank=True,
        help_text='Only used when frequency is overridden — this property then advances on its own '
                   'schedule instead of the template\'s shared cursor.',
    )
    note = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('property', 'template')]

    def __str__(self):
        return f'{self.property} — {self.template} ({self.get_action_display()})'


class TemplateOccurrence(models.Model):
    """Groups sibling Tickets generated from the same template for the same
    period, across every property it fanned out to — the parent for
    multi-property roll-up (e.g. "Monthly Financial Statements — May
    2026")."""
    template = models.ForeignKey(TicketTemplate, on_delete=models.CASCADE, related_name='occurrences')
    scheduled_for = models.DateField()

    class Meta:
        unique_together = [('template', 'scheduled_for')]

    def __str__(self):
        return f'{self.template.title} — {self.scheduled_for}'


class PackageRun(models.Model):
    """Groups sibling Tickets generated from different templates (steps)
    within the same package, for the same property and period — what
    dependency-gating (TaskPackageTemplate.depends_on) checks against. A
    property=None run is a company-wide package run."""
    package = models.ForeignKey(TaskPackage, on_delete=models.CASCADE, related_name='runs')
    property = models.ForeignKey(
        Property, on_delete=models.CASCADE, null=True, blank=True, related_name='package_runs',
    )
    scheduled_for = models.DateField()

    class Meta:
        unique_together = [('package', 'property', 'scheduled_for')]

    def __str__(self):
        return f'{self.package.title} — {self.property or "company-wide"} — {self.scheduled_for}'


class DepartmentDefaultAssignee(models.Model):
    """Who a ticket falls to when it's created for a department (`assigned_role`)
    but nobody specific was picked — one row per role, not per person, since
    a role maps to *a* default person, not the other way around (see
    Ticket.save()'s auto-assign-on-create logic). Deliberately its own small
    model rather than a field on StaffProfile: StaffProfile.role already
    means "which team this person is on," not "who leads that team."

    Every ticket should have a real person on it — auto-assigning from here
    at creation makes that true without staff having to pick someone for
    every single reactive/on-site-issue ticket. Ticket.assignment_source
    records when this happened so an auto-assigned-and-never-touched ticket
    can still surface as unowned work on the owner dashboard's quiet-list
    panel, rather than silently hiding behind a name nobody actually
    claimed."""
    role = models.CharField(max_length=20, choices=StaffProfile.Role.choices, unique=True)
    staff = models.ForeignKey(StaffProfile, on_delete=models.CASCADE, related_name='default_for_roles')

    class Meta:
        ordering = ['role']

    def __str__(self):
        return f'{self.get_role_display()} default → {self.staff}'


class Ticket(models.Model):
    class Status(models.TextChoices):
        OPEN = 'open', 'Open'
        ASSIGNED = 'assigned', 'Assigned'
        IN_PROGRESS = 'in_progress', 'In progress'
        BLOCKED = 'blocked', 'Blocked'
        UPCOMING = 'upcoming', 'Upcoming'
        # Set only by the public vendor-completion link (vendorportal.views) —
        # an outside party can never set COMPLETED directly. Deliberately in
        # OPEN_STATUSES (tickets/views.py) — a vendor's own claim of "done"
        # isn't the real thing until staff reviews and completes it.
        VENDOR_COMPLETE = 'vendor_complete', 'Vendor Complete'
        COMPLETED = 'completed', 'Completed'
        VERIFIED = 'verified', 'Verified'
        SKIPPED = 'skipped', 'Skipped'
        NOT_APPLICABLE = 'not_applicable', 'Not applicable'
        DEFERRED = 'deferred', 'Deferred'
        CANCELLED = 'cancelled', 'Cancelled'

    # Statuses that require a stated reason at the form/view layer (not DB-enforced, matching the
    # existing cancelled_reason convention which also isn't DB-enforced). Deferred is deliberately
    # NOT here — it's a due-date change, not a reason, see ticket_set_status's new_due_date handling.
    REASON_REQUIRED_STATUSES = ['blocked', 'skipped', 'not_applicable']

    # Statuses a package step must reach before dependents blocked on it are released — see
    # tickets.services.package_engine.unblock_dependents.
    DEPENDENCY_SATISFYING_STATUSES = ['completed', 'verified', 'skipped', 'not_applicable', 'cancelled']

    # The only statuses that mean "completed_at should be set" — narrower than
    # DEPENDENCY_SATISFYING_STATUSES/COMPLETE_STATUSES (tickets/views.py), which also
    # include cancelled/skipped/not_applicable: those are "stopped," not "finished," and
    # shouldn't count toward the owner dashboard's "closed today" panel. See save() below.
    TRUE_COMPLETION_STATUSES = ['completed', 'verified']

    class AssignmentSource(models.TextChoices):
        MANUAL = 'manual', 'Manual'
        AUTO = 'auto', 'Auto-assigned'

    class Source(models.TextChoices):
        MANUAL = 'manual', 'Manual'
        EMAIL = 'email', 'Email'
        QUO = 'quo', 'Phone (Quo)'
        CALENDAR = 'calendar', 'Calendar'
        AIRBNB = 'airbnb', 'Airbnb'
        VRBO = 'vrbo', 'VRBO'
        FAKE = 'fake', 'Simulated (dev)'
        RECURRING = 'recurring', 'Recurring template'
        ONSITE = 'onsite', 'On-site visit'

    title = models.CharField(max_length=200, help_text='A short, scannable headline — not a full sentence.')
    description = models.TextField(blank=True, help_text='One concise sentence. Full source context goes in raw_context.')
    raw_context = models.TextField(
        blank=True,
        help_text='Full original text (e.g. a Quo conversation transcript) — kept for reference on the '
                   'ticket detail page, never shown in list views.',
    )
    kind = models.CharField(max_length=20, default='generic')

    source = models.CharField(max_length=20, choices=Source.choices, default=Source.MANUAL)
    source_reference = models.CharField(
        max_length=200, blank=True,
        help_text='Stable external id (e.g. reservation confirmation code, email message id).',
    )

    property = models.ForeignKey(
        Property, on_delete=models.PROTECT, related_name='tickets', null=True, blank=True,
        help_text='Blank when the source (e.g. a shared Quo phone line) can\'t determine which '
                   'property this is about — staff assigns it manually.',
    )

    assigned_role = models.CharField(
        max_length=20, choices=StaffProfile.Role.choices, blank=True,
        help_text='The department/queue this ticket belongs to — the primary classification for '
                   'every ticket. Set automatically for reactive tickets when no specific person can '
                   'be determined yet; a specific assigned_staff can still be set alongside it once '
                   'someone claims it.',
    )
    assigned_staff = models.ForeignKey(
        StaffProfile, on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_tickets',
    )
    assigned_contact = models.ForeignKey(
        Contact, on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_tickets',
        help_text='Use for reassigning to an external vendor/contractor.',
    )
    assignment_source = models.CharField(
        max_length=10, choices=AssignmentSource.choices, default=AssignmentSource.MANUAL,
        help_text='Whether assigned_staff was picked by a human or filled in automatically from '
                   'DepartmentDefaultAssignee at creation because assigned_role had one and nobody '
                   'specific was chosen (see save()). Flipped back to "manual" the moment a human '
                   'reassigns it — this only ever means "auto and still exactly as auto left it."',
    )

    priority = models.CharField(max_length=20, choices=Priority.choices, default=Priority.MEDIUM)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)

    due_date = models.DateTimeField(null=True, blank=True)
    delayed = models.BooleanField(
        default=False,
        help_text='True once due_date has been pushed later via the Edit Due Date action — a lesser '
                   'flag than Overdue, meant to surface "we said we\'d push through this by X and '
                   'didn\'t" separately from genuinely missed work.',
    )
    previous_due_date = models.DateTimeField(
        null=True, blank=True,
        help_text='The due_date immediately before the most recent push-back — shown translucent/'
                   'struck-through next to the new date. Cleared (along with delayed) if due_date is '
                   'ever moved back to or before this value.',
    )

    created_from_template = models.ForeignKey(
        TicketTemplate, on_delete=models.SET_NULL, null=True, blank=True, related_name='generated_tickets',
    )
    scheduled_for = models.DateField(
        null=True, blank=True,
        help_text='For recurring tickets: the occurrence date this instance represents.',
    )
    template_occurrence = models.ForeignKey(
        TemplateOccurrence, on_delete=models.SET_NULL, null=True, blank=True, related_name='tickets',
        help_text='Groups this instance with its siblings generated from the same template for the '
                   'same period, across every property — always null for one-off tickets.',
    )
    package_run = models.ForeignKey(
        PackageRun, on_delete=models.SET_NULL, null=True, blank=True, related_name='tickets',
        help_text='Groups this instance with its sibling steps in the same task package run — always '
                   'null for one-off tickets.',
    )

    completion_token = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    completion_token_expires_at = models.DateTimeField(null=True, blank=True)

    resolution_notes = models.TextField(blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_reason = models.CharField(max_length=300, blank=True)
    status_reason = models.CharField(
        max_length=300, blank=True,
        help_text='Why this was Blocked / Skipped / Not applicable — see REASON_REQUIRED_STATUSES.',
    )

    possible_duplicate_of = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True, related_name='possible_duplicates',
        help_text='Set by intake/duplicate_classifier.py when a newly-created ticket looks like it '
                   'describes the same real-world issue as an already-open ticket at the same '
                   'property. Held in the Pending screen\'s "Possible duplicate" queue until a human '
                   'confirms or dismisses the match — never auto-merged or auto-cancelled.',
    )
    duplicate_reasoning = models.TextField(
        blank=True, help_text='Claude\'s reasoning for flagging possible_duplicate_of.',
    )

    followup_done = models.BooleanField(
        default=False,
        help_text='Set the first time any Follow-Up text or email successfully sends — never reset, '
                   'so it just means "someone has been contacted at least once," not "up to date."',
    )
    vendor_link_sent_at = models.DateTimeField(
        null=True, blank=True,
        help_text='Last time the vendor completion-form link was texted to the assigned contractor '
                   'from the Contractor Communication card — rate-limits that button to once per 24h.',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    status_changed_at = models.DateTimeField(
        null=True, blank=True,
        help_text='When status last changed — set automatically in save() (see from_db()). Powers '
                   '"Xd blocked"/"Xd since" duration displays on the owner dashboard, since a status '
                   'value alone (e.g. "blocked") has no inherent sense of how long it\'s been that way. '
                   "Null means never changed since creation — fall back to created_at.",
    )

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['source', 'source_reference', 'kind'],
                condition=~models.Q(source_reference='') & ~models.Q(status='cancelled'),
                name='uniq_active_ticket_source_ref_kind',
            ),
            models.UniqueConstraint(
                # NULL != NULL in a unique index, so this doesn't actually
                # protect COMPANY-target templates (property always NULL)
                # against duplicate generation the way it protects
                # real-property rows — idempotency for those rests entirely
                # on generate_recurring_tickets' get_or_create call inside
                # its own transaction.atomic() block. Same class of
                # single-process-only risk the app already accepts
                # elsewhere; not a practical concern at this scale.
                fields=['created_from_template', 'scheduled_for', 'property'],
                condition=models.Q(created_from_template__isnull=False),
                name='uniq_template_scheduled_for_property',
            ),
            models.CheckConstraint(
                # Tightened from "at most one" to "exactly one" — every
                # ticket has a real person on it now, staff or vendor, no
                # exceptions. Ticket.save() guarantees this holds before a
                # row ever reaches the DB (falls back to
                # DepartmentDefaultAssignee / any company-admin whenever
                # both would otherwise be empty); the migration that
                # tightened this constraint backfilled every pre-existing
                # row first, in the same migrate step, so it can never fail
                # on old data regardless of what a prior deploy's Procfile
                # backfill command did or didn't get to run.
                condition=(
                    (models.Q(assigned_staff__isnull=False) & models.Q(assigned_contact__isnull=True))
                    | (models.Q(assigned_staff__isnull=True) & models.Q(assigned_contact__isnull=False))
                ),
                name='ticket_exactly_one_assignee',
            ),
        ]

    def __str__(self):
        return self.title

    def assignee_label(self):
        if self.assigned_staff_id:
            label = str(self.assigned_staff)
        elif self.assigned_contact_id:
            label = f'{self.assigned_contact} (external)'
        else:
            label = None
        if self.assigned_role:
            role_label = self.get_assigned_role_display()
            return f'{role_label} — {label}' if label else f'{role_label} (unclaimed)'
        return label or 'Unassigned'

    def clean(self):
        if self.assigned_staff_id and self.assigned_contact_id:
            raise ValidationError('A ticket can be assigned to staff OR a vendor contact, not both.')

    def checklist_progress(self):
        """(done, total), or None if this ticket has no checklist — reads
        the (usually prefetched) checklist_items, no extra query when
        called after a .prefetch_related('checklist_items')."""
        items = list(self.checklist_items.all())
        if not items:
            return None
        return sum(1 for i in items if i.is_checked), len(items)

    def recurrence_label(self):
        """How often this proactive task recurs, for display next to it on
        a department dashboard — e.g. "Monthly · Workday 15". Blank for
        reactive tickets (no created_from_template)."""
        template = self.created_from_template
        if not template:
            return ''
        if template.frequency == Frequency.MONTHLY_WORKDAY and template.workday_of_month:
            return f'Monthly · Workday {template.workday_of_month}'
        return template.get_frequency_display()

    def rotate_completion_token(self):
        self.completion_token = uuid.uuid4()
        self.completion_token_expires_at = timezone.now() + timedelta(
            days=settings.VENDOR_TOKEN_EXPIRY_DAYS
        )

    def is_completion_token_valid(self):
        if self.completion_token_expires_at is None:
            return True
        return timezone.now() <= self.completion_token_expires_at

    @classmethod
    def from_db(cls, db, field_names, values):
        """Remembers the status this instance was actually loaded with, so
        save() can tell "status changed away from completed/verified this
        call" (→ clear completed_at) apart from "just re-saving the same
        completed ticket for an unrelated field" (→ leave it alone). A
        freshly-constructed Ticket() has no _loaded_status, which save()
        treats as "nothing to compare against, don't touch completed_at."
        """
        instance = super().from_db(db, field_names, values)
        instance._loaded_status = instance.status
        return instance

    def save(self, *args, **kwargs):
        if self.completion_token_expires_at is None:
            self.completion_token_expires_at = timezone.now() + timedelta(
                days=settings.VENDOR_TOKEN_EXPIRY_DAYS
            )

        # Every ticket needs a real person on it — deliberately unconditional,
        # not just on create. "Unassigned" isn't a state this app lets a
        # ticket sit in anymore (see the exactly-one CheckConstraint below):
        # a brand-new ticket nobody was assigned to yet, or an existing one
        # a human just cleared down to nobody via the quick-edit/reassign/
        # clear-contractor paths, both land here the same way. Falls back to
        # DepartmentDefaultAssignee for assigned_role, then any company-admin
        # StaffProfile as a last resort, and marks it 'auto' — matching the
        # brief's own framing: auto-assigned-and-never-touched is meant to
        # surface on the quiet-list panel, not disappear as a silent null.
        # Never overrides a human's actual pick — this only runs when BOTH
        # fields are already empty going into this save.
        if not self.assigned_staff_id and not self.assigned_contact_id:
            default = None
            if self.assigned_role:
                default = DepartmentDefaultAssignee.objects.filter(role=self.assigned_role).select_related('staff').first()
            staff = default.staff if default else StaffProfile.objects.filter(is_company_admin=True).first()
            if staff:
                self.assigned_staff = staff
                self.assignment_source = self.AssignmentSource.AUTO

        # completed_at clear-on-reopen: whichever of the several status-
        # changing views moved this ticket, if it left TRUE_COMPLETION_STATUSES
        # the old completion timestamp no longer means anything — a ticket
        # bounced back to in_progress/blocked/cancelled/etc. isn't "closed
        # today" anymore, however it originally got closed.
        loaded_status = getattr(self, '_loaded_status', None)
        status_just_changed = loaded_status is not None and loaded_status != self.status
        if status_just_changed and self.status not in self.TRUE_COMPLETION_STATUSES and self.completed_at:
            self.completed_at = None
        if status_just_changed:
            self.status_changed_at = timezone.now()

        super().save(*args, **kwargs)
        self._loaded_status = self.status


class TicketChecklistItem(models.Model):
    """Copied from TemplateChecklistItem at generation time (see
    generate_recurring_tickets) — a snapshot, not a live reference, so
    editing the template later never touches an already-generated instance."""
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name='checklist_items')
    text = models.CharField(max_length=300)
    sequence_order = models.PositiveSmallIntegerField(default=0)
    is_required = models.BooleanField(default=True)
    is_checked = models.BooleanField(default=False)
    checked_at = models.DateTimeField(null=True, blank=True)
    checked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )

    class Meta:
        ordering = ['sequence_order']

    def __str__(self):
        return self.text


class TicketContact(models.Model):
    class Role(models.TextChoices):
        REPORTER = 'reporter', 'Reporter (follow up here)'
        OWNER = 'owner', 'Owner'
        CONTRACTOR = 'contractor', 'Contractor'
        OTHER = 'other', 'Additional contact'

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name='ticket_contacts')
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name='ticket_links')
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.OTHER)

    class Meta:
        unique_together = [('ticket', 'contact', 'role')]

    def __str__(self):
        return f'{self.contact} on {self.ticket} ({self.role})'


class TicketAttachment(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name='attachments')
    file = models.FileField(upload_to='ticket_attachments/%Y/%m/')
    caption = models.CharField(max_length=200, blank=True)
    uploaded_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    uploaded_by_contact = models.ForeignKey(
        Contact, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text='Set when an external vendor uploaded this via the completion link.',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    VIDEO_EXTENSIONS = ('.mp4', '.mov', '.webm', '.m4v')
    IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp', '.heic', '.gif')

    def __str__(self):
        return self.caption or self.file.name

    @property
    def is_video(self):
        return self.file.name.lower().endswith(self.VIDEO_EXTENSIONS)

    @property
    def is_image(self):
        return self.file.name.lower().endswith(self.IMAGE_EXTENSIONS)

    @property
    def is_document(self):
        """Neither a photo nor a video — a PDF/Word/Excel-type upload from
        the general-purpose Documents card (as opposed to the photo/video
        gallery, or a Follow-Up compose's MMS/email image attachment)."""
        return not self.is_video and not self.is_image


class TicketAssignmentLog(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name='assignment_logs')
    from_staff = models.ForeignKey(
        StaffProfile, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    from_contact = models.ForeignKey(Contact, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    to_staff = models.ForeignKey(StaffProfile, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    to_contact = models.ForeignKey(Contact, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    changed_at = models.DateTimeField(auto_now_add=True)
    note = models.CharField(max_length=300, blank=True)
    previous_conversation_id = models.CharField(
        max_length=100, blank=True,
        help_text='Set when a contractor change resets Ticket.source_reference — the old Quo '
                   'conversation this ticket was bound to, so its history stays reachable (see '
                   'ticket_detail.html\'s audit trail) instead of just disappearing.',
    )

    class Meta:
        ordering = ['-changed_at']


class TicketStatusNote(models.Model):
    """A timestamped free-text update on a ticket's situation — the status
    update thread on the Update Status card. Independent of status changes
    (posting one doesn't require or trigger a status change, and changing
    status doesn't require one) — this replaced the old one-shot
    resolution_notes/status_reason fields with an ongoing, reviewable log."""
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name='status_notes')
    body = models.TextField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f'{self.ticket} — {self.created_at:%Y-%m-%d %H:%M}'


class TicketView(models.Model):
    """When a given staff user last opened this ticket's detail page —
    per-user, not global, since a shared dashboard used by several staff
    at once needs each person's own "have I seen this update" state, not
    one flag that any teammate opening the ticket clears for everyone.
    Powers the department dashboard's "new activity since you last looked"
    indicator (vendor communication only — see department_dashboard)."""
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name='views')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='+')
    last_viewed_at = models.DateTimeField()

    class Meta:
        unique_together = [('ticket', 'user')]


class FollowUpLog(models.Model):
    class Channel(models.TextChoices):
        EMAIL = 'email', 'Email'
        SMS = 'sms', 'Text message'

    # Exactly one of ticket/property is set (see the CheckConstraint below) —
    # a communication is logged against whichever screen it was sent from,
    # the ticket detail Follow-Up/Contractor Communication cards or the
    # property dashboard's Communication card. Both nullable so one model
    # and one _group_followups()-powered history UI (messaging/services.py)
    # serves both contexts instead of two parallel log models.
    ticket = models.ForeignKey(
        Ticket, on_delete=models.CASCADE, null=True, blank=True, related_name='followups',
    )
    property = models.ForeignKey(
        Property, on_delete=models.CASCADE, null=True, blank=True, related_name='followups',
    )
    contact = models.ForeignKey(
        Contact, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text='Who this specific row was sent to — null on rows predating this field.',
    )
    channel = models.CharField(max_length=10, choices=Channel.choices)
    sent_to = models.CharField(max_length=200)
    subject = models.CharField(max_length=200, blank=True)
    body = models.TextField()
    batch_id = models.UUIDField(
        default=uuid.uuid4,
        help_text='Shared by every row created from one Follow-Up "Send" click, so the audit trail can '
                   'render one line per send-action while keeping per-recipient success/failure.',
    )
    is_group = models.BooleanField(
        default=False,
        help_text='True only for a combined group email (all recipients in one to: list) — SMS and '
                   'individual email sends are always False, one physical send per row.',
    )
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    sent_at = models.DateTimeField(auto_now_add=True)
    success = models.BooleanField(default=True)
    error_message = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ['-sent_at']
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(ticket__isnull=False, property__isnull=True)
                    | models.Q(ticket__isnull=True, property__isnull=False)
                ),
                name='followuplog_exactly_one_of_ticket_or_property',
            ),
        ]

    def __str__(self):
        return f'{self.get_channel_display()} to {self.sent_to} re: {self.ticket or self.property}'
