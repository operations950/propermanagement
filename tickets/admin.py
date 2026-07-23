from django import forms
from django.contrib import admin

from core.models import Property

from .models import (
    FollowUpLog,
    PackageRun,
    PropertyPackage,
    PropertyTemplateOverride,
    TaskPackage,
    TaskPackageTemplate,
    TemplateChecklistItem,
    TemplateOccurrence,
    Ticket,
    TicketAssignmentLog,
    TicketAttachment,
    TicketChecklistItem,
    TicketContact,
    TicketTemplate,
)


class TicketContactInline(admin.TabularInline):
    model = TicketContact
    extra = 1


class TicketAttachmentInline(admin.TabularInline):
    model = TicketAttachment
    extra = 0
    readonly_fields = ['created_at']


class TicketChecklistItemInline(admin.TabularInline):
    model = TicketChecklistItem
    extra = 0
    readonly_fields = ['checked_at', 'checked_by']


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = [
        'title', 'property', 'assigned_role', 'status', 'priority', 'assignee_label', 'due_date',
        'source',
    ]
    list_filter = ['status', 'priority', 'source', 'property', 'assigned_role']
    search_fields = ['title', 'description', 'raw_context', 'source_reference']
    readonly_fields = ['completion_token', 'completion_token_expires_at', 'created_at', 'updated_at']
    inlines = [TicketContactInline, TicketAttachmentInline, TicketChecklistItemInline]


class TicketTemplateAdminForm(forms.ModelForm):
    property_types = forms.MultipleChoiceField(
        choices=Property.Type.choices, required=False, widget=forms.CheckboxSelectMultiple,
        help_text='Leave every box unchecked for "every type" (no constraint from this layer).',
    )

    class Meta:
        model = TicketTemplate
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.initial['property_types'] = self.instance.property_types

    def clean_property_types(self):
        return list(self.cleaned_data['property_types'])


class TemplateChecklistItemInline(admin.TabularInline):
    model = TemplateChecklistItem
    extra = 0


@admin.register(TicketTemplate)
class TicketTemplateAdmin(admin.ModelAdmin):
    form = TicketTemplateAdminForm
    list_display = [
        'title', 'property', 'default_assigned_role', 'frequency', 'workday_of_month', 'next_run_date',
        'requires_approval', 'is_active',
    ]
    list_filter = ['frequency', 'is_active', 'default_assigned_role', 'requires_approval']
    filter_horizontal = ['required_attributes']
    inlines = [TemplateChecklistItemInline]


class TaskPackageTemplateInline(admin.TabularInline):
    model = TaskPackageTemplate
    fk_name = 'package'
    extra = 1


@admin.register(TaskPackage)
class TaskPackageAdmin(admin.ModelAdmin):
    list_display = ['title', 'is_active']
    list_filter = ['is_active']
    search_fields = ['title']
    inlines = [TaskPackageTemplateInline]


@admin.register(PropertyPackage)
class PropertyPackageAdmin(admin.ModelAdmin):
    list_display = ['property', 'package', 'assigned_at']
    list_filter = ['package']
    search_fields = ['property__name']


@admin.register(PropertyTemplateOverride)
class PropertyTemplateOverrideAdmin(admin.ModelAdmin):
    list_display = ['property', 'template', 'action', 'frequency', 'assigned_role', 'assigned_staff']
    list_filter = ['action']
    search_fields = ['property__name', 'template__title']


@admin.register(TemplateOccurrence)
class TemplateOccurrenceAdmin(admin.ModelAdmin):
    list_display = ['template', 'scheduled_for']
    list_filter = ['scheduled_for']
    search_fields = ['template__title']


@admin.register(PackageRun)
class PackageRunAdmin(admin.ModelAdmin):
    list_display = ['package', 'property', 'scheduled_for']
    list_filter = ['package', 'scheduled_for']
    search_fields = ['package__title', 'property__name']


@admin.register(TicketAssignmentLog)
class TicketAssignmentLogAdmin(admin.ModelAdmin):
    list_display = ['ticket', 'from_staff', 'from_contact', 'to_staff', 'to_contact', 'changed_at']
    readonly_fields = [f.name for f in TicketAssignmentLog._meta.fields]


@admin.register(FollowUpLog)
class FollowUpLogAdmin(admin.ModelAdmin):
    list_display = ['ticket', 'channel', 'sent_to', 'sent_at', 'success']
    readonly_fields = [f.name for f in FollowUpLog._meta.fields]
