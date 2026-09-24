import builtins
import re
from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.db.models import Case, IntegerField, Value, When

from .fields import EncryptedTextField
from .storage import DocumentStorage

PHONE_REGEX = re.compile(r'^\d{3}-\d{3}-\d{4}$')
phone_validator = RegexValidator(PHONE_REGEX.pattern, 'Enter phone as XXX-XXX-XXXX.')


def is_valid_phone(phone):
    """True for blank (every phone field in the app is optional) or a
    properly dash-formatted 10-digit US number — the one standard format
    static/js/phone-format.js auto-inserts dashes into as people type.
    Used by the handful of raw-POST contact-creation paths that don't go
    through a ModelForm (and so wouldn't otherwise run phone_validator)."""
    return not phone or bool(PHONE_REGEX.fullmatch(phone))


class Property(models.Model):
    class Type(models.TextChoices):
        GENERAL = 'general', 'General'
        ASSOCIATION = 'association', 'Associations'
        SHORT_TERM_RENTAL = 'str', 'Short-Term Rentals'
        LONG_TERM_RENTAL = 'ltr', 'Long-Term Rentals'
        SNOWBIRD = 'snowbird', 'Snowbird Oversight'
        COMMERCIAL = 'commercial', 'Commercial'

    name = models.CharField(max_length=200)
    # Auto-derived from street/city/state/zip_code in save() once all four are
    # present — not directly edited via PropertyForm anymore (see core/forms.py).
    # Existing properties predating the structured address fields keep whatever
    # free text they already had until someone re-verifies them through the
    # property form's address picker.
    address = models.CharField(max_length=300, blank=True)
    street = models.CharField(max_length=200, blank=True)
    city = models.CharField(
        max_length=100, blank=True,
        help_text='Also used by the New Ticket bubble picker to group properties by city once a '
                   'type has more than 50 of them.',
    )
    state = models.CharField(max_length=2, blank=True)
    zip_code = models.CharField(max_length=10, blank=True)
    address_verified = models.BooleanField(
        default=False,
        help_text='Set automatically when USPS confirms this address on save — see core/usps.py.',
    )
    property_type = models.CharField(max_length=20, choices=Type.choices, default=Type.GENERAL)
    is_general = models.BooleanField(
        default=False,
        help_text="A placeholder for 'not a specific property' at this scope (e.g. \"Associations "
                   "(general)\") — not a real unit or building. Lets a ticket be scoped to a business "
                   "line without forcing a specific address when one isn't known.",
    )
    timezone = models.CharField(max_length=50, default='America/Chicago')
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # --- Access info — edited from the Property Detail dashboard, not the
    # create/edit form (see core/views.py::property_detail). Always
    # single-valued per property, unlike system locations below, which are a
    # variable-length list — hence plain fields here rather than a side table.
    gate_code = models.CharField(max_length=50, blank=True)
    door_code = models.CharField(max_length=50, blank=True)
    lockbox_code = models.CharField(max_length=50, blank=True)
    alarm_code = models.CharField(max_length=50, blank=True)
    wifi_network = models.CharField(max_length=100, blank=True)
    wifi_password = models.CharField(max_length=100, blank=True)
    access_notes = models.TextField(
        blank=True, help_text='Anything else staff need to get in or navigate the property.',
    )
    board_meeting_address = models.CharField(
        max_length=300, blank=True,
        help_text='Where this association normally holds its board meetings — used to prefill the '
                   'meeting Notice template. Primarily relevant to Association-type properties, but '
                   'not restricted to them.',
    )
    default_check_in_time = models.TimeField(
        null=True, blank=True,
        help_text='Used by the onsite module to compute a turnover deadline when a booking import '
                   "only carries a date (the common case for ICS feeds), and to fill in a guest's "
                   'check-in time on the calendar. Short-term rentals only.',
    )
    default_check_out_time = models.TimeField(
        null=True, blank=True,
        help_text='Same as check-in, for the checkout side of a turnover.',
    )
    # --- Cleaning size/pricing — replaces the old flat cleaning_fee model
    # (removed). A cleaning's price is now built entirely from time: each
    # on-site checklist item carries its own minutes and an optional
    # multiplier (see onsite.StandardChecklistItem.ScalesBy), and these four
    # counts are what that multiplier reads — "make all beds" x 12 min,
    # multiplied by bed_count, for example. A specific Unit's own value
    # overrides these when set (same fallback pattern as access codes
    # below), for a multi-unit building where each unit's actual size
    # differs. Primarily relevant to Short-Term Rentals but not restricted
    # to them. Blank means this multiplier contributes nothing to any
    # checklist item scaled by it — see onsite.Visit.estimated_minutes().
    bedroom_count = models.PositiveSmallIntegerField(null=True, blank=True)
    bed_count = models.PositiveSmallIntegerField(null=True, blank=True)
    bathroom_count = models.DecimalField(
        max_digits=3, decimal_places=1, null=True, blank=True,
        help_text='Supports a half bath, e.g. 2.5.',
    )
    square_footage = models.PositiveIntegerField(null=True, blank=True)
    # --- QuickBooks: the two accounts this rental's money runs through (see
    # core/qb_accounts.py). The income-statement account is the reimbursable
    # expense account: expenses we pay are coded to it, and the monthly
    # reimbursement comes back into it, so it is NOT all expense. The
    # balance-sheet account is the owner's trust account: deposits in, and out
    # go direct expenses, reimbursements to us, commission and owner payments.
    qb_expense_account = models.ForeignKey(
        'QuickBooksAccount', on_delete=models.SET_NULL, null=True, blank=True, related_name='expense_for_properties',
        help_text="This rental's reimbursable-expense account on the income statement.",
    )
    qb_trust_account = models.ForeignKey(
        'QuickBooksAccount', on_delete=models.SET_NULL, null=True, blank=True, related_name='trust_for_properties',
        help_text="This rental's owner trust account on the balance sheet.",
    )
    ledger_synced_at = models.DateTimeField(
        null=True, blank=True, help_text="When this rental's QuickBooks transactions were last pulled in (a month can only be closed on a recent sync).",
    )

    commission_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('10.00'), validators=[MinValueValidator(Decimal('0')), MaxValueValidator(Decimal('100'))],
        help_text="Our commission, as a percent of the month's income deposits (the top line: we are paid whether or not the month is profitable). Only an administrator can change it.",
    )

    class FinancialsLevel(models.TextChoices):
        PROPERTY = 'property', 'One set of books for the whole property'
        UNIT = 'unit', 'Separate books for each unit'

    financials_level = models.CharField(
        max_length=10, choices=FinancialsLevel.choices, default=FinancialsLevel.PROPERTY,
        help_text='Whether this rental is closed as one set of books (the two accounts above) or unit by unit '
                   '(each unit has its own two accounts and its own owner payment, and the property is their sum). '
                   'A month already closed keeps the shape it was closed in; open months follow this setting.',
    )
    turnover_price_override = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True,
        help_text='A negotiated flat price for a standard Turnover Clean at this property, replacing '
                   'the checklist-computed time x rate estimate for that one visit type specifically '
                   '(deep clean and other visit types are unaffected and still price from their own '
                   "checklist time). The estimate itself is still computed and shown even when this "
                   "is set — useful for checking whether the negotiated price still roughly matches "
                   'how long the job actually takes. Admin-only: never shown to regular staff. A '
                   "specific Unit's own value overrides this when set.",
    )

    class Meta:
        verbose_name_plural = 'properties'
        ordering = ['name']

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.street and self.city and self.state and self.zip_code:
            self.address = f'{self.street}, {self.city}, {self.state} {self.zip_code}'
        super().save(*args, **kwargs)


