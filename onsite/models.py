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
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.text import slugify

from core.models import Contact, Property, PropertyAttribute, StaffProfile, Unit
from core.storage import DocumentStorage


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
    is_addon = models.BooleanField(
        default=False,
        help_text='A checklist bundle that gets added onto another visit (e.g. Deep Clean extras '
                   'added onto a Turnover) rather than a kind of visit that gets scheduled on its '
                   'own — excluded from visit-type pickers. Its standard_items are still managed '
                   'normally through the checklist editor.',
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
        # Deliberately NOT ['visit_type', 'section', 'order'] — sorting by
        # section name would alphabetize the sections (Bathrooms before
        # Kitchen before... Final Walkthrough would jump ahead of Kitchen),
        # scrambling the deliberate room-by-room flow the seed data is
        # written in. Sections stay grouped correctly anyway because each
        # section's rows are seeded contiguously — a single 'order' sort
        # preserves both the section grouping AND the intended sequence.
        # See resolve_checklist()'s matching note.
        ordering = ['visit_type', 'order']

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
    raw_file = models.FileField(upload_to='onsite_import_batches/%Y/%m/', storage=DocumentStorage())
    covers_start = models.DateField(help_text='Earliest checkout date this file actually covers.')
    covers_end = models.DateField(help_text='Latest checkout date this file actually covers.')
    new_count = models.PositiveIntegerField(default=0)
    changed_count = models.PositiveIntegerField(default=0)
    reactivated_count = models.PositiveIntegerField(
        default=0,
        help_text='Reservations that came back ACTIVE after previously being marked Cancelled — see '
                   'onsite/services/bookings.py::diff_bookings\'s "reactivated" bucket.',
    )
    cancelled_count = models.PositiveIntegerField(default=0)
    applied_at = models.DateTimeField(null=True, blank=True)
    imported_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.get_source_display()} import {self.created_at:%Y-%m-%d %H:%M}'


class BookingFeedHealth(models.Model):
    """One row per booking source (Airbnb, VRBO, ...) tracking whether that
    platform's import pipeline is actually alive — feeds the owner
    dashboard's on-site panel. Updated in booking_import_apply right after
    a batch for that source applies successfully (see
    onsite/views.py::_update_feed_health).

    Three deliberately separate signals, because they fail differently:
    - last_upload_at stale → the upload agent/staff routine is broken
      (an alarm — nobody's feeding this source at all).
    - newest_booked_date old despite a fresh last_upload_at → uploads are
      happening but few/no NEW reservations have come in since (booking
      pace is slow — informational, not a pipeline problem). Only
      populated when the source file actually carries a "booked/reserved
      on" column (see RawBooking.booked_at) — stays null if it never has,
      rather than guessing.
    - coverage_through → how far into the future the file's reservations
      reach (same number as that batch's own ImportBatch.covers_end,
      carried forward as a running high-water mark across every batch for
      this source).
    All three only ever move forward (a batch with an older max date than
    what's already stored never overwrites it) — a stray partial/backdated
    file should never make a healthy feed look like it regressed."""
    source = models.CharField(max_length=20, choices=ImportBatch.Source.choices, unique=True)
    last_upload_at = models.DateTimeField(null=True, blank=True)
    newest_booked_date = models.DateField(null=True, blank=True)
    coverage_through = models.DateField(null=True, blank=True)

    class Meta:
        verbose_name_plural = 'booking feed health'
        ordering = ['source']

    def __str__(self):
        return f'{self.get_source_display()} feed health'


