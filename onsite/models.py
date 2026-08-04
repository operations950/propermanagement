"""On-site visits (turnovers, deep cleans, inspections) — see
ONSITE_DESIGN.md at the repo root for the full design rationale. A Visit is
one person, physically at one property, working an ordered checklist and
required to report back with photos and a signature; VisitType/checklist
items describe *what kind* of visit and *what's on the list*, Booking/
ImportBatch describe *why a visit exists* (a guest checkout), and Visit/
VisitChecklistItem/VisitMedia/VisitIssue describe what actually happened.

Deliberately not built on the `processes` app — that's a step-type engine
for administrative staff at a computer; this is a flat, ordered, mandatory
checklist completed on a phone by someone standing in a house."""
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.text import slugify

from core.models import Contact, Property, PropertyAttribute, StaffProfile


class VisitType(models.Model):
    """A kind of on-site work (turnover clean, deep clean, inspection) — a
    real model rather than an enum so a new kind can be added from admin
    without a deploy. Seeded by seed_checklist_templates."""
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True, blank=True)
    color = models.CharField(max_length=20, blank=True, help_text='CSS color/variable for the dashboard board.')
    default_duration_minutes = models.PositiveIntegerField(default=90)
    requires_deadline = models.BooleanField(
        default=True,
        help_text='True for turnovers, which must beat the next check-in. False for deep cleans/'
                   "inspections scheduled between guests, where there's no hard ready_by.",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def clean(self):
        # A VisitType nobody can actually perform is worse than no VisitType
        # — enforced here (called from the admin form) rather than only by
        # convention. Skipped for a not-yet-saved instance since the
        # checklist necessarily gets created afterward.
        if self.pk and not self.standard_items.filter(is_active=True).exists():
            raise ValidationError('A visit type needs at least one active standard checklist item.')


class StandardChecklistItem(models.Model):
    """The reservoir for a VisitType — the living standard checklist. Never
    copied onto a property; see onsite/services/checklist.py for how a
    property's actual list is resolved from this plus its overrides/
    additions."""
    visit_type = models.ForeignKey(VisitType, on_delete=models.CASCADE, related_name='standard_items')
    section = models.CharField(max_length=100, blank=True, help_text='e.g. "Kitchen", "Bathrooms" — for grouping.')
    order = models.PositiveIntegerField(default=0)
    text = models.CharField(max_length=300)
    mandatory = models.BooleanField(default=True)
    requires_photo = models.BooleanField(default=False)
    requires_note = models.BooleanField(default=False)
    required_attributes = models.ManyToManyField(
        PropertyAttribute, blank=True, related_name='onsite_checklist_items',
        help_text='This item only resolves at properties tagged with ALL of these attributes '
                   '(e.g. "has_grill") — leave empty to apply everywhere.',
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['visit_type', 'section', 'order']

    def __str__(self):
        return f'{self.visit_type} — {self.text}'


class PropertyChecklistOverride(models.Model):
    """A deviation from the standard list at one property. Absence of a row
    means inherit — hiding a standard item is is_hidden=True, never a
    delete, so it stays visible (greyed) in the property's checklist editor
    with a one-click restore."""
    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='onsite_checklist_overrides')
    visit_type = models.ForeignKey(VisitType, on_delete=models.CASCADE, related_name='property_overrides')
    standard_item = models.ForeignKey(StandardChecklistItem, on_delete=models.CASCADE, related_name='overrides')
    is_hidden = models.BooleanField(default=False)
    mandatory_override = models.BooleanField(null=True, blank=True)
    order_override = models.PositiveIntegerField(null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [('property', 'standard_item')]

    def __str__(self):
        return f'{self.property} override — {self.standard_item.text}'


class PropertyChecklistItem(models.Model):
    """A property-specific checklist addition that isn't in the standard
    reservoir at all — the gate code quirk, the one genuinely unique thing.
    The most valuable content in the system; see the admin promote-to-
    standard action in onsite/views.py for turning a repeated one into a
    real StandardChecklistItem."""
    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='onsite_custom_items')
    visit_type = models.ForeignKey(VisitType, on_delete=models.CASCADE, related_name='property_custom_items')
    text = models.CharField(max_length=300)
    mandatory = models.BooleanField(default=True)
    requires_photo = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['property', 'visit_type', 'order']

    def __str__(self):
        return f'{self.property} — {self.text}'


class ImportBatch(models.Model):
    """One booking-file upload — retains the raw file and a summary of what
    it did, so "why did this cleaning vanish" always has an answer. See
    onsite/importers.py and the two-phase upload/apply views."""
    class Source(models.TextChoices):
        AIRBNB = 'airbnb', 'Airbnb'
        VRBO = 'vrbo', 'VRBO'

    # Null for a portfolio-wide .csv (many properties in one file, resolved
    # per-row by listing name) — set only for the single-property .ics flow,
    # or a .csv without a listing/property column. See onsite/importers.py.
    property = models.ForeignKey(
        Property, on_delete=models.CASCADE, related_name='onsite_import_batches', null=True, blank=True,
    )
    source = models.CharField(max_length=20, choices=Source.choices)
    raw_file = models.FileField(upload_to='onsite_import_batches/%Y/%m/')
    covers_start = models.DateField(help_text='Earliest checkout date this file actually covers.')
    covers_end = models.DateField(help_text='Latest checkout date this file actually covers.')
    new_count = models.PositiveIntegerField(default=0)
    changed_count = models.PositiveIntegerField(default=0)
    cancelled_count = models.PositiveIntegerField(default=0)
    applied_at = models.DateTimeField(null=True, blank=True)
    imported_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.get_source_display()} import {self.created_at:%Y-%m-%d %H:%M}'


