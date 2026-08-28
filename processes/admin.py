from django.contrib import admin

from core.admin_utils import mask_secret

from .models import (
    ProcessAttachment,
    ProcessRun,
    ProcessRunExternalAccess,
    ProcessRunStep,
    ProcessTemplate,
    ProcessTemplateAttachment,
    ProcessTemplateStep,
)


class ProcessTemplateStepInline(admin.TabularInline):
    model = ProcessTemplateStep
    extra = 0


class ProcessTemplateAttachmentInline(admin.TabularInline):
    model = ProcessTemplateAttachment
    extra = 0


@admin.register(ProcessTemplate)
class ProcessTemplateAdmin(admin.ModelAdmin):
    list_display = ['name', 'category', 'is_active', 'created_at']
    list_filter = ['is_active', 'category']
    search_fields = ['name', 'description']
    inlines = [ProcessTemplateStepInline, ProcessTemplateAttachmentInline]


class ProcessRunStepInline(admin.TabularInline):
    model = ProcessRunStep
    extra = 0
    readonly_fields = ['completed_at', 'completed_by']


@admin.register(ProcessRun)
class ProcessRunAdmin(admin.ModelAdmin):
    list_display = ['process_template', 'get_target', 'status', 'created_at', 'completed_at']
    list_filter = ['process_template', 'status']
    inlines = [ProcessRunStepInline]


@admin.register(ProcessAttachment)
class ProcessAttachmentAdmin(admin.ModelAdmin):
    list_display = ['run_step', 'caption', 'created_at']


@admin.register(ProcessRunExternalAccess)
class ProcessRunExternalAccessAdmin(admin.ModelAdmin):
    # token is the bearer credential for this run's public, unauthenticated
    # access link — masked in both the list and the change form rather
    # than shown in full (list_display previously rendered it in the raw,
    # even more exposed than the change-page-only fields fixed alongside
    # this one in core/intake/tickets' admin.py).
    list_display = ['run', 'token_masked', 'token_expires_at', 'external_contact', 'created_at']
    readonly_fields = ['token_masked']

    @admin.display(description='Token')
    def token_masked(self, obj):
        return mask_secret(obj.token)
