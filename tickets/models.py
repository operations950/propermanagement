import uuid
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from core.models import Contact, Property, StaffProfile


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
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.title} ({self.get_frequency_display()})'


class Ticket(models.Model):
    class Status(models.TextChoices):
        OPEN = 'open', 'Open'
        ASSIGNED = 'assigned', 'Assigned'
        IN_PROGRESS = 'in_progress', 'In progress'
        BLOCKED = 'blocked', 'Blocked'
        COMPLETED = 'completed', 'Completed'
        VERIFIED = 'verified', 'Verified'
        CANCELLED = 'cancelled', 'Cancelled'

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

    completion_token = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    completion_token_expires_at = models.DateTimeField(null=True, blank=True)

    resolution_notes = models.TextField(blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_reason = models.CharField(max_length=300, blank=True)

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
    channel = models.CharField(max_length=10, choices=Channel.choices)
    sent_to = models.CharField(max_length=200)
    subject = models.CharField(max_length=200, blank=True)
    body = models.TextField()
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