class DailyUploadSlot(models.Model):
    """One of the small, fixed set of reports staff actually pull every day
    (e.g. "Airbnb - Upcoming Page 1", "VRBO - Patrick") — a real model
    rather than a hardcoded list so admin can add/rename/retire a slot
    without a deploy if the daily routine changes. Seeded by
    seed_daily_upload_slots. Replaces a single generic "pick a source, pick
    a file" upload with named drop zones matching exactly what staff
    already do each day — see onsite/views.py::upload_slot."""
    label = models.CharField(max_length=100, unique=True)
    source = models.CharField(max_length=20, choices=ImportBatch.Source.choices)
    filename_hint = models.CharField(
        max_length=100, blank=True,
        help_text='A substring (case-insensitive) often found in this file\'s real filename — used only '
                   "to highlight a likely match when someone browses instead of dragging; dropping a file "
                   "directly onto this slot always uses it regardless of filename. Leave blank if there's no reliable pattern.",
    )
    required_columns = models.CharField(
        max_length=500, blank=True,
        help_text='Comma-separated column names (case-insensitive, matched against the file\'s header '
                   'row) that MUST be present for a dropped file to be accepted here — e.g. '
                   '"Confirmation Code, Start Date, End Date, Listing". A file missing any of these is '
                   "rejected outright with a clear error before anything is parsed or saved, so dropping "
                   "the wrong platform's file into this slot can't silently misread columns. Leave blank "
                   'to skip this check for this slot (only the importer\'s own generic required columns '
                   'still apply).',
    )
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    last_uploaded_at = models.DateTimeField(null=True, blank=True)
    last_uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    last_batch = models.ForeignKey(ImportBatch, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        ordering = ['order', 'label']

    def __str__(self):
        return self.label

    def required_columns_list(self):
        return [c.strip() for c in self.required_columns.split(',') if c.strip()]

    def missing_columns(self, fieldnames):
        """Given a CSV's actual header row (fieldnames), returns the subset
        of required_columns_list() not present (case/whitespace-insensitive
        match) — empty list means the file's format checks out for this
        slot. No-op (always passes) when required_columns is blank."""
        required = self.required_columns_list()
        if not required:
            return []
        available = {(f or '').strip().lower() for f in fieldnames or []}
        return [col for col in required if col.strip().lower() not in available]


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
    unit = models.ForeignKey(
        Unit, on_delete=models.SET_NULL, null=True, blank=True, related_name='bookings',
        help_text='Which unit under the property this reservation is for — resolved from the matched '
                   'PropertyListingName.unit at import time. Blank for a single-unit property.',
    )
    source = models.CharField(max_length=20, choices=Source.choices)
    external_uid = models.CharField(max_length=200, help_text='UID from the ICS/CSV row — the idempotency key.')
    listing_name = models.CharField(
        max_length=200, blank=True,
        help_text="The platform's own listing title for this specific reservation (from the portfolio "
                   "CSV's listing/property column — see RawBooking.listing_name), e.g. \"800 Tropic - "
                   'Wave (C)\". Kept verbatim for reference/audit even now that `unit` above carries the '
                   'actual resolved unit — useful when a listing name hasn\'t been pinned to a specific '
                   'Unit yet. Blank for a single-property .ics import, which has no listing column.',
    )
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


class CleaningPricingSettings(models.Model):
    """Singleton (always exactly one row — see get()) holding the one
    number that controls deep-clean pricing across the whole portfolio: a
    deep clean pays this percent of whatever the property/unit's normal
    cleaning_fee already is, rather than a separate fee set per property.
    Previously Property/Unit each had their own deep_clean_fee field —
    replaced by this after real usage showed that's not how the business
    actually prices a deep clean (a fixed multiple of the standard rate,
    controlled in one place, not an independent per-listing number to keep
    in sync). Deliberately its own tiny model rather than living in
    core.AppSetting — that store is explicitly scoped to secrets/API keys
    (see its own docstring), not general business config."""
    deep_clean_fee_percent = models.DecimalField(
        max_digits=5, decimal_places=1, default=Decimal('150.0'),
        help_text='A deep clean pays this percent of the normal cleaning fee — 150 means 1.5x the '
                   'standard rate. Applied in Visit.cleaner_payout_amount().',
    )
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.deep_clean_fee_percent}% deep-clean markup'

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class CleaningPaymentBatch(models.Model):
    """One payment run against the internal cleaner queue — what we
    actually paid for a group of cleanings, made in one go (e.g. "$310 to
    Maria via Venmo on the 15th"). Deliberately not "payroll": this is
    per-cleaning-job pay, not an employee wage/salary record. total_amount
    is stored rather than always summed live from its visits, because it's
    the historical fact of what was paid — see Visit.paid_amount below for
    why the per-visit amount is locked the same way."""
    paid_at = models.DateTimeField(auto_now_add=True)
    paid_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)
    note = models.CharField(max_length=300, blank=True, help_text='Optional — how it was paid, a check number, etc.')

    class Meta:
        ordering = ['-paid_at']

    def __str__(self):
        return f'${self.total_amount} paid {self.paid_at:%Y-%m-%d} ({self.visits.count()} cleaning(s))'


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
    unit = models.ForeignKey(
        Unit, on_delete=models.SET_NULL, null=True, blank=True, related_name='visits',
        help_text='Which unit under the property this visit is for, if any — carried over from the '
                   'generating Booking. Checklist resolution stays property-level regardless (a shared '
                   'building checklist); only the visit record itself is unit-aware.',
    )
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
    is_deep_clean = models.BooleanField(
        default=False,
        help_text='This turnover also includes the deep-clean extra checklist items — see the '
                   "addon VisitType's standard_items. Only changeable before the visit starts.",
    )
    created_from_rule = models.ForeignKey(
        'VisitRule', on_delete=models.SET_NULL, null=True, blank=True, related_name='generated_visits',
    )

    payment_batch = models.ForeignKey(
        CleaningPaymentBatch, on_delete=models.SET_NULL, null=True, blank=True, related_name='visits',
        help_text='Set once this cleaning has actually been paid for — see the Cleaning Payments '
                   'screen. Null means still owed.',
    )
    paid_amount = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True,
        help_text="What was actually paid for this specific cleaning, locked in at the moment "
                   "payment_batch was set — unlike cleaner_payout_amount() (used for what's still "
                   "owed), this deliberately does NOT track later edits to the property/unit's price. "
                   "A real payment already made shouldn't silently reprice itself.",
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

    def cleaner_payout_amount(self):
        """What we pay ourselves for this turnover, resolved live from
        today's Property/Unit pricing (not snapshotted onto the visit — an
        edited price is meant to apply retroactively here, by design).
        Unit's cleaning_fee wins over the property's when set; that's
        always the base rate, deep clean or not. A deep clean pays a
        percentage of that base rate — CleaningPricingSettings.get()
        .deep_clean_fee_percent, controlled in one place across the whole
        portfolio, not a separate fee set per property/unit. None means
        unpriced (no base cleaning_fee anywhere in the chain) rather than
        free — callers should treat that as "needs pricing," not $0.
        Deliberately a plain method, not @property — see assignee_label's
        own comment on why (this model's own `property` FK shadows the
        builtin)."""
        base = None
        for source in (self.unit, self.property):
            if source is not None and source.cleaning_fee is not None:
                base = source.cleaning_fee
                break
        if base is None:
            return None
        if not self.is_deep_clean:
            return base
        percent = CleaningPricingSettings.get().deep_clean_fee_percent
        return (base * percent / Decimal('100')).quantize(Decimal('0.01'))

    def is_same_day_checkin(self):
        """True when the next guest checks in the same calendar day this
        cleaning is scheduled for — the tightest possible turnover, worth
        its own signal rather than folding into ready_by/at_risk (those are
        about lateness; this is about same-day tightness regardless of how
        much time is actually left). Reads next_booking directly rather
        than ready_by, since ready_by is deliberately overridable (see its
        own field help_text) and shouldn't silently change what counts as
        same-day.

        Deliberately a plain method, not @property — this model has its
        own `property` field (the FK to core.Property), which shadows the
        builtin `property` decorator inside this class body. See
        assignee_label just above for the same reason."""
        if not self.next_booking_id or not self.scheduled_date:
            return False
        return timezone.localtime(self.next_booking.check_in).date() == self.scheduled_date

    def vacancy_days(self, cap=30):
        """Days between this cleaning and the next guest's check-in,
        capped at `cap` — the companion signal to is_same_day_checkin for
        every cleaning that ISN'T same-day: an exact count much past the
        cap isn't operationally useful (a property empty 3 days matters
        for staffing in a way one empty 90 days doesn't), and no
        next_booking at all (nothing on the books yet) reads the same as
        a distant one from a "does this need attention soon" standpoint,
        so both are represented as `cap`. Returns None only when this
        visit has no scheduled_date at all to measure from (shouldn't
        happen for a real turnover visit, but a manually-created one
        could lack it)."""
        if not self.scheduled_date:
            return None
        if not self.next_booking_id:
            return cap
        days = (timezone.localtime(self.next_booking.check_in).date() - self.scheduled_date).days
        if days <= 0:
            return 0
        return min(days, cap)

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
        DEEP_CLEAN = 'deep_clean', 'Deep clean extra'

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
        # See StandardChecklistItem.Meta's comment — same reasoning: a plain
        # 'order' sort, not '(section, order)', keeps the deliberate
        # room-by-room sequence instead of alphabetizing sections.
        ordering = ['visit', 'order']

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
    file = models.FileField(upload_to='onsite_visit_media/%Y/%m/', storage=DocumentStorage())
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
    recurring path. See generate_scheduled_visits management command.

    Note the name collision this shares with the site-wide "Recurring"
    nav item (worksessions app, mounted at /recurring/) — unrelated
    systems that happen to both mean "happens on a schedule." This
    model's own staff-facing screen is deliberately labeled "Recurring
    visits" rather than just "Recurring" to stay unambiguous, since it
    lives inside the On-Site section rather than the global nav."""
    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='onsite_rules')
    unit = models.ForeignKey(
        Unit, on_delete=models.SET_NULL, null=True, blank=True, related_name='onsite_rules',
        help_text='Which unit under the property this rule generates visits for. Blank means the '
                   'whole property (or a single-unit property, where this always stays blank).',
    )
    visit_type = models.ForeignKey(VisitType, on_delete=models.CASCADE, related_name='rules')
    interval_months = models.PositiveIntegerField(default=3)
    interval_days = models.PositiveIntegerField(
        null=True, blank=True,
        help_text='Day-based cadence (e.g. 7 for weekly, 14 for biweekly) — takes precedence over '
                   'interval_months when set. Leave blank for a month-based cadence like a quarterly '
                   'deep clean/inspection. Exists because relativedelta(months=...) has no way to express '
                   'anything shorter than a month, which a weekly commercial common-area clean needs.',
    )
    default_assignee = models.ForeignKey(
        StaffProfile, on_delete=models.SET_NULL, null=True, blank=True, related_name='onsite_rules',
    )
    last_generated_at = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # A plain method, not @property — this class already has a field
    # literally named `property` (the FK above), which shadows the
    # builtin `property` decorator for the rest of the class body.
    # Django templates call a zero-arg method exactly like a property, so
    # `{{ rule.cadence_display }}` still works fine either way.
    def cadence_display(self):
        if self.interval_days:
            weeks, remainder_days = divmod(self.interval_days, 7)
            if weeks and not remainder_days:
                return f'{weeks} week{"s" if weeks != 1 else ""}'
            return f'{self.interval_days} day{"s" if self.interval_days != 1 else ""}'
        return f'{self.interval_months} month{"s" if self.interval_months != 1 else ""}'

    def __str__(self):
        target = f'{self.property} — {self.unit.label}' if self.unit_id else str(self.property)
        return f'{target} — {self.visit_type} every {self.cadence_display()}'
