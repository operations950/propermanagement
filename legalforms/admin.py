from django.contrib import admin

from .models import DocumentTemplate, GeneratedDocument


@admin.register(DocumentTemplate)
class DocumentTemplateAdmin(admin.ModelAdmin):
    list_display = ('name', 'category', 'statute', 'version', 'is_active')
    list_filter = ('category', 'is_active')
    prepopulated_fields = {}
    readonly_fields = ('version',)


@admin.register(GeneratedDocument)
class GeneratedDocumentAdmin(admin.ModelAdmin):
    list_display = ('template', 'subject', 'property', 'status', 'created_at')
    list_filter = ('status', 'template')
    readonly_fields = [f.name for f in GeneratedDocument._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
