from django.contrib import admin

from .models import Session, SessionLine, SessionTemplate, SessionTemplateLine


class SessionTemplateLineInline(admin.TabularInline):
    model = SessionTemplateLine
    extra = 1
    fields = ['label', 'display_order']


@admin.register(SessionTemplate)
class SessionTemplateAdmin(admin.ModelAdmin):
    list_display = ['name', 'department', 'owner', 'frequency', 'line_source', 'next_open_date', 'is_active']
    list_filter = ['department', 'frequency', 'line_source', 'is_active']
    search_fields = ['name']
    inlines = [SessionTemplateLineInline]


class SessionLineInline(admin.TabularInline):
    model = SessionLine
    extra = 0
    fields = ['label', 'property', 'state', 'skip_reason', 'promoted_ticket']
    readonly_fields = ['promoted_ticket']


@admin.register(Session)
class SessionAdmin(admin.ModelAdmin):
    list_display = ['template', 'period_label', 'owner', 'status', 'opens_at', 'due_at', 'submitted_at']
    list_filter = ['status', 'template']
    search_fields = ['template__name', 'period_label']
    inlines = [SessionLineInline]