class Booking(models.Model):
    """One reservation, from an Airbnb/VRBO file import — the source of
    truth for "is there a checkout today, and when's the next check-in."
    Never hand-entered."""
    class Source(models.TextChoices):
        AIRBNB = 'airbnb', 'Airbnb'
        VRBO = 'vrbo', 'VRBO'
        MANUAL = 'manual', 'Manual'

    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        CANCELLED = 'cancelled', 'Cancelled'

    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='bookings')
    source = models.CharField(max_length=20, choices=Source.choices)
    external_uid = models.CharField(max_length=200, help_text='UID from the ICS/CSV row — the idempotency key.')
    guest_name = models.CharField(max_length=200, blank=True)
    guest_phone_last4 = models.CharField(max_length=4, blank=True)
    check_in = models.DateTimeField()
    check_out = models.DateTimeField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    last_seen_at = models.DateTimeField(
        default=timezone.now,
        help_text='Updated on every import that still contains this UID — a row not touched by an '
                   "import covering its date range is presumed cancelled off the guest's platform.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['check_out']
        unique_together = [('source', 'external_uid')]

    def __str__(self):
        return f'{self.property} — {self.check_out:%Y-%m-%d} checkout'


class Visit(models.Model):
    """The center of this module — one person, one property, one ordered
    checklist. Assignment mirrors tickets.Ticket's dual-FK pattern
    (assigned_staff xor assigned_contact), enforced by a real CheckConstraint
    here exactly as tickets.Ticket does. access_token mirrors
    Ticket.completion_token's shape for the same no-login external-access
    need."""
    class Status(models.TextChoices):
        UNASSIGNED = 'unassigned', 'Unassigned'
        SCHEDULED = 'scheduled', 'Scheduled'
        IN_PROGRESS = 'in_progress', 'In progress'
        SUBMITTED = 'submitted', 'Submitted'
        VERIFIED = 'verified', 'Verified'
        CANCELLED = 'cancelled', 'Cancelled'
        SKIPPED = 'skipped', 'Skipped'

    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='visits')
    visit_type = models.ForeignKey(VisitType, on_delete=models.PROTECT, related_name='visits')
    booking = models.ForeignKey(
        Booking, on_delete=models.SET_NULL, null=True, blank=True, related_name='visits',
        help_text='The checkout that generated this visit, if any.',
    )
    next_booking = models.ForeignKey(
        Booking, on_delete=models.SET_NULL, null=True, blank=True, related_name='visits_before',
        help_text="The check-in this visit has to beat — drives ready_by.",
    )

    scheduled_date = models.DateField(null=True, blank=True)
    scheduled_start = models.TimeField(null=True, blank=True)
    ready_by = models.DateTimeField(
        null=True, blank=True,
        help_text='Defaults to next_booking.check_in when set. Null means no hard deadline.',
    )

    assigned_staff = models.ForeignKey(
        StaffProfile, on_delete=models.SET_NULL, null=True, blank=True, related_name='onsite_visits',
    )
    assigned_contact = models.ForeignKey(
        Contact, on_delete=models.SET_NULL, null=True, blank=True, related_name='onsite_visits',
        help_text='Use for an external/contract cleaner.',
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.UNASSIGNED)

    started_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)

    access_token = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    token_expires_at = models.DateTimeField(null=True, blank=True)

    google_event_id = models.CharField(max_length=200, blank=True)
    google_sync_pending = models.BooleanField(default=False)

    signature_image = models.ImageField(upload_to='onsite_signatures/%Y/%m/', null=True, blank=True)
    signed_name = models.CharField(max_length=200, blank=True)
    signed_at = models.DateTimeField(null=True, blank=True)
    signed_ip = models.GenericIPAddressField(null=True, blank=True)

    notes = models.TextField(blank=True, help_text='Staff-authored, visible to the assignee.')
    created_from_rule = models.ForeignKey(
        'VisitRule', on_delete=models.SET_NULL, null=True, blank=True, related_name='generated_visits',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['scheduled_date', 'ready_by']
        constraints = [
            models.CheckConstraint(
                condition=models.Q(assigned_staff__isnull=True) | models.Q(assigned_contact__isnull=True),
                name='visit_single_assignee',
            ),
        ]

    def __str__(self):
        return f'{self.property} — {self.visit_type} ({self.scheduled_date})'

    def clean(self):
        if self.assigned_staff_id and self.assigned_contact_id:
            raise ValidationError('A visit can be assigned to staff OR a contact, not both.')

    def assignee_label(self):
        if self.assigned_staff_id:
            return str(self.assigned_staff)
        if self.assigned_contact_id:
            return f'{self.assigned_contact} (external)'
        return 'Unassigned'

    def rotate_access_token(self):
        self.access_token = uuid.uuid4()
        self.token_expires_at = timezone.now() + timedelta(days=settings.VENDOR_TOKEN_EXPIRY_DAYS)

    def is_access_token_valid(self):
        if self.token_expires_at is None:
            return True
        return timezone.now() <= self.token_expires_at


class VisitChecklistItem(models.Model):
    """The materialized checklist for one visit — snapshotted from
    onsite.services.checklist.resolve_checklist() at creation. This is the
    one place copying the resolved view is correct: once a visit exists,
    its checklist is frozen (except one-off additions before it starts) so
    a submitted visit's record never changes underneath it."""
    class Source(models.TextChoices):
        STANDARD = 'standard', 'Standard'
        PROPERTY = 'property', 'Property-specific'
        ONEOFF = 'oneoff', 'One-off'

    visit = models.ForeignKey(Visit, on_delete=models.CASCADE, related_name='checklist_items')
    source = models.CharField(max_length=20, choices=Source.choices)
    section = models.CharField(max_length=100, blank=True)
    order = models.PositiveIntegerField(default=0)
    text = models.CharField(max_length=300)
    mandatory = models.BooleanField(default=True)
    requires_photo = models.BooleanField(default=False)
    requires_note = models.BooleanField(default=False)
    is_new_unreviewed = models.BooleanField(
        default=False, help_text='Added to the standard list after this property was last reviewed.',
    )

    is_completed = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)
    note = models.TextField(blank=True)
    skip_reason = models.CharField(
        max_length=300, blank=True, help_text='Required to pass a mandatory item without completing it.',
    )

    class Meta:
        ordering = ['visit', 'section', 'order']

    def __str__(self):
        return self.text


