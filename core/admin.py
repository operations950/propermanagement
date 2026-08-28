from django.contrib import admin

from .admin_utils import mask_secret
from .models import (
    Contact, ContactImportCandidate, GoogleCalendarToken, Property, PropertyAttribute,
    PropertyAttributeAssignment, PropertyListingName, PropertySystemLocation, StaffProfile, Unit,
)


class PropertyAttributeAssignmentInline(admin.TabularInline):
    model = PropertyAttributeAssignment
    extra = 1


class PropertySystemLocationInline(admin.TabularInline):
    model = PropertySystemLocation
    extra = 1


class PropertyListingNameInline(admin.TabularInline):
    model = PropertyListingName
    extra = 1


class UnitInline(admin.TabularInline):
    model = Unit
    extra = 1


@admin.register(Property)
class PropertyAdmin(admin.ModelAdmin):
    list_display = ['name', 'property_type', 'is_general', 'address', 'address_verified', 'is_active', 'created_at']
    list_filter = ['property_type', 'is_general', 'address_verified', 'is_active']
    search_fields = ['name', 'address']
    inlines = [UnitInline, PropertyAttributeAssignmentInline, PropertySystemLocationInline, PropertyListingNameInline]


@admin.register(Unit)
class UnitAdmin(admin.ModelAdmin):
    list_display = ['label', 'property', 'is_active', 'created_at']
    list_filter = ['is_active']
    search_fields = ['label', 'property__name']


@admin.register(PropertyListingName)
class PropertyListingNameAdmin(admin.ModelAdmin):
    list_display = ['name', 'platform', 'property', 'created_at']
    list_filter = ['platform']
    search_fields = ['name', 'property__name']


@admin.register(PropertyAttribute)
class PropertyAttributeAdmin(admin.ModelAdmin):
    list_display = ['label', 'key', 'category', 'is_active']
    list_filter = ['category', 'is_active']
    search_fields = ['label', 'key']
    prepopulated_fields = {'key': ('label',)}


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = ['name', 'contact_type', 'trade', 'phone', 'email', 'properties_display', 'source']
    list_filter = ['contact_type', 'source']
    search_fields = ['name', 'phone', 'email']

    @admin.display(description='Properties')
    def properties_display(self, obj):
        return ', '.join(p.name for p in obj.properties.all()) or '—'


@admin.register(ContactImportCandidate)
class ContactImportCandidateAdmin(admin.ModelAdmin):
    list_display = ['name', 'source', 'phone', 'email', 'status', 'created_at']
    list_filter = ['source', 'status']
    search_fields = ['name', 'phone', 'email']
    readonly_fields = ['created_at', 'resolved_at', 'resolved_by', 'resolved_contact']


@admin.register(StaffProfile)
class StaffProfileAdmin(admin.ModelAdmin):
    list_display = ['user', 'role', 'phone']
    list_filter = ['role']


@admin.register(GoogleCalendarToken)
class GoogleCalendarTokenAdmin(admin.ModelAdmin):
    list_display = ['staff', 'google_email', 'connected_at', 'updated_at']
    # exclude, not just readonly_fields: readonly_fields alone only swaps a
    # field's edit widget for read-only text — the real refresh_token/
    # access_token model fields would still otherwise render in the form
    # (as plain EDITABLE text inputs, arguably worse) since they're not
    # otherwise excluded. The masked *_masked methods below are the only
    # representation of these two fields shown in admin now.
    exclude = ['refresh_token', 'access_token']
    readonly_fields = [
        'refresh_token_masked', 'access_token_masked', 'access_token_expires_at', 'connected_at', 'updated_at',
    ]

    @admin.display(description='Refresh token')
    def refresh_token_masked(self, obj):
        return mask_secret(obj.refresh_token)

    @admin.display(description='Access token')
    def access_token_masked(self, obj):
        return mask_secret(obj.access_token)
