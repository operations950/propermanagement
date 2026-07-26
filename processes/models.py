from django.conf import settings
from django.db import models

from tickets.models import Ticket

VIDEO_EXTENSIONS = ('.mp4', '.mov', '.webm', '.m4v')
IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp', '.heic', '.gif')


class ProcessTemplate(models.Model):
    """A reusable SOP checklist (e.g. "Board Meeting Checklist") — the
    library entry. Authored via Django admin for v1 (see processes/admin.py);
    attaching one to a ticket and running its checklist is the staff-facing
    UI (see tickets/views.py's process_attach and the Processes card on
    ticket_detail.html)."""
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class ProcessTemplateItem(models.Model):
    """One checklist line on a template. action_type drives special
    behavior beyond a plain checkbox — see tickets/views.py's process_item_*
    views. document_key is only meaningful when action_type is
    'document_template': it selects a builder/template pair from a small
    Python registry (see tickets/views.py::DOCUMENT_BUILDERS) rather than a
    generic templating engine — deliberately concrete for now, see the plan
    this shipped under for why."""
    class ActionType(models.TextChoices):
        PLAIN = '', 'Plain checkbox'
        GOOGLE_MEET = 'google_meet', 'Schedule Google Meet'
        DOCUMENT_TEMPLATE = 'document_template', 'Prefilled document template'
        EMAIL_LINK = 'email_link', 'Link to email compose'

    process_template = models.ForeignKey(ProcessTemplate, on_delete=models.CASCADE, related_name='items')
    sequence_order = models.PositiveSmallIntegerField(default=0)
    text = models.CharField(max_length=300)
    is_required = models.BooleanField(default=True)
    requires_upload = models.BooleanField(
        default=False,
        help_text="Can't be checked until at least one file is attached to this step on the instance "
                   "(e.g. a signed affidavit, a photo of a physically-posted notice).",
    )
    action_type = models.CharField(max_length=20, choices=ActionType.choices, blank=True, default='')
    document_key = models.CharField(
        max_length=100, blank=True,
        help_text="Only used when action_type is 'document_template' — e.g. 'board_meeting_notice'.",
    )

    class Meta:
        ordering = ['sequence_order']

    def __str__(self):
        return self.text


class ProcessTemplateAttachment(models.Model):
    """Reference material authored into the template itself — an inline
    picture or a reference document (pdf/word/excel/...) shown alongside a
    specific checklist line (template_item set) or the template generally
    (template_item blank). This is the "insert pictures between lines /
    attach other doc types" part of the process being a reservoir of
    business knowledge, not just a checkbox list."""
    process_template = models.ForeignKey(ProcessTemplate, on_delete=models.CASCADE, related_name='attachments')
    template_item = models.ForeignKey(
        ProcessTemplateItem, on_delete=models.CASCADE, null=True, blank=True, related_name='attachments',
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


class ProcessInstance(models.Model):
    """One attachment of a ProcessTemplate to a Ticket. A ticket can have
    several — the ticket can't close (see tickets/services/process_gate.py)
    until every required item on every attached instance is checked."""
    process_template = models.ForeignKey(ProcessTemplate, on_delete=models.PROTECT, related_name='instances')
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name='process_instances')
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f'{self.process_template} on {self.ticket}'

    def is_complete(self):
        return not self.items.filter(is_required=True, is_checked=False).exists()

    def progress(self):
        items = list(self.items.all())
        if not items:
            return None
        return sum(1 for i in items if i.is_checked), len(items)


class ProcessInstanceItem(models.Model):
    """Copied from ProcessTemplateItem at attach time — a snapshot, not a
    live reference, so editing the template later never mutates an
    already-running or completed instance (same rationale as
    TemplateChecklistItem -> TicketChecklistItem in tickets/models.py). The
    meeting_* fields are only ever populated when action_type is
    'google_meet' — concrete named fields rather than a generic variable
    blob, since this is currently the only step type that produces data a
    later step reads (see process_item_document's Notice builder)."""
    instance = models.ForeignKey(ProcessInstance, on_delete=models.CASCADE, related_name='items')
    text = models.CharField(max_length=300)
    sequence_order = models.PositiveSmallIntegerField(default=0)
    is_required = models.BooleanField(default=True)
    requires_upload = models.BooleanField(default=False)
    action_type = models.CharField(max_length=20, blank=True, default='')
    document_key = models.CharField(max_length=100, blank=True)

    is_checked = models.BooleanField(default=False)
    checked_at = models.DateTimeField(null=True, blank=True)
    checked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )

    # Only populated for action_type == 'google_meet'.
    meeting_datetime = models.DateTimeField(null=True, blank=True)
    meeting_link = models.URLField(blank=True)
    meeting_dial_in = models.CharField(max_length=200, blank=True)
    calendar_event_id = models.CharField(max_length=200, blank=True)
    calendar_id = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ['sequence_order']

    def __str__(self):
        return self.text


class ProcessAttachment(models.Model):
    """A proof-of-completion upload against one instance item — the
    affidavit-signature upload, a photo of the physically-posted notice,
    etc. Broader allowed content types than the vendor portal's photo/video
    upload (see settings.PROCESS_ATTACHMENT_ALLOWED_CONTENT_TYPES) since
    these are often scanned documents, not photos."""
    instance_item = models.ForeignKey(ProcessInstanceItem, on_delete=models.CASCADE, related_name='attachments')
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


class ProcessInstanceDocument(models.Model):
    """The saved Notice/Affidavit content for a 'document_template' step —
    an editable, prefilled HTML page (see tickets/views.py's
    process_item_document), not a generated PDF (browser print-to-PDF
    covers that need without a new dependency, per this feature's agreed
    scope)."""
    instance_item = models.OneToOneField(ProcessInstanceItem, on_delete=models.CASCADE, related_name='document')
    content = models.TextField(blank=True)
    generated_at = models.DateTimeField(auto_now=True)
    generated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )

    def __str__(self):
        return f'Document for {self.instance_item}'
