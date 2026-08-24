from django.conf import settings
from django.db import models

from core.models import Contact, Property, StaffProfile, Unit
from tickets.models import Ticket


class SupplyRequest(models.Model):
    """Superseded by the par-level reading system below (SupplyItem/
    PropertySupply/SupplyReading/SupplyOrder/SupplyOrderLine) — the SMS/AI
    intake this fed never reliably resolved item/quantity from free text.
    Left in place, unused by any new code, until Phase 6 of the supply
    reorder redesign explicitly decommissions it (see the build brief)."""
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
    inferred."""
    name = models.CharField(max_length=200)
    unit_label = models.CharField(
        max_length=20, blank=True, help_text='e.g. "ct", "ea", "oz" — shown next to quantity, not enforced.',
    )
    walmart_item_id = models.CharField(
        max_length=32, blank=True,
        help_text='Walmart\'s numeric item id for the exact product to buy — e.g. from the product page '
                   'URL or the "Share" link. Blank is allowed at catalog-entry time (a property can stock '
                   'an item before someone\'s picked the exact Walmart listing for it) but a PropertySupply '
                   'row referencing an item with no id here is surfaced as a catalog problem on the cart '
                   'page, never silently dropped from the order.',
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class PropertySupply(models.Model):
    """Which items a given property (or, when unit is set, one specific
    unit within a multi-unit property) stocks, and how many to buy when a
    reading comes back below par. There is no stored "par level" number —
    HIGH *is* par, by definition; reorder_quantity is purely "how many to
    buy this time," set by human judgment and capped by that property's
    actual storage space, independent of whatever the par state means.

    unit is nullable/optional alongside property, same pattern as every
    other Unit-aware FK in this app (Booking.unit, Visit.unit,
    PropertyListingName.unit) — a single-unit property just has every row
    at unit=None, unchanged from before this field existed. A multi-unit
    property can mix both: some items tracked per-unit (each unit's own
    kitchen/bath supplies) alongside shared unit=None items (e.g. a
    building-wide laundry detergent) — see supply_check_context in
    services.py for exactly how a cleaner's visit resolves which rows to
    show, given their Visit.unit."""
    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='supplies')
    unit = models.ForeignKey(
        Unit, on_delete=models.CASCADE, null=True, blank=True, related_name='supplies',
        help_text='Leave blank for a property-wide item (or a single-unit property). Set to track this '
                   "item's par level independently for one specific unit.",
    )
    supply_item = models.ForeignKey(SupplyItem, on_delete=models.CASCADE, related_name='property_supplies')
    reorder_quantity = models.PositiveIntegerField(default=1, help_text='How many to buy when this reads below par.')
    display_order = models.PositiveIntegerField(default=0, help_text='Order this item appears in the par check.')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['property', 'unit__label', 'display_order']
        constraints = [
            # Two constraints, not one — a plain UniqueConstraint across
            # ['property', 'unit', 'supply_item'] would NOT catch a
            # duplicate property-wide row (unit=None): SQL treats every
            # NULL as distinct from every other NULL for uniqueness
            # purposes, so two unit=None rows for the same (property, item)
            # would silently both be allowed. Splitting into two
            # conditional constraints closes that gap: one for the
            # property-wide case (unit IS NULL), one for the per-unit case
            # (unit IS NOT NULL, property omitted since a unit already
            # belongs to exactly one property).
            models.UniqueConstraint(
                fields=['property', 'supply_item'], condition=models.Q(unit__isnull=True),
                name='uniq_property_supply_item_no_unit',
            ),
            models.UniqueConstraint(
                fields=['unit', 'supply_item'], condition=models.Q(unit__isnull=False),
                name='uniq_unit_supply_item',
            ),
        ]
        verbose_name_plural = 'property supplies'

    def __str__(self):
        return f'{self.supply_item} @ {self.unit or self.property}'


class SupplyReading(models.Model):
    """One row per item per visit — an append-only, never-updated
    observation. The whole detection loop in services.py compares
    successive readings against order timestamps, so a reading's honesty
    as an independent snapshot matters more than almost anything else in
    this app; nothing here should ever be mutated after creation."""
    class Level(models.TextChoices):
        HIGH = 'high', 'High'
        MID = 'mid', 'Mid'
        LOW = 'low', 'Low'

    property_supply = models.ForeignKey(PropertySupply, on_delete=models.CASCADE, related_name='readings')
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
            models.UniqueConstraint(fields=['property_supply', 'visit'], name='uniq_reading_per_visit_per_item'),
        ]

    def __str__(self):
        return f'{self.property_supply} = {self.get_level_display()} ({self.read_at:%Y-%m-%d})'


class SupplyOrder(models.Model):
    """One Walmart cart for one property — never combined across
    properties, since a Walmart order carries exactly one delivery address
    (see the build brief's cart-consolidation rule). created_by is a real
    logged-in staff user — unlike SupplyReading's read_by, cart-building is
    a staff-only action behind the normal login, not the cleaner's token
    link."""
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
    that put it in the cart — the audit trail a delivery-failure flag reads
    from (see the build brief's cart-state table)."""
    order = models.ForeignKey(SupplyOrder, on_delete=models.CASCADE, related_name='lines')
    property_supply = models.ForeignKey(PropertySupply, on_delete=models.CASCADE, related_name='order_lines')
    quantity = models.PositiveIntegerField()
    triggered_by_reading = models.ForeignKey(
        SupplyReading, on_delete=models.SET_NULL, null=True, blank=True, related_name='order_lines',
    )

    def __str__(self):
        return f'{self.quantity} x {self.property_supply.supply_item} ({self.order})'