class VisitMedia(models.Model):
    class MediaType(models.TextChoices):
        PHOTO = 'photo', 'Photo'
        VIDEO = 'video', 'Video'

    visit = models.ForeignKey(Visit, on_delete=models.CASCADE, related_name='media')
    checklist_item = models.ForeignKey(
        VisitChecklistItem, on_delete=models.SET_NULL, null=True, blank=True, related_name='media',
    )
    issue = models.ForeignKey(
        'VisitIssue', on_delete=models.SET_NULL, null=True, blank=True, related_name='media',
        help_text='Set when this photo was attached to a reported issue rather than a checklist item.',
    )
    file = models.FileField(upload_to='onsite_visit_media/%Y/%m/')
    media_type = models.CharField(max_length=10, choices=MediaType.choices, default=MediaType.PHOTO)
    caption = models.CharField(max_length=200, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['uploaded_at']


class VisitIssue(models.Model):
    """Something the cleaner reports that isn't a checklist item — creates a
    real Ticket on submit (see onsite/services/checklist.py::submit_visit),
    which is the bridge between this module and the rest of the app."""
    visit = models.ForeignKey(Visit, on_delete=models.CASCADE, related_name='issues')
    description = models.TextField()
    created_ticket = models.ForeignKey(
        'tickets.Ticket', on_delete=models.SET_NULL, null=True, blank=True, related_name='onsite_issue',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.description[:60]


class PropertyChecklistReview(models.Model):
    """When a property's checklist for a visit type was last acknowledged
    by a human — the anchor for VisitChecklistItem.is_new_unreviewed (see
    onsite/services/checklist.py::resolve_checklist). A missing row means
    "never reviewed," so every active standard item shows as new until
    someone opens the property's checklist editor and reviews it."""
    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='onsite_checklist_reviews')
    visit_type = models.ForeignKey(VisitType, on_delete=models.CASCADE, related_name='property_reviews')
    reviewed_at = models.DateTimeField()

    class Meta:
        unique_together = [('property', 'visit_type')]

    def __str__(self):
        return f'{self.property} / {self.visit_type} reviewed {self.reviewed_at:%Y-%m-%d}'


class VisitRule(models.Model):
    """Recurring visit generation for inspections/deep cleans — modeled on
    tickets.TicketTemplate per CLAUDE.md's instruction to follow the
    recurring path. See generate_scheduled_visits management command."""
    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='onsite_rules')
    visit_type = models.ForeignKey(VisitType, on_delete=models.CASCADE, related_name='rules')
    interval_months = models.PositiveIntegerField(default=3)
    default_assignee = models.ForeignKey(
        StaffProfile, on_delete=models.SET_NULL, null=True, blank=True, related_name='onsite_rules',
    )
    last_generated_at = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.property} — {self.visit_type} every {self.interval_months}mo'
