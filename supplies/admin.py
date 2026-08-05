from django.contrib import admin

from .models import (
    SupplyItem,
    SupplyOrder,
    SupplyOrderBatch,
    SupplyOrderLine,
    SupplyReading,
    SupplyRequest,
    PropertySupply,
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
    list_display = ['name', 'unit_label', 'walmart_item_id', 'is_active']
    list_filter = ['is_active']
    search_fields = ['name', 'walmart_item_id']


@admin.register(PropertySupply)
class PropertySupplyAdmin(admin.ModelAdmin):
    list_display = ['property', 'supply_item', 'reorder_quantity', 'display_order', 'is_active']
    list_filter = ['property', 'is_active']
    search_fields = ['property__name', 'supply_item__name']


@admin.register(SupplyReading)
class SupplyReadingAdmin(admin.ModelAdmin):
    list_display = ['property_supply', 'level', 'read_at', 'visit']
    list_filter = ['level']
    search_fields = ['property_supply__property__name', 'property_supply__supply_item__name']


class SupplyOrderLineInline(admin.TabularInline):
    model = SupplyOrderLine
    extra = 0


@admin.register(SupplyOrder)
class SupplyOrderAdmin(admin.ModelAdmin):
    list_display = ['property', 'created_at', 'created_by', 'sent_at']
    list_filter = ['property']
    inlines = [SupplyOrderLineInline]
