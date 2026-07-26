from django.contrib import admin

from .models import (
    ProcessAttachment,
    ProcessInstance,
    ProcessInstanceDocument,
    ProcessInstanceItem,
    ProcessTemplate,
    ProcessTemplateAttachment,
    ProcessTemplateItem,
)


class ProcessTemplateItemInline(admin.TabularInline):
    model = ProcessTemplateItem
    extra = 0


class ProcessTemplateAttachmentInline(admin.TabularInline):
    model = ProcessTemplateAttachment
    extra = 0


@admin.register(ProcessTemplate)
class ProcessTemplateAdmin(admin.ModelAdmin):
    list_display = ['name', 'is_active', 'created_at']
    list_filter = ['is_active']
    search_fields = ['name', 'description']
    inlines = [ProcessTemplateItemInline, ProcessTemplateAttachmentInline]


class ProcessInstanceItemInline(admin.TabularInline):
    model = ProcessInstanceItem
    extra = 0
    readonly_fields = ['checked_at', 'checked_by']


@admin.register(ProcessInstance)
class ProcessInstanceAdmin(admin.ModelAdmin):
    list_display = ['process_template', 'ticket', 'created_at', 'completed_at']
    list_filter = ['process_template']
    inlines = [ProcessInstanceItemInline]


@admin.register(ProcessAttachment)
class ProcessAttachmentAdmin(admin.ModelAdmin):
    list_display = ['instance_item', 'caption', 'created_at']


@admin.register(ProcessInstanceDocument)
class ProcessInstanceDocumentAdmin(admin.ModelAdmin):
    list_display = ['instance_item', 'generated_at', 'generated_by']
    readonly_fields = ['generated_at']