class PropertyListingName(models.Model):
    """A name/title this property answers to on a booking platform — used
    by the onsite module's portfolio-wide booking import to tie each
    reservation row to the right property (and, via `unit` below, the right
    unit within it — see onsite/services/bookings.py). A variable-length
    list rather than a single field on Property: a multi-unit building
    commonly has a separate Airbnb/VRBO listing per unit, all pointing at
    the same property (each pinned to its own `unit`). A given literal
    name still belongs to exactly one property (the unique constraint
    below) — it's the property side that's one-to-many, not the name
    side."""
    class Platform(models.TextChoices):
        AIRBNB = 'airbnb', 'Airbnb'
        VRBO = 'vrbo', 'VRBO'

    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='listing_names')
    unit = models.ForeignKey(
        'Unit', on_delete=models.SET_NULL, null=True, blank=True, related_name='listing_names',
        help_text='Which unit under the property this specific listing is for — the real fix for the '
                   '"3 units, 1 property record" gap this model\'s own docstring above used to flag. '
                   'Blank for a single-unit property, where the listing name resolves to the whole '
                   'property as it always has.',
    )
    platform = models.CharField(max_length=20, choices=Platform.choices)
    name = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['platform', 'name']
        constraints = [
            models.UniqueConstraint(fields=['platform', 'name'], name='uniq_listing_name_per_platform'),
        ]

    def __str__(self):
        return f'{self.name} ({self.get_platform_display()}) → {self.property.name}'


class PropertySystemLocation(models.Model):
    """Where to find something on-site (water shutoff, electrical panel,
    sprinkler timer, ...) — an open-ended list since which systems exist
    varies per property, unlike the fixed access-code fields on Property
    itself."""
    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='system_locations')
    unit = models.ForeignKey(
        'Unit', on_delete=models.CASCADE, null=True, blank=True, related_name='system_locations',
        help_text="Set when this is inside one unit (its own shutoff or panel); blank for something that serves the whole building.",
    )
    system_name = models.CharField(max_length=120, help_text='e.g. "Water shutoff", "Electrical panel", "Sprinkler timer"')
    location = models.CharField(max_length=300)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['system_name']

    def __str__(self):
        return f'{self.system_name} — {self.property}'


class PropertyDocument(models.Model):
    """A staff-uploaded reference document for a property — governing docs
    for an Association, or anything else worth keeping on hand for other
    property types. Manually named by whoever uploads it (no fixed doc-type
    schema); `category` is a freeform hint (e.g. "Governing Documents"),
    left blank when it doesn't apply."""
    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='documents')
    name = models.CharField(max_length=200)
    category = models.CharField(max_length=100, blank=True)
    file = models.FileField(upload_to='property_documents/%Y/%m/', storage=DocumentStorage())
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.name} — {self.property}'


class Unit(models.Model):
    """A specific unit within a multi-unit Property — a bookable listing in
    a multi-unit STR building, or an individually-owned condo/townhome
    within an Association. Deliberately thin (just a label) at first:
    STR-vs-Association behavior is already distinguished by the parent
    Property.property_type, so unit-specific fields can be added later
    without disruption once real usage shows what's actually needed.
    A single-unit property simply has zero Unit rows — every FK that can
    reference a Unit (Booking, Visit, Ticket, ...) keeps it nullable and
    optional alongside its existing Property FK, never a replacement for
    it."""
    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='units')
    label = models.CharField(max_length=100, help_text='e.g. "Bamboo", "3B", "Unit 204"')
    access_code = models.CharField(
        max_length=50, blank=True,
        help_text="This unit's own door/lock code — separate from the property's gate/door/lockbox "
                   'codes (those cover the whole building). Shown to a cleaner on the on-site visit '
                   'link only once they tap "Get Code," which also marks the visit started.',
    )
    # Overrides the property's own value for this specific unit (e.g. a
    # studio vs. a 3-bedroom under the same building) when set — blank
    # means "use the property's value." See Property's matching fields for
    # the full explanation of how these feed checklist time estimates.
    bedroom_count = models.PositiveSmallIntegerField(null=True, blank=True)
    bed_count = models.PositiveSmallIntegerField(null=True, blank=True)
    bathroom_count = models.DecimalField(max_digits=3, decimal_places=1, null=True, blank=True)
    square_footage = models.PositiveIntegerField(null=True, blank=True)
    turnover_price_override = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True,
        help_text="Overrides the property's own turnover_price_override for this specific unit. "
                   'Admin-only: never shown to regular staff.',
    )
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # Used only when the property's financials are kept unit by unit (see
    # Property.financials_level): this unit's own two QuickBooks accounts.
    qb_expense_account = models.ForeignKey(
        'QuickBooksAccount', on_delete=models.SET_NULL, null=True, blank=True, related_name='expense_for_units',
        help_text="This unit's reimbursable-expense account on the income statement.",
    )
    qb_trust_account = models.ForeignKey(
        'QuickBooksAccount', on_delete=models.SET_NULL, null=True, blank=True, related_name='trust_for_units',
        help_text="This unit's owner trust account on the balance sheet.",
    )
    ledger_synced_at = models.DateTimeField(null=True, blank=True, help_text="When this unit's QuickBooks transactions were last pulled in.")
    # A unit's own wifi and how to get in, when they differ from the building's
    # (the property's wifi_network / wifi_password / access_notes still apply to a
    # unit that leaves these blank).
    exclude_from_stats = models.BooleanField(
        default=False,
        help_text="Leave this unit out of the rental performance statistics — an owner's unit that is rarely rented, say. "
                   'It stays on the calendars; it just isn\'t counted in occupancy, rates, revenue or gaps.',
    )
    lockbox_code = models.CharField(max_length=50, blank=True, help_text="This unit's own lockbox code, when it has one (else the building's applies).")
    alarm_code = models.CharField(max_length=50, blank=True, help_text="This unit's own alarm code, when it has one (else the building's applies).")
    wifi_network = models.CharField(max_length=100, blank=True)
    wifi_password = models.CharField(max_length=100, blank=True)
    access_notes = models.TextField(blank=True, help_text="How to get into this unit (which door, which side of the building) — private, like the property's access notes.")

    class Meta:
        ordering = ['label']
        constraints = [
            models.UniqueConstraint(fields=['property', 'label'], name='uniq_unit_label_per_property'),
        ]

    def __str__(self):
        return f'{self.property.name} — {self.label}'


