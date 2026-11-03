from django.contrib import admin

from core.admin_utils import mask_secret

from .models import (
    DepartmentDefaultAssignee,
    FollowUpLog,
    Ticket,
    TicketAssignmentLog,
    TicketAttachment,
    TicketContact,
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
        'source', 'assignment_source',
    ]
    list_filter = ['status', 'priority', 'source', 'property', 'assigned_role', 'assignment_source']
    search_fields = ['title', 'description', 'raw_context', 'source_reference']
    # completion_token is the bearer token for the unauthenticated public
    # vendor completion link (/vendor/t/<token>/, grants mark-complete +
    # photo upload on this ticket) — masked here rather than shown in
    # full the way readonly_fields normally would, same as the OAuth
    # token fields in core/intake's admin.py. No `exclude` needed for
    # this one specifically: completion_token is editable=False on the
    # model itself, so Django's admin never auto-includes it as an
    # editable widget in the first place — only listing it in
    # readonly_fields makes it visible at all, and it's now the masked
    # method instead of the raw field.
    readonly_fields = ['completion_token_masked', 'completion_token_expires_at', 'created_at', 'updated_at']
    inlines = [TicketContactInline, TicketAttachmentInline]

    @admin.display(description='Completion token')
    def completion_token_masked(self, obj):
        return mask_secret(obj.completion_token)


@admin.register(DepartmentDefaultAssignee)
class DepartmentDefaultAssigneeAdmin(admin.ModelAdmin):
    list_display = ['role', 'staff']


@admin.register(TicketAssignmentLog)
class TicketAssignmentLogAdmin(admin.ModelAdmin):
    list_display = ['ticket', 'from_staff', 'from_contact', 'to_staff', 'to_contact', 'changed_at']
    readonly_fields = [f.name for f in TicketAssignmentLog._meta.fields]


@admin.register(FollowUpLog)
class FollowUpLogAdmin(admin.ModelAdmin):
    list_display = ['ticket', 'property', 'channel', 'sent_to', 'sent_at', 'success']
    readonly_fields = [f.name for f in FollowUpLog._meta.fields]
