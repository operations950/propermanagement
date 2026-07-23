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
        help_text='Leave blank to generate this task for every active property.',
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
    # `property` above stays highest-precedence: if set, this template applies
    # to that one property only, regardless of everything below. These layers
    # only matter when `property` is blank.
    property_types = models.JSONField(
        default=list, blank=True,
        help_text='Property.Type codes this applies to (e.g. ["str", "commercial"]). Empty = every '
                   'type. JSONField (not ArrayField) so this works on SQLite dev and Postgres prod alike.',
    )
    required_attributes = models.ManyToManyField(
        PropertyAttribute, blank=True, related_name='required_by_templates',
        help_text='Property must have ALL of these tags for this template to auto-apply. Empty = no constraint.',
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


class TaskPackage(models.Model):
    """A reusable, admin-authored bundle of TicketTemplates attachable to a
    property (e.g. "STR Base Package") — see PropertyPackage. Steps may
    optionally be dependency-ordered (see TaskPackageTemplate.depends_on),
    which is what makes the same model also cover a "Recurring Process"
    like "Monthly Accounting Close": the only structural difference is
    whether a step has a depends_on set, not a different kind of object."""
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['title']

    def __str__(self):
        return self.title


class TaskPackageTemplate(models.Model):
    package = models.ForeignKey(TaskPackage, on_delete=models.CASCADE, related_name='steps')
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


class Ticket(models.Model):
    class Status(models.TextChoices):
        OPEN = 'open', 'Open'
        ASSIGNED = 'assigned', 'Assigned'
        IN_PROGRESS = 'in_progress', 'In progress'
        BLOCKED = 'blocked', 'Blocked'
        UPCOMING = 'upcoming', 'Upcoming'
        COMPLETED = 'completed', 'Completed'
        VERIFIED = 'verified', 'Verified'
        SKIPPED = 'skipped', 'Skipped'
        NOT_APPLICABLE = 'not_applicable', 'Not applicable'
        DEFERRED = 'deferred', 'Deferred'
        CANCELLED = 'cancelled', 'Cancelled'

    # Statuses that require a stated reason at the form/view layer (not DB-enforced, matching the
    # existing cancelled_reason convention which also isn't DB-enforced).
    REASON_REQUIRED_STATUSES = ['skipped', 'not_applicable', 'deferred']

    # Statuses a package step must reach before dependents blocked on it are released — see
    # tickets.services.package_engine.unblock_dependents.
    DEPENDENCY_SATISFYING_STATUSES = ['completed', 'verified', 'skipped', 'not_applicable', 'cancelled']

    class Source(models.TextChoices):
        MANUAL = 'manual', 'Manual'
        EMAIL = 'email', 'Email'
        QUO = 'quo', 'Phone (Quo)'
        CALENDAR = 'calendar', 'Calendar'
        AIRBNB = 'airbnb', 'Airbnb'
        VRBO = 'vrbo', 'VRBO'
        FAKE = 'fake', 'Simulated (dev)'
        RECURRING = 'recurring', 'Recurring template'

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

    priority = models.CharField(max_length=20, choices=Priority.choices, default=Priority.MEDIUM)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)

    due_date = models.DateTimeField(null=True, blank=True)

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
        help_text='Why this was Skipped / Not applicable / Deferred — see REASON_REQUIRED_STATUSES.',
    )

    followup_done = models.BooleanField(
        default=False,
        help_text='Set the first time any Follow-Up text or email successfully sends — never reset, '
                   'so it just means "someone has been contacted at least once," not "up to date."',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['source', 'source_reference', 'kind'],
                condition=~models.Q(source_reference='') & ~models.Q(status='cancelled'),
                name='uniq_active_ticket_source_ref_kind',
            ),
            models.UniqueConstraint(
                fields=['created_from_template', 'scheduled_for', 'property'],
                condition=models.Q(created_from_template__isnull=False),
                name='uniq_template_scheduled_for_property',
            ),
            models.CheckConstraint(
                condition=models.Q(assigned_staff__isnull=True) | models.Q(assigned_contact__isnull=True),
                name='ticket_single_assignee',
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

    def save(self, *args, **kwargs):
        if self.completion_token_expires_at is None:
            self.completion_token_expires_at = timezone.now() + timedelta(
                days=settings.VENDOR_TOKEN_EXPIRY_DAYS
            )
        super().save(*args, **kwargs)


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
        CC = 'cc', 'CC'
        OTHER = 'other', 'Other'

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

    def __str__(self):
        return self.caption or self.file.name


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

    class Meta:
        ordering = ['-changed_at']


class FollowUpLog(models.Model):
    class Channel(models.TextChoices):
        EMAIL = 'email', 'Email'
        SMS = 'sms', 'Text message'

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name='followups')
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

    def __str__(self):
        return f'{self.get_channel_display()} to {self.sent_to} re: {self.ticket}'