def property_dropdown_queryset():
    """Properties ordered for a grouped dropdown: General, then Associations,
    Short-Term Rentals, Long-Term Rentals, Snowbird Oversight, Commercial —
    with each type's general/non-specific placeholder sorted first within
    its group. Used with {% regroup %} on get_property_type_display in
    templates."""
    type_order = Case(
        When(property_type=Property.Type.GENERAL, then=Value(0)),
        When(property_type=Property.Type.ASSOCIATION, then=Value(1)),
        When(property_type=Property.Type.SHORT_TERM_RENTAL, then=Value(2)),
        When(property_type=Property.Type.LONG_TERM_RENTAL, then=Value(3)),
        When(property_type=Property.Type.SNOWBIRD, then=Value(4)),
        When(property_type=Property.Type.COMMERCIAL, then=Value(5)),
        default=Value(6), output_field=IntegerField(),
    )
    return (
        Property.objects.filter(is_active=True)
        .annotate(_type_order=type_order)
        .order_by('_type_order', '-is_general', 'name')
    )


def properties_by_type():
    """Property drilldown-bubble-picker data, grouped by type in the same
    order as property_dropdown_queryset(). Each type also carries a city
    breakdown for the (currently dormant, given real property counts all
    well under 50) capacity-aware drill-down: a type's properties only get
    grouped by city once there are more than 50 of them, and a city only
    gets a text filter once IT has more than 50. Shared by every bubble
    property picker across the site (New Ticket, Pending, ticket detail's
    assign banner, the Contact review queue, ...) — one grouping helper,
    reused wherever the drilldown markup contract is used."""
    buckets = {}
    for p in property_dropdown_queryset():
        buckets.setdefault(p.property_type, []).append(p)

    result = []
    for value, label in Property.Type.choices:
        props = buckets.get(value, [])
        entry = {'type_key': value, 'type_label': label, 'needs_city_tier': len(props) > 50}
        if entry['needs_city_tier']:
            city_buckets = {}
            for p in props:
                city_buckets.setdefault(p.city or 'Unspecified', []).append(p)
            entry['cities'] = [
                {
                    'city': city,
                    'properties': [{'id': p.id, 'name': p.name} for p in city_props],
                    'needs_filter': len(city_props) > 50,
                }
                for city, city_props in sorted(city_buckets.items())
            ]
        else:
            entry['properties'] = [{'id': p.id, 'name': p.name} for p in props]
        result.append(entry)
    return result


# Trade options for the Contact form's bubble-lock picker (required once
# contact_type is Vendor/Contractor — see core/forms.py::ContactForm.clean).
# Contact.trade stays a plain CharField (not a TextChoices enum) since
# "Other" needs to accept free text that isn't one of these — the bubble UI
# is just a convenience over the same field, not a stricter schema.
TRADE_CHOICES = [
    'HVAC', 'Plumbing', 'Electrical', 'Handyman', 'Landscaping', 'Tree Trimming', 'Irrigation',
    'Pool Service', 'Pest Control', 'Roofing', 'Painting', 'Locksmith', 'Appliance Repair',
    'Cleaning', 'General Contractor', 'Flooring', 'Drywall', 'Fencing', 'Security / Alarm',
    'Elevator', 'Waterproofing', 'Window / Glass', 'Concrete / Paving', 'Moving / Hauling',
    'Insurance', 'Legal',
]


def group_vendors_by_trade(contacts):
    """Buckets Vendor/Contractor contacts by their `trade` field — feeds the
    trade-tier drilldown bubble pickers (Tickets tab's Contractor filter,
    the ticket-row Assignee quick-edit) so a company with 20+ vendors never
    renders them as one flat wall of bubbles. Untraded contacts land in a
    trailing "Other" group rather than being dropped."""
    from django.utils.text import slugify

    groups, order = {}, []
    for c in contacts:
        label = c.trade or 'Other'
        key = slugify(label) or 'other'
        if key not in groups:
            groups[key] = {'key': key, 'label': label, 'contacts': []}
            order.append(key)
        groups[key]['contacts'].append(c)
    return sorted(
        (groups[k] for k in order),
        key=lambda g: (g['label'] == 'Other', g['label']),
    )


def group_contacts_by_type(contacts):
    """Buckets contacts by their `contact_type` — feeds the same group-tier
    drilldown bubble picker as group_vendors_by_trade, but for the property
    Communication card's "quick-add Board Members / Association Members /
    Owners" pickers rather than vendor trades."""
    labels = dict(Contact.ContactType.choices)
    groups, order = {}, []
    for c in contacts:
        key = c.contact_type
        if key not in groups:
            groups[key] = {'key': key, 'label': labels.get(key, key), 'contacts': []}
            order.append(key)
        groups[key]['contacts'].append(c)
    return [groups[k] for k in order]


class Contact(models.Model):
    class ContactType(models.TextChoices):
        GUEST = 'guest', 'Guest'
        TENANT = 'tenant', 'Tenant'
        OWNER = 'owner', 'Owner'
        BOARD_MEMBER = 'board_member', 'Board Member'
        ASSOCIATION_MEMBER = 'association_member', 'Association Member'
        ON_SITE_STAFF = 'on_site_staff', 'On-site Staff'
        LEAD = 'lead', 'Lead'
        VENDOR = 'vendor', 'Vendor / Contractor'
        STAFF_ADJACENT = 'staff_adjacent', 'Staff'
        OTHER = 'other', 'Other'

    class Source(models.TextChoices):
        MANUAL = 'manual', 'Manual'
        QUO = 'quo', 'Quo'
        GMAIL = 'gmail', 'Gmail'
        YARDI = 'yardi', 'Yardi'
        DOCUMENT = 'document', 'Document import'

    name = models.CharField(max_length=200)
    contact_type = models.CharField(max_length=20, choices=ContactType.choices, default=ContactType.OTHER)
    secondary_types = models.JSONField(
        default=list, blank=True,
        help_text='Additional simultaneous types beyond the primary Type above — e.g. an Owner who is '
                   'also a Board Member. Never used for Vendor/Contractor, which stays single-type since '
                   "it forces a Trade below. A plain list of ContactType values, not a relation — this "
                   'is tag-like metadata, not something ever queried/filtered on at scale.',
    )
    trade = models.CharField(
        max_length=100, blank=True,
        help_text='For vendors: e.g. plumbing, HVAC, cleaning, handyman',
    )
    phone = models.CharField(max_length=30, blank=True, validators=[phone_validator])
    email = models.EmailField(blank=True)
    properties = models.ManyToManyField(
        Property, blank=True, related_name='contacts',
        help_text='The propert(y/ies) this contact is associated with — e.g. a tenant, an owner, or a '
                   'board member who may sit on more than one board.',
    )
    units = models.ManyToManyField(
        Unit, blank=True, related_name='contacts',
        help_text='Specific unit(s) this contact owns/occupies, if the property has units and it\'s '
                   'known — e.g. which condo an Owner actually owns. Independent of `properties` above, '
                   'not a replacement for it: a Board Member is tied to the whole association via '
                   '`properties` with no unit needed, while an individual unit Owner has both.',
    )
    source = models.CharField(
        max_length=20, choices=Source.choices, default=Source.MANUAL,
        help_text='Where this contact came from — set automatically, kept for provenance/audit.',
    )
    quo_external_id = models.CharField(
        max_length=64, blank=True, db_index=True,
        help_text="This contact's stable id in Quo's own Contacts API — lets sync_quo_contacts match "
                   'this row on future runs even if name/phone/email later change in Quo, and detect '
                   'when Quo\'s own record has been edited since (see quo_updated_at).',
    )
    quo_updated_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Quo's own updatedAt for this contact as of the last sync — a newer value on the next "
                   'sync means something changed there, which stages a ContactUpdateCandidate for '
                   'review rather than silently overwriting this row.',
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'{self.name} ({self.get_contact_type_display()})'

    def secondary_type_labels(self):
        labels = dict(self.ContactType.choices)
        return [labels.get(t, t) for t in (self.secondary_types or [])]


