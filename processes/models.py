import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.text import slugify

from core.models import Contact, Property, StaffProfile
from tickets.models import Ticket

VIDEO_EXTENSIONS = ('.mp4', '.mov', '.webm', '.m4v')
IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp', '.heic', '.gif')


class StepType(models.TextChoices):
    """The 17 approved step types — see processes app's plan doc for the
    full rationale. Each type's extra configuration lives in
    ProcessTemplateStep.config (JSON), not a dedicated column per type —
    17 heterogeneous shapes can't share a fixed column set without dozens
    of mostly-null columns."""
    CHECKBOX = 'checkbox', 'Checkbox'
    CHECKLIST = 'checklist', 'Checklist'
    SHORT_TEXT = 'short_text', 'Short text'
    LONG_TEXT = 'long_text', 'Long text / notes'
    NUMBER_CURRENCY = 'number_currency', 'Number / currency'
    DATE_TIME = 'date_time', 'Date and time'
    DROPDOWN_MULTISELECT = 'dropdown_multiselect', 'Dropdown / multi-select'
    RECORD_SELECTOR = 'record_selector', 'Record selector'
    DOCUMENT_UPLOAD = 'document_upload', 'Document upload'
    PHOTO_VIDEO_UPLOAD = 'photo_video_upload', 'Photo / video upload'
    DIGITAL_SIGNATURE = 'digital_signature', 'Digital signature'
    TASK_ASSIGNMENT = 'task_assignment', 'Task assignment'
    APPROVAL_DECISION = 'approval_decision', 'Approval / decision'
    EMAIL_TEXT_ACTION = 'email_text_action', 'Email / text action'
    CALENDAR_EVENT = 'calendar_event', 'Calendar event'
    CALCULATION_FORMULA = 'calculation_formula', 'Calculation / formula'
    WAIT_TIMER = 'wait_timer', 'Wait / timer'


ASSIGNEE_CHOICES = list(StaffProfile.Role.choices) + [('external', 'External party')]


class ProcessTemplate(models.Model):
    """A reusable, ordered-step SOP (e.g. "Short-Term Rental Cleaning") —
    the library entry. Authored via the staff-facing builder
    (processes/views.py::process_template_form); attaching one to a
    ticket/property/contact and running its steps is a separate,
    generalized runtime UI (see ProcessRun)."""
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    category = models.CharField(
        max_length=100, blank=True,
        help_text='Freeform grouping for the template list, e.g. "Association", "Short-Term Rental".',
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['category', 'name']

    def __str__(self):
        return self.name


class ProcessTemplateStep(models.Model):
    """One ordered step on a template. step_type drives which config
    shape/runtime behavior applies — see StepType and
    processes/views.py::STEP_RUNTIME_HANDLERS. step_key lets a later step
    reference this one by a stable name (a calculation formula, an
    approval's "jump to step" target) instead of by a fragile numeric
    sequence_order."""
    process_template = models.ForeignKey(ProcessTemplate, on_delete=models.CASCADE, related_name='steps')
    sequence_order = models.PositiveSmallIntegerField(default=0)
    step_key = models.SlugField(max_length=60, blank=True)
    step_type = models.CharField(max_length=30, choices=StepType.choices)
    label = models.CharField(max_length=300)
    help_text = models.TextField(blank=True)
    config = models.JSONField(
        default=dict, blank=True,
        help_text='Step-type-specific settings — shape depends on step_type, see StepType docstring.',
    )
    is_required = models.BooleanField(default=True)
    requires_upload = models.BooleanField(
        default=False,
        help_text="Can't be marked complete until at least one file is attached to this step on the run "
                   "(e.g. a signed affidavit, a photo of a physically-posted notice) — independent of "
                   "step_type, so even a plain checkbox step can require proof.",
    )
    assignee_role = models.CharField(
        max_length=20, choices=ASSIGNEE_CHOICES, blank=True,
        help_text='Who performs this step — a department, or "External party" for a step sent via a '
                   'secure link. Mirrors Ticket.assigned_role/assigned_staff\'s existing dual-field shape.',
    )
    assignee_staff = models.ForeignKey(
        StaffProfile, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text='Optional specific-person override of assignee_role.',
    )
    deadline_days_after_start = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text='Days after the run starts that this step is due.',
    )

    class Meta:
        ordering = ['sequence_order']
        constraints = [
            models.UniqueConstraint(fields=['process_template', 'step_key'], name='uniq_template_step_key'),
        ]

    def __str__(self):
        return self.label

    def save(self, *args, **kwargs):
        if not self.step_key:
            base = slugify(self.label)[:50] or 'step'
            key = base
            n = 2
            while ProcessTemplateStep.objects.filter(
                process_template=self.process_template, step_key=key,
            ).exclude(pk=self.pk).exists():
                key = f'{base}-{n}'
                n += 1
            self.step_key = key
        super().save(*args, **kwargs)

    def move(self, direction):
        """Swaps sequence_order with the immediately preceding ('up') or
        following ('down') sibling step — the builder's simple move-buttons
        reorder mechanism (no drag-and-drop in this app)."""
        siblings = list(ProcessTemplateStep.objects.filter(process_template=self.process_template).order_by('sequence_order'))
        idx = next((i for i, s in enumerate(siblings) if s.pk == self.pk), None)
        if idx is None:
            return
        swap_idx = idx - 1 if direction == 'up' else idx + 1
        if swap_idx < 0 or swap_idx >= len(siblings):
            return
        other = siblings[swap_idx]
        self.sequence_order, other.sequence_order = other.sequence_order, self.sequence_order
        self.save(update_fields=['sequence_order'])
        other.save(update_fields=['sequence_order'])


