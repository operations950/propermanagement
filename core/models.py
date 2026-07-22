from django.conf import settings
from django.db import models
from django.db.models import Case, IntegerField, Value, When


class Property(models.Model):
    class Type(models.TextChoices):
        GENERAL = 'general', 'General'
        ASSOCIATION = 'association', 'Associations'
        SHORT_TERM_RENTAL = 'str', 'Short-Term Rentals'
        LONG_TERM_RENTAL = 'ltr', 'Long-Term Rentals'
        COMMERCIAL = 'commercial', 'Commercial'

    name = models.CharField(max_length=200)
    address = models.CharField(max_length=300, blank=True)
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


def property_dropdown_queryset():
    """Properties ordered for a grouped dropdown: General, then Associations,
    Short-Term Rentals, Long-Term Rentals, Commercial — with each type's
    general/non-specific placeholder sorted first within its group. Used
    with {% regroup %} on get_property_type_display in templates."""
    type_order = Case(
        When(property_type=Property.Type.GENERAL, then=Value(0)),
        When(property_type=Property.Type.ASSOCIATION, then=Value(1)),
        When(property_type=Property.Type.SHORT_TERM_RENTAL, then=Value(2)),
        When(property_type=Property.Type.LONG_TERM_RENTAL, then=Value(3)),
        When(property_type=Property.Type.COMMERCIAL, then=Value(4)),
        default=Value(5), output_field=IntegerField(),
    )
    return (
        Property.objects.filter(is_active=True)
        .annotate(_type_order=type_order)
        .order_by('_type_order', '-is_general', 'name')
    )


class Contact(models.Model):
    class ContactType(models.TextChoices):
        GUEST = 'guest', 'Guest'
        TENANT = 'tenant', 'Tenant'
        VENDOR = 'vendor', 'Vendor / Contractor'
        STAFF_ADJACENT = 'staff_adjacent', 'Staff-adjacent'
        OTHER = 'other', 'Other'

    name = models.CharField(max_length=200)
    contact_type = models.CharField(max_length=20, choices=ContactType.choices, default=ContactType.OTHER)
    trade = models.CharField(
        max_length=100, blank=True,
        help_text='For vendors: e.g. plumbing, HVAC, cleaning, handyman',
    )
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    property = models.ForeignKey(
        Property, on_delete=models.SET_NULL, null=True, blank=True, related_name='contacts',
        help_text='Optional: the property this contact is primarily associated with (e.g. a tenant or a guest).',
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'{self.name} ({self.get_contact_type_display()})'


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
    phone = models.CharField(max_length=30, blank=True)

    def __str__(self):
        return self.user.get_full_name() or self.user.username


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