def creatable_contact_types():
    """Contact.ContactType choices offered by every contact-creation path
    EXCEPT Admin Tools' staff-creation flow (core/views.py::staff_create) —
    'Staff' is admin-only, so the plain Contact form, the quick-add-from-
    ticket flow, and AI-classified imports (Quo/Gmail/document) all draw
    from this instead of the full choice list. An already-Staff contact
    being edited through the plain form is handled separately (see
    ContactForm) rather than here, so this stays a flat exclusion list."""
    return [c for c in Contact.ContactType.choices if c[0] != Contact.ContactType.STAFF_ADJACENT]


class ContactDocument(models.Model):
    """A staff-uploaded reference document for a contact — same shape as
    PropertyDocument (manually named, no fixed doc-type schema)."""
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name='documents')
    name = models.CharField(max_length=200)
    file = models.FileField(upload_to='contact_documents/%Y/%m/', storage=DocumentStorage())
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.name} — {self.contact}'


class ContactImportCandidate(models.Model):
    """A contact harvested from a bulk Quo/Gmail import, held here — not in
    the real Contact table — until a human reviews and approves it. Hard
    gate by design: nothing from an import is usable anywhere in the app
    (ticket pickers, property pages, assignment) until it's promoted. See
    core/views.py's contact_review/_approve/_reject and the
    sync_quo_contacts/import_gmail_contacts management commands.

    Deliberately no unique constraint on phone/email — both are optional
    here, and dedup against existing Contacts/other pending candidates is
    a functional check in the importer, not a DB guarantee (same pragmatic
    approach as the inline add-contact flow on New Ticket)."""
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending review'
        APPROVED = 'approved', 'Approved'
        REJECTED = 'rejected', 'Rejected'

    source = models.CharField(max_length=20, choices=Contact.Source.choices)
    external_id = models.CharField(
        max_length=64, blank=True, db_index=True,
        help_text="The source system's own stable id for this contact (Quo's contact id, currently) — "
                   'lets sync_quo_contacts recognize "already staged, still pending" without relying on '
                   'phone/email, which can change.',
    )
    name = models.CharField(max_length=200, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    trade = models.CharField(max_length=100, blank=True)
    suggested_contact_type = models.CharField(
        max_length=20, choices=Contact.ContactType.choices, default=Contact.ContactType.OTHER,
    )
    suggested_property = models.ForeignKey(
        Property, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text='Best-guess property from AI classification of this contact\'s Quo message history '
                   '(see intake/contact_classifier.py) — pre-fills the review queue\'s property picker, '
                   'staff still confirms or changes it on approval.',
    )
    raw_context = models.TextField(
        blank=True, help_text='Evidence for the reviewer — e.g. the Quo company field or a Gmail subject line.',
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    resolved_contact = models.ForeignKey(
        Contact, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text='Set once approved — the real Contact this candidate became.',
    )

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.name or self.phone or self.email} ({self.get_source_display()}, {self.get_status_display()})'


class ContactUpdateCandidate(models.Model):
    """A proposed edit to an already-approved, already-in-use Contact,
    staged for review rather than applied automatically — see
    sync_quo_contacts. Different from ContactImportCandidate (a brand new
    person awaiting first approval): this Contact already exists and may
    be linked to tickets/properties/follow-ups, so silently overwriting it
    from an external source on a daily timer would be a real regression,
    not a convenience. Staff see old vs. proposed side by side and choose
    to apply or dismiss."""
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending review'
        APPLIED = 'applied', 'Applied'
        DISMISSED = 'dismissed', 'Dismissed'

    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name='pending_updates')
    proposed_name = models.CharField(max_length=200, blank=True)
    proposed_phone = models.CharField(max_length=30, blank=True)
    proposed_email = models.EmailField(blank=True)
    raw_context = models.TextField(blank=True, help_text='What changed in Quo, for the reviewer.')
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'Update for {self.contact.name} ({self.get_status_display()})'


class DuplicateDismissal(models.Model):
    """Staff said 'these two are not actually the same person' on the
    duplicate-contacts screen — remembered so that pair stops being
    flagged on every future scan. Stored as an unordered pair (always
    saved with contact_a_id < contact_b_id) so a dismissal is found
    regardless of which contact core.duplicates scans first."""
    contact_a = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name='+')
    contact_b = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name='+')
    dismissed_at = models.DateTimeField(auto_now_add=True)
    dismissed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )

    class Meta:
        unique_together = [('contact_a', 'contact_b')]

    @classmethod
    def record(cls, contact_1, contact_2, user=None):
        a, b = sorted([contact_1.pk, contact_2.pk])
        cls.objects.get_or_create(contact_a_id=a, contact_b_id=b, defaults={'dismissed_by': user})

    @classmethod
    def is_dismissed(cls, contact_1, contact_2):
        a, b = sorted([contact_1.pk, contact_2.pk])
        return cls.objects.filter(contact_a_id=a, contact_b_id=b).exists()