class ProcessTemplateAttachment(models.Model):
    """Reference material authored into the template itself — an inline
    picture or a reference document shown alongside a specific step
    (template_step set) or the template generally (template_step blank)."""
    process_template = models.ForeignKey(ProcessTemplate, on_delete=models.CASCADE, related_name='attachments')
    template_step = models.ForeignKey(
        ProcessTemplateStep, on_delete=models.CASCADE, null=True, blank=True, related_name='attachments',
    )
    file = models.FileField(upload_to='process_template_attachments/%Y/%m/')
    caption = models.CharField(max_length=200, blank=True)
    sequence_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['sequence_order']

    @property
    def is_image(self):
        return self.file.name.lower().endswith(IMAGE_EXTENSIONS)

    def __str__(self):
        return self.caption or self.file.name


class ProcessRun(models.Model):
    """One launch of a ProcessTemplate against a real record. Exactly one
    of ticket/property/contact is set (see the CheckConstraint below) —
    directly mirrors tickets.models.FollowUpLog's existing "log against
    whichever screen" pattern, extended to a third target. A ticket can't
    close (see tickets/services/process_gate.py) until every required step
    on every attached, still-active run is complete."""
    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        COMPLETED = 'completed', 'Completed'
        CANCELLED = 'cancelled', 'Cancelled'

    process_template = models.ForeignKey(ProcessTemplate, on_delete=models.PROTECT, related_name='runs')
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, null=True, blank=True, related_name='process_runs')
    property = models.ForeignKey(Property, on_delete=models.CASCADE, null=True, blank=True, related_name='process_runs')
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, null=True, blank=True, related_name='process_runs')
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.CheckConstraint(
                condition=(
                    (models.Q(ticket__isnull=False) & models.Q(property__isnull=True) & models.Q(contact__isnull=True))
                    | (models.Q(ticket__isnull=True) & models.Q(property__isnull=False) & models.Q(contact__isnull=True))
                    | (models.Q(ticket__isnull=True) & models.Q(property__isnull=True) & models.Q(contact__isnull=False))
                ),
                name='processrun_exactly_one_of_ticket_property_contact',
            ),
        ]

    def __str__(self):
        return f'{self.process_template} on {self.get_target()}'

    def get_target(self):
        return self.ticket or self.property or self.contact

    def is_complete(self):
        return not self.steps.filter(is_required=True, is_complete=False).exists()

    def progress(self):
        steps = list(self.steps.all())
        if not steps:
            return None
        return sum(1 for s in steps if s.is_complete), len(steps)


