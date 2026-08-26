from django.contrib import admin

from .models import (
    PropertySupplyOverride,
    SupplyItem,
    SupplyOrder,
    SupplyOrderBatch,
    SupplyOrderLine,
    SupplyReading,
    SupplyRequest,
)


@admin.register(SupplyRequest)
class SupplyRequestAdmin(admin.ModelAdmin):
    list_display = ['item_guess', 'property', 'status', 'quantity_guess', 'created_at']
    list_filter = ['status', 'property']
    search_fields = ['raw_text', 'item_guess']


@admin.register(SupplyOrderBatch)
class SupplyOrderBatchAdmin(admin.ModelAdmin):
    list_display = ['property', 'date', 'exported_at']
    list_filter = ['property']


@admin.register(SupplyItem)
class SupplyItemAdmin(admin.ModelAdmin):
    list_display = ['name', 'unit_label', 'walmart_item_id', 'is_standard', 'standard_reorder_quantity', 'is_active']
    list_filter = ['is_standard', 'is_active']
    search_fields = ['name', 'walmart_item_id']


@admin.register(PropertySupplyOverride)
class PropertySupplyOverrideAdmin(admin.ModelAdmin):
    list_display = ['property', 'unit', 'supply_item', 'is_hidden', 'reorder_quantity', 'is_active']
    list_filter = ['property', 'is_hidden', 'is_active']
    search_fields = ['property__name', 'unit__label', 'supply_item__name']


@admin.register(SupplyReading)
class SupplyReadingAdmin(admin.ModelAdmin):
    list_display = ['property', 'unit', 'supply_item', 'level', 'read_at', 'visit']
    list_filter = ['level']
    search_fields = ['property__name', 'unit__label', 'supply_item__name']


class SupplyOrderLineInline(admin.TabularInline):
    model = SupplyOrderLine
    extra = 0


@admin.register(SupplyOrder)
class SupplyOrderAdmin(admin.ModelAdmin):
    list_display = ['property', 'created_at', 'created_by', 'sent_at']
    list_filter = ['property']
    inlines = [SupplyOrderLineInline]