class StaffProfile(models.Model):
    class Role(models.TextChoices):
        ADMIN = 'admin', 'Admin'
        PROPERTY_MANAGER = 'property_manager', 'Property Manager'
        MAINTENANCE = 'maintenance', 'Maintenance'
        CLEANER = 'cleaner', 'Cleaner'
        CONTRACTOR = 'contractor', 'Contractor'
        ACCOUNTING = 'accounting', 'Accounting'

    class Timezone(models.TextChoices):
        EASTERN = 'America/New_York', 'Eastern'
        CENTRAL = 'America/Chicago', 'Central'
        MOUNTAIN = 'America/Denver', 'Mountain'
        ARIZONA = 'America/Phoenix', 'Arizona (no DST)'
        PACIFIC = 'America/Los_Angeles', 'Pacific'
        ALASKA = 'America/Anchorage', 'Alaska'
        HAWAII = 'Pacific/Honolulu', 'Hawaii'

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='staff_profile')
    role = models.CharField(
        max_length=20, choices=Role.choices, blank=True,
        help_text='Which team this person is on — also used as the default queue reactive tickets route to.',
    )
    is_company_admin = models.BooleanField(
        default=False,
        help_text='Full admin: the company-wide owner dashboard, checklist/property editing, staff creation, '
                   'and Admin Tools (including API keys/secrets) — equivalent to User.is_superuser. '
                   'Orthogonal to role, which is just a department/queue concept.',
    )
    is_portfolio_owner = models.BooleanField(
        default=False,
        help_text='Gates the private /portfolio/ multi-business dashboard (see the portfolio app) — '
                   'deliberately a separate flag from is_company_admin, which is about the shared '
                   'real-estate business and may be held by more than one person; this one is not '
                   'meant to be. Not linked from the shared site nav.',
    )
    phone = models.CharField(max_length=30, blank=True, validators=[phone_validator])
    timezone = models.CharField(
        max_length=40, choices=Timezone.choices, default=Timezone.EASTERN,
        help_text='Overrides settings.TIME_ZONE for everything this user sees — see core.middleware.TimezoneMiddleware.',
    )

    def __str__(self):
        return self.user.get_full_name() or self.user.username


class PropertyAttribute(models.Model):
    """A tag catalog for property characteristics — services provided,
    physical features, jurisdiction/compliance requirements, or anything
    else operationally relevant. Deliberately one flexible model instead of
    fixed booleans: staff can add a new attribute in admin (e.g. a new
    jurisdiction, a new inspection requirement) without a code change, and
    recurring task templates can require one to auto-apply — see
    tickets.services.applicability."""
    class Category(models.TextChoices):
        SERVICE = 'service', 'Service provided'
        PHYSICAL = 'physical', 'Physical characteristic'
        COMPLIANCE = 'compliance', 'Jurisdiction / compliance'
        OTHER = 'other', 'Other'

    key = models.SlugField(max_length=60, unique=True)
    label = models.CharField(max_length=120)
    category = models.CharField(max_length=20, choices=Category.choices, default=Category.OTHER)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['category', 'label']

    def __str__(self):
        return self.label


class PropertyAttributeAssignment(models.Model):
    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='attribute_assignments')
    attribute = models.ForeignKey(PropertyAttribute, on_delete=models.CASCADE, related_name='property_assignments')
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('property', 'attribute')]

    def __str__(self):
        return f'{self.property} — {self.attribute}'


class GoogleCalendarToken(models.Model):
    """One staff member's own connected Google Calendar (their personal
    account, not the business's shared calendar — see intake/adapters and
    GOOGLE_CALENDAR_CREDENTIALS_PATH for that separate concept). Holds a
    long-lived refresh_token; access_token is short-lived and refreshed
    on demand by core/google_calendar.py."""
    staff = models.OneToOneField(StaffProfile, on_delete=models.CASCADE, related_name='google_calendar_token')
    google_email = models.EmailField(blank=True)
    refresh_token = models.TextField()
    access_token = models.TextField(blank=True)
    access_token_expires_at = models.DateTimeField(null=True, blank=True)
    connected_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    enabled_calendar_ids = models.JSONField(
        default=list, blank=True,
        help_text='Which of this Google account\'s calendars to pull onto the dashboard — empty means '
                   '"just the primary calendar" (the default before anyone has touched the picker).',
    )

    def __str__(self):
        return f'{self.staff} — {self.google_email or "Google Calendar"}'


class QuickBooksToken(models.Model):
    """The company's single connected QuickBooks Online company file — not
    per-staff like GoogleCalendarToken, since there's only one company to
    connect (see core/quickbooks.py). Also caches the last-synced YTD
    financial snapshot the Owner Dashboard reads, rather than calling
    QuickBooks on every page load — refreshed by the daily
    sync_quickbooks_financials job (also run at startup and right after
    connecting; see quickbooks.sync_snapshot). QuickBooks refresh tokens expire after
    ~100 days (unlike Google's), so periodic reconnection is expected."""
    # All three encrypted at rest (AES, see core/fields.py) per Intuit's
    # security requirements — realm_id was a 50-char CharField before, but
    # ciphertext is longer than the value it wraps, so it's a text column now.
    realm_id = EncryptedTextField(help_text='The QuickBooks company ID this token authorizes access to.')
    access_token = EncryptedTextField(blank=True)
    refresh_token = EncryptedTextField()
    access_token_expires_at = models.DateTimeField(null=True, blank=True)
    refresh_token_expires_at = models.DateTimeField(null=True, blank=True)
    connected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
    )
    connected_at = models.DateTimeField(auto_now_add=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    # Most recent sync attempt and, if it failed, a plain-language reason the
    # dashboard/Admin Tools show — otherwise a dead connection (e.g. refresh
    # token expired or revoked) just leaves the numbers quietly going stale.
    last_sync_attempt_at = models.DateTimeField(null=True, blank=True)
    last_sync_error = models.CharField(max_length=255, blank=True)
    accounts_synced_at = models.DateTimeField(null=True, blank=True, help_text='When the chart of accounts was last read.')
    accounts_sync_error = models.CharField(max_length=255, blank=True)
    ledger_synced_at = models.DateTimeField(null=True, blank=True, help_text='When the property transactions were last pulled in.')
    ledger_sync_error = models.CharField(max_length=255, blank=True)
    ytd_revenue = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    ytd_expenses = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    ytd_net_income = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    def __str__(self):
        return f'QuickBooks company {self.realm_id}'


class AppSetting(models.Model):
    """A DB-backed override for one API key/secret, editable from
    /admin-tools/ instead of requiring a code or Railway env var edit —
    see core/app_settings.py, which applies these on top of settings.py's
    env-var defaults. Deliberately just a flat key/value store scoped to
    secrets (see app_settings.SECRET_KEYS) rather than a generic settings
    editor — arbitrary Django settings shouldn't be runtime-editable."""
    key = models.CharField(max_length=100, unique=True)
    value = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )

    def __str__(self):
        return self.key


