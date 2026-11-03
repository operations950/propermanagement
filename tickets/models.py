import uuid
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from core.models import Contact, Property, StaffProfile, Unit
from core.storage import DocumentStorage


class Priority(models.TextChoices):
    LOW = 'low', 'Low'
    MEDIUM = 'medium', 'Medium'
    HIGH = 'high', 'High'
    URGENT = 'urgent', 'Urgent'


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
        BLOCKED = 'blocked', 'Waiting on an earlier step'     # legacy: only ever set by the removed task-package engine; kept so existing tickets still display
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
    REASON_REQUIRED_STATUSES = ['skipped', 'not_applicable']

    # Statuses that count as "finished or stood down" for a step in a process run
    # (see processes/views.py).
    DEPENDENCY_SATISFYING_STATUSES = ['completed', 'verified', 'skipped', 'not_applicable', 'cancelled']

    # The only statuses that mean "completed_at should be set" — narrower than
    # DEPENDENCY_SATISFYING_STATUSES/COMPLETE_STATUSES (tickets/views.py), which also
    # include cancelled/skipped/not_applicable: those are "stopped," not "finished," and
    # shouldn't count toward the owner dashboard's "closed today" panel. See save() below.
    TRUE_COMPLETION_STATUSES = ['completed', 'verified']

    @classmethod
    def selectable_statuses(cls):
        """The statuses a person can pick or filter by. "Waiting on an earlier step" (BLOCKED) was set by the removed task-package engine, never by hand, and is
        not offered as a choice; the "Active" filter still includes those tickets."""
        return [(v, l) for v, l in cls.Status.choices if v != cls.Status.BLOCKED]

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
        RECURRING = 'recurring', 'Earlier recurring task'
        ONSITE = 'onsite', 'On-site visit'
        SESSION = 'session', 'Compliance item'

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
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text='The staff user who was actually at the keyboard when this ticket was created, if '
                   'any — set from the New Ticket form, a process-step task assignment, a promoted '
                   'session line, or a staff-side visit status override that converted a reported '
                   'issue. Orthogonal to `source` (which workflow produced the ticket): a fully '
                   'automated origin (email/Quo/booking-triggered/recurring template/a cleaner\'s own '
                   'token-link submit) has no human author at creation time, so this stays blank there '
                   '— that absence IS the answer for "who created this," not a data gap to fill in '
                   'later.',
    )

    property = models.ForeignKey(
        Property, on_delete=models.PROTECT, related_name='tickets', null=True, blank=True,
        help_text='Blank when the source (e.g. a shared Quo phone line) can\'t determine which '
                   'property this is about — staff assigns it manually.',
    )
    unit = models.ForeignKey(
        Unit, on_delete=models.PROTECT, related_name='tickets', null=True, blank=True,
        help_text='Which unit under the property this is about, if the property has units and it\'s '
                   'known — e.g. "AC broken in 3B." Optional even on a multi-unit property: a building-'
                   'wide issue is still just filed against the property with no unit set.',
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

    scheduled_for = models.DateField(
        null=True, blank=True,
        help_text='For recurring tickets: the occurrence date this instance represents.',
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

    def is_project(self):      # a plain method: `property` is a field name on this model, so @property cannot be used here
        """True while this ticket has been turned into a running project (it then lives under Projects, not in the ticket lists)."""
        return self.process_runs.filter(process_template__kind='project', status='active').exists()

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
    file = models.FileField(upload_to='ticket_attachments/%Y/%m/', storage=DocumentStorage())
    caption = models.CharField(max_length=200, blank=True)
    uploaded_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    uploaded_by_contact = models.ForeignKey(
        Contact, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text='Set when an external vendor uploaded this via the completion link.',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    visible_to_vendor = models.BooleanField(
        help_text="Whether this can appear on the public, unauthenticated vendor completion link. "
                   "False for internal-only uploads like the Documents card, regardless of file type — "
                   "deliberately has no model-level default, so every creation site must decide explicitly "
                   "rather than leaking a new upload path by accident.",
    )

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


class TicketClosingNote(models.Model):
    """The required closing/resolution status captured in a popup at the
    moment a ticket actually moves into one of Ticket.COMPLETE_STATUSES
    (tickets/views.py — completed, verified, cancelled, skipped, or not
    applicable). Deliberately its own model, separate from the free-form
    TicketStatusNote thread above it on the Update Status card: that log
    is optional and ongoing (posting one never requires or triggers a
    status change), this one is required and tied to a specific status
    change — a record of why THIS is the final status, not a running
    commentary. One row per closing, not an overwrite — a ticket that's
    reopened and closed again (e.g. Completed -> reopened -> Verified)
    gets a second row, so the history survives, the same reason
    TicketStatusNote itself replaced the old one-shot resolution_notes/
    status_reason fields. Enforced server-side in ticket_set_status, not
    just in the UI."""
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name='closing_notes')
    status = models.CharField(max_length=20, choices=Ticket.Status.choices)
    body = models.TextField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.ticket} — {self.get_status_display()} — {self.created_at:%Y-%m-%d %H:%M}'


class TicketPush(models.Model):
    """One time a ticket's due date was pushed later: from when, to when, how
    far, and why. The delayed / previous_due_date pair on Ticket only keeps
    the latest push; this keeps every one, so "how many times has this been
    pushed, and why" has an answer. The reason is required for people on the
    new layout (see ticket_set_due_date) and blank for anyone who pushes from
    the original screens."""
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name='pushes')
    from_date = models.DateTimeField()
    to_date = models.DateTimeField()
    days = models.PositiveIntegerField(help_text='Whole days the due date moved.')
    reason = models.CharField(max_length=200, blank=True)
    pushed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.ticket} pushed {self.days}d — {self.reason or "no reason"}'


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
