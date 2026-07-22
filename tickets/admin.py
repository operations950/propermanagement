from django.contrib import admin

from .models import (
    FollowUpLog,
    Ticket,
    TicketAssignmentLog,
    TicketAttachment,
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


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = [
        'title', 'property', 'assigned_role', 'status', 'priority', 'assignee_label', 'due_date',
        'source',
    ]
    list_filter = ['status', 'priority', 'source', 'property', 'assigned_role']
    search_fields = ['title', 'description', 'raw_context', 'source_reference']
    readonly_fields = ['completion_token', 'completion_token_expires_at', 'created_at', 'updated_at']
    inlines = [TicketContactInline, TicketAttachmentInline]


@admin.register(TicketTemplate)
class TicketTemplateAdmin(admin.ModelAdmin):
    list_display = [
        'title', 'property', 'default_assigned_role', 'frequency', 'workday_of_month', 'next_run_date',
        'is_active',
    ]
    list_filter = ['frequency', 'is_active', 'default_assigned_role']


@admin.register(TicketAssignmentLog)
class TicketAssignmentLogAdmin(admin.ModelAdmin):
    list_display = ['ticket', 'from_staff', 'from_contact', 'to_staff', 'to_contact', 'changed_at']
    readonly_fields = [f.name for f in TicketAssignmentLog._meta.fields]


@admin.register(FollowUpLog)
class FollowUpLogAdmin(admin.ModelAdmin):
    list_display = ['ticket', 'channel', 'sent_to', 'sent_at', 'success']
    readonly_fields = [f.name for f in FollowUpLog._meta.fields]