class BotAccessKey(models.Model):
    """A credential for an outside program — the AI that answers guest messages —
    to read property information and keep a per-property FAQ through the JSON API
    (core/bot_api.py). The key itself is shown once when it is made and only its
    SHA-256 hash is stored, so a database leak can't be replayed; it can be revoked
    at any time. What a key may see is set per key: plain facts (bedrooms, check-in
    time, amenities, where the water shutoff is) are always readable; door and
    lockbox codes, the wifi password and access notes need allow_access_info;
    internal notes, contacts and document names need allow_internal_info; writing
    FAQ entries needs allow_faq_write."""
    name = models.CharField(max_length=120, help_text='What this key is for, e.g. "Guest message bot".')
    key_prefix = models.CharField(max_length=16, help_text='The first few characters, to recognise it in the list.')
    key_hash = models.CharField(max_length=64, unique=True)
    allow_access_info = models.BooleanField(
        default=False, help_text='May read gate/door/lockbox/alarm codes, the wifi password and access notes.',
    )
    allow_internal_info = models.BooleanField(
        default=False, help_text='May read internal notes, contacts (names, phones, emails) and document names.',
    )
    allow_faq_write = models.BooleanField(default=True, help_text='May add and correct FAQ entries.')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    use_count = models.PositiveIntegerField(default=0)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.name} ({self.key_prefix}…)'

    @property
    def is_active(self):
        return self.revoked_at is None

    @staticmethod
    def hash_key(raw):
        import hashlib
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()

    @classmethod
    def issue(cls, name, user=None, allow_access_info=False, allow_internal_info=False, allow_faq_write=True):
        """Makes a key. Returns (BotAccessKey, raw_key); the raw key exists only
        in this return value."""
        import secrets
        raw = 'pmk_' + secrets.token_urlsafe(32)
        key = cls.objects.create(
            name=name.strip()[:120] or 'Unnamed key', key_prefix=raw[:12], key_hash=cls.hash_key(raw), created_by=user,
            allow_access_info=allow_access_info, allow_internal_info=allow_internal_info, allow_faq_write=allow_faq_write,
        )
        return key, raw


class PropertyFAQ(models.Model):
    """A question guests (or owners) ask about a property, and its answer — the
    property's own knowledge base, kept on the property record so it survives
    between sessions of the AI that answers messages. The bot writes entries as it
    learns; staff review, correct or archive them on the property page. An entry a
    person has reviewed (or wrote) is locked against the bot: it can be read and
    used, but the bot can't overwrite it. Door codes, lockbox codes and wifi
    passwords are refused here on purpose (see core/faq.py) — they live in one
    place, the property's access info, so a changed code never leaves a stale
    copy behind in an answer."""
    class Origin(models.TextChoices):
        BOT = 'bot', 'Written by the assistant'
        STAFF = 'staff', 'Written by staff'

    class Basis(models.TextChoices):
        HOST_REPLY = 'host_reply', 'A reply the host sent'
        PROPERTY_RECORD = 'property_record', 'From the property record'
        INFERRED = 'inferred', 'Inferred by the assistant'
        STAFF = 'staff', 'Entered by staff'

    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        ARCHIVED = 'archived', 'Archived'

    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='faqs')
    unit = models.ForeignKey(
        'Unit', on_delete=models.SET_NULL, null=True, blank=True, related_name='faqs',
        help_text='Blank = applies to the whole property; set for an answer that is only true of one unit.',
    )
    question = models.CharField(max_length=300)
    question_key = models.CharField(max_length=320, db_index=True, help_text='The question, normalised, for spotting duplicates.')
    answer = models.TextField()
    origin = models.CharField(max_length=10, choices=Origin.choices, default=Origin.BOT)
    basis = models.CharField(max_length=20, choices=Basis.choices, default=Basis.INFERRED)
    source_note = models.CharField(max_length=200, blank=True, help_text='Where the assistant learned it (never a guest\'s private details).')
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    reviewed = models.BooleanField(default=False, help_text='A person has checked this; the assistant can no longer overwrite it.')
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    times_used = models.PositiveIntegerField(default=0)
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_by_key = models.ForeignKey(BotAccessKey, on_delete=models.SET_NULL, null=True, blank=True, related_name='faqs')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['question']
        verbose_name = 'property FAQ entry'
        verbose_name_plural = 'property FAQ entries'
        constraints = [
            models.UniqueConstraint(
                fields=['property', 'question_key'], condition=models.Q(status='active', unit__isnull=True),
                name='uniq_active_faq_property',
            ),
            models.UniqueConstraint(
                fields=['property', 'unit', 'question_key'], condition=models.Q(status='active', unit__isnull=False),
                name='uniq_active_faq_unit',
            ),
        ]

    def __str__(self):
        return f'{self.property}: {self.question}'

    def locked_against_bot(self):
        """True when a person has reviewed or written this, so the assistant may
        read and use it but not overwrite it. (A method, not a property: this
        model has a field called `property`, which shadows the decorator.)"""
        return self.reviewed or self.origin == self.Origin.STAFF


class QuickBooksAccount(models.Model):
    """One account from the QuickBooks chart of accounts, kept locally so a
    property can be tied to two of them without a live QuickBooks call on every
    page (refreshed by the daily sync and by a button on the mapping screens).

    `classification` is QuickBooks's own split: Asset / Liability / Equity sit on
    the balance sheet, Revenue / Expense on the income statement. An account that
    disappears from QuickBooks is kept (so a mapping to it stays visible) but
    marked inactive."""
    class Classification(models.TextChoices):
        ASSET = 'Asset', 'Asset'
        LIABILITY = 'Liability', 'Liability'
        EQUITY = 'Equity', 'Equity'
        REVENUE = 'Revenue', 'Revenue'
        EXPENSE = 'Expense', 'Expense'

    BALANCE_SHEET = ('Asset', 'Liability', 'Equity')
    INCOME_STATEMENT = ('Revenue', 'Expense')

    qb_id = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=300)
    fully_qualified_name = models.CharField(max_length=500, help_text='The account with its parents, e.g. "Trust Accounts:324 Harmon".')
    account_type = models.CharField(max_length=60, blank=True)
    account_sub_type = models.CharField(max_length=80, blank=True)
    classification = models.CharField(max_length=20, blank=True)
    active = models.BooleanField(default=True)
    synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['fully_qualified_name']

    def __str__(self):
        return self.fully_qualified_name or self.name

    @property
    def on_balance_sheet(self):
        return self.classification in self.BALANCE_SHEET

    @property
    def on_income_statement(self):
        return self.classification in self.INCOME_STATEMENT


