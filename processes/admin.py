from django.contrib import admin

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
    list_display = ['run', 'token', 'token_expires_at', 'external_contact', 'created_at']
    readonly_fields = ['token']
