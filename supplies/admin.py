from django.contrib import admin

from .models import SupplyOrderBatch, SupplyRequest


@admin.register(SupplyRequest)
class SupplyRequestAdmin(admin.ModelAdmin):
    list_display = ['item_guess', 'property', 'status', 'quantity_guess', 'created_at']
    list_filter = ['status', 'property']
    search_fields = ['raw_text', 'item_guess']


@admin.register(SupplyOrderBatch)
class SupplyOrderBatchAdmin(admin.ModelAdmin):
    list_display = ['property', 'date', 'exported_at']
    list_filter = ['property']