class FinancialsSettings(models.Model):
    """Singleton: where the books being managed here begin. Transactions before
    this month are never pulled in or asked about (a year of history would mean a
    year of closes)."""
    books_start = models.DateField(null=True, blank=True, help_text='The first month to manage; earlier transactions are ignored.')

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class LedgerLine(models.Model):
    """One QuickBooks transaction as it hits one of a rental's two mapped accounts
    (the sum of its lines in that account), pulled in so a person can code it at
    month end. QuickBooks is the system of record: the identity is QuickBooks's own
    (account role, transaction type, transaction id), so a resync recognises the same
    transaction, updates it if it changed and keeps its coding, and marks it
    removed if it left the account (voided, deleted, or re-coded to another
    property, where it turns up as a new line). Once the month is closed the line is
    locked and the sync no longer touches it.

    `flow` is signed so that for the expense account positive = an expense charged
    (a debit) and negative = a credit; for the trust account positive = money into
    the trust and negative = money out."""
    class Role(models.TextChoices):
        EXPENSE = 'expense', 'Reimbursable-expense account'
        TRUST = 'trust', 'Owner trust account'

    class Category(models.TextChoices):
        EXPENSE = 'expense', 'Expense'
        DEPOSIT = 'deposit', 'Income deposit (booking payout)'
        OWNER_PAYMENT = 'owner_payment', 'Owner payment'
        REIMBURSEMENT = 'reimbursement', 'Expense reimbursement'
        COMMISSION = 'commission', 'Commission'

    class Source(models.TextChoices):
        DEFAULT = 'default', 'Default'
        SUGGESTED = 'suggested', 'Suggested'
        USER = 'user', 'Coded by a person'

    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        REMOVED = 'removed', 'Gone from QuickBooks'

    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='ledger_lines')
    unit = models.ForeignKey(
        Unit, on_delete=models.PROTECT, null=True, blank=True, related_name='ledger_lines',
        help_text='Set when the property keeps unit-level books: the unit whose accounts this line belongs to.',
    )
    role = models.CharField(max_length=10, choices=Role.choices)
    account = models.ForeignKey(QuickBooksAccount, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    txn_type = models.CharField(max_length=60)
    txn_id = models.CharField(max_length=40)
    txn_date = models.DateField()
    month = models.DateField(db_index=True, help_text='The first day of the month the transaction falls in.')
    doc_num = models.CharField(max_length=60, blank=True)
    payee = models.CharField(max_length=300, blank=True)
    memo = models.CharField(max_length=500, blank=True)
    split = models.CharField(max_length=300, blank=True, help_text='The other account(s) of the transaction, as QuickBooks shows them.')
    description = models.CharField(max_length=500, blank=True, help_text="Our own wording for this transaction, when QuickBooks's memo needs correcting. QuickBooks's memo stays beside it and a sync never overwrites this.")
    flow = models.DecimalField(max_digits=12, decimal_places=2)
    fingerprint = models.CharField(max_length=40, blank=True)

    category = models.CharField(max_length=20, choices=Category.choices, default=Category.EXPENSE)
    category_source = models.CharField(max_length=10, choices=Source.choices, default=Source.DEFAULT)
    reviewed = models.BooleanField(default=False, help_text='A person has looked at this line and accepted its category.')
    coded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    coded_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=300, blank=True)

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    removed_at = models.DateTimeField(null=True, blank=True)
    changed_in_qb = models.BooleanField(default=False, help_text='QuickBooks changed the amount, date or account after this was pulled in; a person should glance at it.')
    change_note = models.CharField(max_length=300, blank=True)
    locked_at = models.DateTimeField(null=True, blank=True, help_text='Set when the month is closed; a locked line never changes.')

    @builtins.property      # (this class has a field called `property`)
    def shown_memo(self):
        """What the screens say the transaction is: our wording if we corrected QuickBooks's, else QuickBooks's memo."""
        return self.description or self.memo

    class Meta:
        ordering = ['txn_date', 'txn_type', 'txn_id']
        constraints = [
            models.UniqueConstraint(fields=['property', 'role', 'txn_type', 'txn_id'], condition=models.Q(unit__isnull=True), name='uniq_ledger_line'),
            models.UniqueConstraint(fields=['unit', 'role', 'txn_type', 'txn_id'], condition=models.Q(unit__isnull=False), name='uniq_ledger_line_unit'),
        ]
        indexes = [models.Index(fields=['property', 'month'])]

    def __str__(self):
        return f'{self.property} {self.txn_date} {self.txn_type} {self.flow}'


class MonthClose(models.Model):
    """A rental's month, closed. From then on its transactions are frozen: the
    figures were used to pay the owner, so QuickBooks changes to that month do not
    flow in (they are logged as ClosedMonthChange). A mistake found later is fixed
    in the current month."""
    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='month_closes')
    unit = models.ForeignKey(Unit, on_delete=models.PROTECT, null=True, blank=True, related_name='month_closes')
    level = models.CharField(
        max_length=10, choices=Property.FinancialsLevel.choices, default=Property.FinancialsLevel.PROPERTY,
        help_text='The shape the month was closed in: one set of books for the property, or one per unit.',
    )
    month = models.DateField(help_text='The first day of the month.')
    closed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    closed_at = models.DateTimeField(auto_now_add=True)
    totals = models.JSONField(default=dict, help_text='The figures as closed, frozen.')
    recon = models.JSONField(default=dict, blank=True, help_text='The income reconciliation as closed, frozen.')
    warnings_acknowledged = models.JSONField(default=list, blank=True)
    note = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ['-month']
        constraints = [
            models.UniqueConstraint(fields=['property', 'month'], condition=models.Q(unit__isnull=True), name='uniq_month_close'),
            models.UniqueConstraint(fields=['unit', 'month'], condition=models.Q(unit__isnull=False), name='uniq_month_close_unit'),
        ]

    def __str__(self):
        return f'{self.unit or self.property} {self.month:%B %Y} closed'


class ReopenedClose(models.Model):
    """A closed month that was reopened, kept as it was closed: the frozen figures and reconciliation are copied here
    before the close is removed, so reopening a month (to redo it) never loses what was once signed off."""
    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='reopened_closes')
    unit = models.ForeignKey(Unit, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    level = models.CharField(max_length=10, blank=True)
    month = models.DateField()
    closed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    closed_at = models.DateTimeField(null=True, blank=True)
    totals = models.JSONField(default=dict)
    recon = models.JSONField(default=dict, blank=True)
    warnings_acknowledged = models.JSONField(default=list, blank=True)
    note = models.CharField(max_length=500, blank=True)
    reopened_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    reopened_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-reopened_at']

    def __str__(self):
        return f'{self.unit or self.property} {self.month:%B %Y} reopened'


class ReconAcceptance(models.Model):
    """A person accepted one unmatched item in a month's income reconciliation as a
    reconciling item, with the reason (a platform payout still on its way to the
    bank, a deposit that is not from a platform, ...). Tied to the amount it was
    accepted at: if that changes the acceptance no longer applies."""
    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='recon_acceptances')
    unit = models.ForeignKey(Unit, on_delete=models.PROTECT, null=True, blank=True, related_name='recon_acceptances')
    month = models.DateField()
    kind = models.CharField(max_length=10, help_text='"deposit" (in the bank, no platform payout) or "payout" (a platform payout not in the bank).')
    key = models.CharField(max_length=80)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    description = models.CharField(max_length=300, blank=True)
    note = models.CharField(max_length=300)
    prior_period = models.BooleanField(default=False, help_text='A deposit that pays out something from before the books start (the first month could not have had it carried forward).')
    class Reason(models.TextChoices):
        TIMING = 'timing', 'Timing — paid out or deposited in another month'
        ERROR = 'error', 'Bookkeeping error / something to chase'
        PRIOR_BOOKS = 'prior_books', 'From before the books'
    reason = models.CharField(max_length=12, choices=Reason.choices, blank=True, help_text='Why it is a reconciling item: a timing difference that clears itself, or a genuine bookkeeping error.')
    accepted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    accepted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['accepted_at']
        indexes = [models.Index(fields=['property', 'month'])]


