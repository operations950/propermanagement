from django.contrib import admin

from .models import (
    Contact, GoogleCalendarToken, Property, PropertyAttribute, PropertyAttributeAssignment, StaffProfile,
)


class PropertyAttributeAssignmentInline(admin.TabularInline):
    model = PropertyAttributeAssignment
    extra = 1


@admin.register(Property)
class PropertyAdmin(admin.ModelAdmin):
    list_display = ['name', 'property_type', 'is_general', 'address', 'is_active', 'created_at']
    list_filter = ['property_type', 'is_general', 'is_active']
    search_fields = ['name', 'address']
    inlines = [PropertyAttributeAssignmentInline]


@admin.register(PropertyAttribute)
class PropertyAttributeAdmin(admin.ModelAdmin):
    list_display = ['label', 'key', 'category', 'is_active']
    list_filter = ['category', 'is_active']
    search_fields = ['label', 'key']
    prepopulated_fields = {'key': ('label',)}


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = ['name', 'contact_type', 'trade', 'phone', 'email', 'property']
    list_filter = ['contact_type']
    search_fields = ['name', 'phone', 'email']


@admin.register(StaffProfile)
class StaffProfileAdmin(admin.ModelAdmin):
    list_display = ['user', 'role', 'phone']
    list_filter = ['role']


@admin.register(GoogleCalendarToken)
class GoogleCalendarTokenAdmin(admin.ModelAdmin):
    list_display = ['staff', 'google_email', 'connected_at', 'updated_at']
    readonly_fields = ['refresh_token', 'access_token', 'access_token_expires_at', 'connected_at', 'updated_at']
