from django.conf import settings
from django.db import models

from core.models import Contact, Property, StaffProfile, Unit
from tickets.models import Ticket


class SupplyRequest(models.Model):
    """Superseded by the par-level reading system below (SupplyItem/
    SupplyReading/SupplyOrder/SupplyOrderLine) — the SMS/AI intake this
    fed never reliably resolved item/quantity from free text. Left in
    place, unused by any new code, until Phase 6 of the supply reorder
    redesign explicitly decommissions it (see the build brief)."""
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        ORDERED = 'ordered', 'Ordered'
        CANCELLED = 'cancelled', 'Cancelled'

    property = models.ForeignKey(
        Property, on_delete=models.CASCADE, related_name='supply_requests', null=True, blank=True,
        help_text='Blank when the source (e.g. a shared Quo phone line) can\'t determine which property.',
    )
    raw_text = models.TextField(help_text='The original message text this request was parsed from.')
    source_reference = models.CharField(
        max_length=200, blank=True,
        help_text='External event id (e.g. email message id) this was parsed from, for idempotent re-polling.',
    )
    item_guess = models.CharField(max_length=200, blank=True)
    quantity_guess = models.CharField(max_length=50, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    source_ticket = models.ForeignKey(
        Ticket, on_delete=models.SET_NULL, null=True, blank=True, related_name='supply_requests',
    )
    order_batch = models.ForeignKey(
        'SupplyOrderBatch', on_delete=models.SET_NULL, null=True, blank=True, related_name='requests',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = [('property', 'source_reference', 'item_guess')]

    def __str__(self):
        return f'{self.item_guess or self.raw_text[:40]} ({self.property})'


class SupplyOrderBatch(models.Model):
    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='supply_order_batches')
    date = models.DateField()
    notes = models.TextField(blank=True)
    exported_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date']
        unique_together = [('property', 'date')]
        verbose_name_plural = 'supply order batches'

    def __str__(self):
        return f'{self.property} order list — {self.date}'


class SupplyItem(models.Model):
    """The global catalog — one row per purchasable product, shared across
    every property. Product identity (which real-world item this is) is
    resolved exactly once here, by a human choosing the right Walmart
    listing — never re-guessed at order time. Cart building downstream is
    then pure, deterministic string assembly from walmart_item_id, nothing
    inferred.

    is_standard/standard_reorder_quantity is the whole portfolio's default
    supply list, replacing what used to be a per-property PropertySupply
    row staff had to create at every single property/unit by hand. A
    standard item shows up everywhere automatically — new unit, no setup,
    nothing to clone. See supplies/services.py::resolve_supplies for
    exactly how a property/unit's actual list gets computed live from this
    plus PropertySupplyOverride, mirroring how
    onsite.services.checklist.resolve_checklist already resolves a
    property's checklist from StandardChecklistItem plus its own
    overrides — same shape of problem, same answer."""
    name = models.CharField(max_length=200)
    unit_label = models.CharField(
        max_length=20, blank=True, help_text='e.g. "ct", "ea", "oz" — shown next to quantity, not enforced.',
    )
    walmart_item_id = models.CharField(
        max_length=32, blank=True,
        help_text='Walmart\'s numeric item id for the exact product to buy — e.g. from the product page '
                   'URL or the "Share" link. Blank is allowed at catalog-entry time but an item with no id '
                   'here is surfaced as a catalog problem on the cart page, never silently dropped from '
                   'the order.',
    )
    is_standard = models.BooleanField(
        default=False,
        help_text='On the portfolio-wide standard list — every property/unit stocks this automatically, '
                   'no per-property setup required. Uncheck instead of deactivating if a few properties '
                   'still need it added manually as an extra (see PropertySupplyOverride) but most no '
                   "longer do.",
    )
    standard_reorder_quantity = models.PositiveIntegerField(
        default=1,
        help_text='Default "how many to buy when this reads below par" for every property/unit following '
                   'the standard list. A specific property/unit can still override this — see '
                   'PropertySupplyOverride.reorder_quantity. Ignored when is_standard is off.',
    )
    standard_display_order = models.PositiveIntegerField(
        default=0, help_text='Order this item appears in the standard list / par check.',
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class PropertySupplyOverride(models.Model):
    """The ONLY thing stored per property/unit — a deviation from the
    portfolio-wide standard list (see SupplyItem.is_standard). Absence of
    a row means "follow the standard list exactly as-is," the same
    inherit-by-default shape core.PropertyChecklistOverride already uses
    for the on-site checklist. Three real shapes, distinguished by
    is_hidden/reorder_quantity/the item's own is_standard flag — see
    resolve_supplies in services.py for exactly how each is interpreted:

    - Hide a standard item this property/unit doesn't stock:
      is_hidden=True (reorder_quantity irrelevant).
    - Override a standard item's quantity here: is_hidden=False,
      reorder_quantity=<the override>.
    - Add an item that ISN'T on the standard list, specific to this
      property/unit only ("an extra"): supply_item.is_standard is False,
      is_hidden=False, reorder_quantity=<required — there's no standard
      default to fall back on>.

    unit is nullable/optional alongside property, same pattern as every
    other Unit-aware FK in this app (Booking.unit, Visit.unit,
    PropertyListingName.unit) — unit=None means "applies to the whole
    property, every unit" (or is simply the only option for a single-unit
    property); a specific unit's own override wins over a property-wide
    one for the same item when both exist (see resolve_supplies)."""
    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='supply_overrides')
    unit = models.ForeignKey(
        Unit, on_delete=models.CASCADE, null=True, blank=True, related_name='supply_overrides',
        help_text='Leave blank to apply to the whole property (every unit). Set to override just one unit.',
    )
    supply_item = models.ForeignKey(SupplyItem, on_delete=models.CASCADE, related_name='property_overrides')
    is_hidden = models.BooleanField(
        default=False, help_text="This property/unit doesn't stock this standard item at all.",
    )
    reorder_quantity = models.PositiveIntegerField(
        null=True, blank=True,
        help_text='How many to buy when below par. Overrides the standard quantity for a standard item; '
                   'required (the only source of quantity) for a non-standard "extra" item. Irrelevant '
                   'when is_hidden is set.',
    )
    display_order = models.PositiveIntegerField(default=0, help_text='Order an "extra" item appears in the par check.')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['property', 'unit__label', 'display_order']
        constraints = [
            models.UniqueConstraint(
                fields=['property', 'supply_item'], condition=models.Q(unit__isnull=True),
                name='uniq_property_override_no_unit',
            ),
            models.UniqueConstraint(
                fields=['unit', 'supply_item'], condition=models.Q(unit__isnull=False),
                name='uniq_unit_override',
            ),
        ]

    def __str__(self):
        what = 'hidden' if self.is_hidden else f'qty {self.reorder_quantity}'
        return f'{self.supply_item} @ {self.unit or self.property} ({what})'


class SupplyReading(models.Model):
    """One row per item per visit — an append-only, never-updated
    observation. The whole detection loop in services.py compares
    successive readings against order timestamps, so a reading's honesty
    as an independent snapshot matters more than almost anything else in
    this app; nothing here should ever be mutated after creation.

    References property/unit/supply_item DIRECTLY rather than through an
    intermediate per-property "setup" row — a reading needs somewhere
    stable to correlate against over time (that's the whole point of a
    par-level history), but that stability comes from this natural
    (property, unit, supply_item) combination itself, not from a row that
    has to exist in advance. Nothing needs to be pre-created for a
    standard item before a cleaner can read it."""
    class Level(models.TextChoices):
        HIGH = 'high', 'High'
        MID = 'mid', 'Mid'
        LOW = 'low', 'Low'

    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='supply_readings')
    unit = models.ForeignKey(Unit, on_delete=models.CASCADE, null=True, blank=True, related_name='supply_readings')
    supply_item = models.ForeignKey(SupplyItem, on_delete=models.CASCADE, related_name='readings')
    visit = models.ForeignKey('onsite.Visit', on_delete=models.CASCADE, related_name='supply_readings')
    level = models.CharField(max_length=10, choices=Level.choices)
    read_at = models.DateTimeField(auto_now_add=True)
    # Attribution only, not "assignment" — cleaners reach a visit through
    # its no-login token link (see onsite.Visit.access_token), so there's
    # no authenticated user to stamp here directly. Snapshots whoever the
    # visit was assigned to at the moment the reading was taken (mirrors
    # onsite.Visit's own assigned_staff/assigned_contact dual-FK, tightened
    # here to "at most one" rather than "exactly one" since a reading taken
    # against a not-yet-assigned visit is a real, legitimate case this
    # can't rule out with a stricter constraint).
    read_by_staff = models.ForeignKey(StaffProfile, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    read_by_contact = models.ForeignKey(Contact, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        ordering = ['-read_at']
        constraints = [
            models.CheckConstraint(
                condition=models.Q(read_by_staff__isnull=True) | models.Q(read_by_contact__isnull=True),
                name='supplyreading_single_reader',
            ),
            # "One row per item per visit" (the brief's own words) — a
            # second tap on an already-answered item within the same visit
            # isn't a correction (readings are never updated), it's a
            # double-submit to reject outright.
            models.UniqueConstraint(fields=['property', 'unit', 'supply_item', 'visit'], name='uniq_reading_per_visit_per_item'),
        ]

    def __str__(self):
        return f'{self.supply_item} @ {self.unit or self.property} = {self.get_level_display()} ({self.read_at:%Y-%m-%d})'


class SupplyOrder(models.Model):
    """One Walmart cart for one property — never combined across
    properties, since a Walmart order carries exactly one delivery address
    (see the build brief's cart-consolidation rule). Units at the same
    property still consolidate naturally into this one cart — see
    SupplyOrderLine.unit. created_by is a real logged-in staff user —
    unlike SupplyReading's read_by, cart-building is a staff-only action
    behind the normal login, not the cleaner's token link."""
    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='supply_orders')
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    sent_at = models.DateTimeField(
        null=True, blank=True,
        help_text='Set optimistically the moment staff click through to Walmart — clicking doesn\'t prove '
                   'a purchase happened, but this is what suppresses the item from tomorrow\'s cart, and '
                   'the reading loop self-corrects either way: if the order never actually completed, the '
                   'next reading is still below par and the item returns, flagged. Undo-able by staff, '
                   'which just clears this back to null.',
    )
    cart_url = models.TextField(
        blank=True,
        help_text='The generated Walmart addToCart URL(s) for this order. More than one, newline-'
                   'separated, only if the item count pushed a single URL past a safe length.',
    )

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.property} order — {self.created_at:%Y-%m-%d}'


class SupplyOrderLine(models.Model):
    """One item within a SupplyOrder, tied back to the specific reading
    that put it in the cart — the audit trail a delivery-failure flag
    reads from (see the build brief's cart-state table). unit records
    which unit this line actually came from (None = property-wide) —
    purely attribution, since the order itself is still always one per
    property regardless of how many units contributed lines to it."""
    order = models.ForeignKey(SupplyOrder, on_delete=models.CASCADE, related_name='lines')
    unit = models.ForeignKey(Unit, on_delete=models.SET_NULL, null=True, blank=True, related_name='supply_order_lines')
    supply_item = models.ForeignKey(SupplyItem, on_delete=models.CASCADE, related_name='order_lines')
    quantity = models.PositiveIntegerField()
    triggered_by_reading = models.ForeignKey(
        SupplyReading, on_delete=models.SET_NULL, null=True, blank=True, related_name='order_lines',
    )

    def __str__(self):
        return f'{self.quantity} x {self.supply_item} ({self.order})'
