import re

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Case, IntegerField, Value, When

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

    class Meta:
        verbose_name_plural = 'properties'
        ordering = ['name']

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.street and self.city and self.state and self.zip_code:
            self.address = f'{self.street}, {self.city}, {self.state} {self.zip_code}'
        super().save(*args, **kwargs)


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


class Contact(models.Model):
    class ContactType(models.TextChoices):
        GUEST = 'guest', 'Guest'
        TENANT = 'tenant', 'Tenant'
        OWNER = 'owner', 'Owner'
        BOARD_MEMBER = 'board_member', 'Board Member'
        ASSOCIATION_MEMBER = 'association_member', 'Association Member'
        VENDOR = 'vendor', 'Vendor / Contractor'
        STAFF_ADJACENT = 'staff_adjacent', 'Staff-adjacent'
        OTHER = 'other', 'Other'

    class Source(models.TextChoices):
        MANUAL = 'manual', 'Manual'
        QUO = 'quo', 'Quo'
        GMAIL = 'gmail', 'Gmail'

    name = models.CharField(max_length=200)
    contact_type = models.CharField(max_length=20, choices=ContactType.choices, default=ContactType.OTHER)
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
    source = models.CharField(
        max_length=20, choices=Source.choices, default=Source.MANUAL,
        help_text='Where this contact came from — set automatically, kept for provenance/audit.',
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'{self.name} ({self.get_contact_type_display()})'


class ContactImportCandidate(models.Model):
    """A contact harvested from a bulk Quo/Gmail import, held here — not in
    the real Contact table — until a human reviews and approves it. Hard
    gate by design: nothing from an import is usable anywhere in the app
    (ticket pickers, property pages, assignment) until it's promoted. See
    core/views.py's contact_review/_approve/_reject and the
    import_quo_contacts/import_gmail_contacts management commands.

    Deliberately no unique constraint on phone/email — both are optional
    here, and dedup against existing Contacts/other pending candidates is
    a functional check in the importer, not a DB guarantee (same pragmatic
    approach as the inline add-contact flow on New Ticket)."""
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending review'
        APPROVED = 'approved', 'Approved'
        REJECTED = 'rejected', 'Rejected'

    source = models.CharField(max_length=20, choices=Contact.Source.choices)
    name = models.CharField(max_length=200, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    trade = models.CharField(max_length=100, blank=True)
    suggested_contact_type = models.CharField(
        max_length=20, choices=Contact.ContactType.choices, default=Contact.ContactType.OTHER,
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


class StaffProfile(models.Model):
    class Role(models.TextChoices):
        ADMIN = 'admin', 'Admin'
        PROPERTY_MANAGER = 'property_manager', 'Property Manager'
        MAINTENANCE = 'maintenance', 'Maintenance'
        CLEANER = 'cleaner', 'Cleaner'
        CONTRACTOR = 'contractor', 'Contractor'
        ACCOUNTING = 'accounting', 'Accounting'

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='staff_profile')
    role = models.CharField(
        max_length=20, choices=Role.choices, blank=True,
        help_text='Which team this person is on — also used as the default queue reactive tickets route to.',
    )
    phone = models.CharField(max_length=30, blank=True, validators=[phone_validator])

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

    def __str__(self):
        return f'{self.staff} — {self.google_email or "Google Calendar"}'