class ProcessRunStep(models.Model):
    """Snapshotted from ProcessTemplateStep at launch time — a copy, not a
    live reference, so editing the template later never mutates an
    already-running or completed run (same rationale as
    TemplateChecklistItem -> TicketChecklistItem in tickets/models.py)."""
    run = models.ForeignKey(ProcessRun, on_delete=models.CASCADE, related_name='steps')
    sequence_order = models.PositiveSmallIntegerField(default=0)
    step_key = models.SlugField(max_length=60)
    step_type = models.CharField(max_length=30, choices=StepType.choices)
    label = models.CharField(max_length=300)
    help_text = models.TextField(blank=True)
    config = models.JSONField(default=dict, blank=True)
    is_required = models.BooleanField(default=True)
    requires_upload = models.BooleanField(default=False)
    assignee_role = models.CharField(max_length=20, choices=ASSIGNEE_CHOICES, blank=True)
    assignee_staff = models.ForeignKey(
        StaffProfile, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    deadline_days_after_start = models.PositiveSmallIntegerField(null=True, blank=True)

    is_complete = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    response = models.JSONField(
        default=dict, blank=True,
        help_text='The recorded value/output for this step — shape depends on step_type, see StepType docstring.',
    )

    class Meta:
        ordering = ['sequence_order']

    def __str__(self):
        return self.label

    def mark_complete(self, user=None):
        # response is included here because every caller sets it just
        # before calling mark_complete() — a narrower update_fields would
        # silently discard that in-memory change instead of persisting it.
        self.is_complete = True
        self.completed_at = timezone.now()
        self.completed_by = user
        self.save(update_fields=['is_complete', 'completed_at', 'completed_by', 'response'])


class ProcessAttachment(models.Model):
    """A proof-of-completion upload against one run step — the affidavit
    signature upload, a photo of the physically-posted notice, a signed
    document, or the DIGITAL_SIGNATURE step's drawn-signature PNG. Broader
    allowed content types than the vendor portal's photo/video upload (see
    settings.PROCESS_ATTACHMENT_ALLOWED_CONTENT_TYPES) since these are
    often scanned documents, not photos."""
    run_step = models.ForeignKey(ProcessRunStep, on_delete=models.CASCADE, related_name='attachments')
    file = models.FileField(upload_to='process_attachments/%Y/%m/')
    caption = models.CharField(max_length=200, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    @property
    def is_image(self):
        return self.file.name.lower().endswith(IMAGE_EXTENSIONS)

    def __str__(self):
        return self.caption or self.file.name


class ProcessRunExternalAccess(models.Model):
    """A secure, no-login mobile link to a subset of one ProcessRun's
    steps — for an external party (a cleaner, a tenant, an owner) who
    doesn't need a system account. Mirrors tickets.models.Ticket's
    existing completion_token/completion_token_expires_at/
    rotate_completion_token/is_completion_token_valid pattern exactly,
    generalized off of Ticket onto its own model since a run can now
    attach to a property or contact instead. Rate-limiting reuses
    vendorportal.models.AccessAttempt as-is (already IP-keyed, not
    ticket-specific)."""
    run = models.ForeignKey(ProcessRun, on_delete=models.CASCADE, related_name='external_links')
    token = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    token_expires_at = models.DateTimeField(null=True, blank=True)
    assigned_step_keys = models.JSONField(
        default=list, blank=True,
        help_text='step_key values this link may see/act on. Empty list means every step on the run '
                   'whose assignee_role is "external".',
    )
    external_contact = models.ForeignKey(
        Contact, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text='Who this link was generated for, for the audit trail — not required for access.',
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'External link for {self.run}'

    def is_valid(self):
        if self.token_expires_at is None:
            return True
        return timezone.now() <= self.token_expires_at

    def visible_steps(self):
        qs = self.run.steps.all()
        if self.assigned_step_keys:
            qs = qs.filter(step_key__in=self.assigned_step_keys)
        else:
            qs = qs.filter(assignee_role='external')
        return qs