class ReconMatch(models.Model):
    """A person's decision about lines in a month's income reconciliation that the program should not decide on its own.
    kind USER: these bank `lines` (LedgerLine ids) and these platform `events` ([booking id, day] payouts) ARE the same
    money, matched by hand — any number on either side (one payout landing as two deposits, two payouts as one, ...).
    kind HOLD: the program had matched these and the person broke that match, so they stay open (and are not
    re-matched automatically) until matched by hand. Supersedes ReconTie, which could only tie ONE deposit to payouts."""
    class Kind(models.TextChoices):
        USER = 'user', 'Matched by hand'
        HOLD = 'hold', 'Kept open'
    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='recon_matches')
    unit = models.ForeignKey(Unit, on_delete=models.PROTECT, null=True, blank=True, related_name='recon_matches')
    month = models.DateField()
    kind = models.CharField(max_length=6, choices=Kind.choices, default=Kind.USER)
    lines = models.JSONField(default=list)
    events = models.JSONField(default=list)
    note = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['property', 'month'])]


class ReconTie(models.Model):
    """SUPERSEDED by ReconMatch (existing rows were copied into it); no longer read or written. A person tied one bank deposit to platform payouts BY HAND, when the reconciliation could not: it lists the
    reservations around the month and they pick the ones the deposit is made of. `events` are the dated payouts
    chosen, [booking id, day]; `amount` is the deposit's amount when it was tied (the tie stops applying if that
    changes). If the payouts do not add up to the deposit the difference is still an item to accept with a reason."""
    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='recon_ties')
    unit = models.ForeignKey(Unit, on_delete=models.PROTECT, null=True, blank=True, related_name='recon_ties')
    month = models.DateField()
    line = models.OneToOneField('LedgerLine', on_delete=models.CASCADE, related_name='recon_tie')
    events = models.JSONField(default=list)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    note = models.CharField(max_length=300, blank=True)
    tied_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    tied_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['property', 'month'])]


class ClosedMonthChange(models.Model):
    """QuickBooks differs from the closed books: a transaction in a closed month
    was added, changed or removed after the close. It is NOT applied — the closed
    numbers stand — but recorded so the team can see QuickBooks and the books have
    drifted apart and put the correction in the current month."""
    class Kind(models.TextChoices):
        NEW = 'new', 'Added in QuickBooks'
        CHANGED = 'changed', 'Changed in QuickBooks'
        REMOVED = 'removed', 'Removed from QuickBooks'

    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='closed_month_changes')
    unit = models.ForeignKey(Unit, on_delete=models.PROTECT, null=True, blank=True, related_name='closed_month_changes')
    month = models.DateField()
    role = models.CharField(max_length=10)
    txn_type = models.CharField(max_length=60)
    txn_id = models.CharField(max_length=40)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    detail = models.CharField(max_length=400, blank=True)
    detected_at = models.DateTimeField(auto_now_add=True)
    resolved = models.BooleanField(default=False)

    class Meta:
        ordering = ['-detected_at']
        constraints = [
            models.UniqueConstraint(fields=['property', 'role', 'txn_type', 'txn_id', 'kind'], condition=models.Q(unit__isnull=True), name='uniq_closed_month_change'),
            models.UniqueConstraint(fields=['unit', 'role', 'txn_type', 'txn_id', 'kind'], condition=models.Q(unit__isnull=False), name='uniq_closed_month_change_unit'),
        ]



class TrashSchedule(models.Model):
    """A property's trash and recycling pickup days — one schedule per property. Setting a new
    one replaces the old (see core/trash.py); the individual pickups are its TrashRules."""
    property = models.OneToOneField(Property, on_delete=models.CASCADE, related_name='trash_schedule')
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    def __str__(self):
        return f'Trash schedule — {self.property}'


class TrashRule(models.Model):
    """One kind of pickup and the weekdays it happens (0 = Monday ... 6 = Sunday)."""
    class Kind(models.TextChoices):
        TRASH = 'trash', 'Regular trash'
        BULK = 'bulk', 'Bulk pickup'
        RECYCLING = 'recycling', 'Recycling'
        CUSTOM = 'custom', 'Custom'

    schedule = models.ForeignKey(TrashSchedule, on_delete=models.CASCADE, related_name='rules')
    kind = models.CharField(max_length=12, choices=Kind.choices)
    label = models.CharField(max_length=60, blank=True, help_text='The name of a custom pickup (vegetation, yard waste, ...); blank for the standard kinds.')
    days = models.JSONField(default=list, help_text='Weekday numbers, 0 = Monday ... 6 = Sunday.')

    class Meta:
        ordering = ['id']
        constraints = [
            models.UniqueConstraint(fields=['schedule', 'kind'], condition=~models.Q(kind='custom'), name='uniq_trash_rule_kind'),
        ]

    @property
    def name(self):
        return self.label if self.kind == self.Kind.CUSTOM and self.label else self.get_kind_display()

    def __str__(self):
        return f'{self.name}: {self.days}'


class ListingLink(models.Model):
    """The Airbnb or VRBO page of one unit (or of a single-unit property), and its guest rating,
    read from the page about once a month (see core/listings.py) — or typed in by hand when the
    platform won't let a program read it."""
    class Platform(models.TextChoices):
        AIRBNB = 'airbnb', 'Airbnb'
        VRBO = 'vrbo', 'VRBO'

    class Source(models.TextChoices):
        AUTO = 'auto', 'Read from the listing'
        MANUAL = 'manual', 'Entered by hand'

    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='listing_links')
    unit = models.ForeignKey(Unit, on_delete=models.CASCADE, null=True, blank=True, related_name='listing_links')
    platform = models.CharField(max_length=10, choices=Platform.choices)
    url = models.URLField(max_length=500)
    rating = models.DecimalField(max_digits=3, decimal_places=2, null=True, blank=True)
    review_count = models.PositiveIntegerField(null=True, blank=True)
    rating_source = models.CharField(max_length=10, choices=Source.choices, blank=True)
    rating_checked_at = models.DateTimeField(null=True, blank=True, help_text='When the rating was last successfully read or entered.')
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    check_error = models.CharField(max_length=200, blank=True, help_text='Why the last automatic read did not work, if it did not.')
    failures = models.PositiveSmallIntegerField(default=0, help_text='Automatic reads in a row that did not work.')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['platform']
        constraints = [
            models.UniqueConstraint(fields=['property', 'platform'], condition=models.Q(unit__isnull=True), name='uniq_listing_link'),
            models.UniqueConstraint(fields=['unit', 'platform'], condition=models.Q(unit__isnull=False), name='uniq_listing_link_unit'),
        ]

    def __str__(self):
        return f'{self.get_platform_display()} — {self.unit or self.property}'


class ListingRating(models.Model):
    """One reading of a listing's rating, kept so the trend is visible."""
    link = models.ForeignKey(ListingLink, on_delete=models.CASCADE, related_name='history')
    rating = models.DecimalField(max_digits=3, decimal_places=2)
    review_count = models.PositiveIntegerField(null=True, blank=True)
    source = models.CharField(max_length=10, choices=ListingLink.Source.choices, default=ListingLink.Source.AUTO)
    checked_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-checked_at']
