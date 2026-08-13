from django.contrib import admin

from .models import BizRecurringRule, BizTask, Business, BusinessCategory


@admin.register(Business)
class BusinessAdmin(admin.ModelAdmin):
    list_display = ('name', 'owner', 'is_active', 'custom_field_label', 'created_at')
    prepopulated_fields = {'slug': ('name',)}
    filter_horizontal = ('additional_staff',)


@admin.register(BusinessCategory)
class BusinessCategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'business', 'display_order')
    list_filter = ('business',)


@admin.register(BizRecurringRule)
class BizRecurringRuleAdmin(admin.ModelAdmin):
    list_display = ('title', 'business', 'category', 'frequency', 'next_due_date', 'is_active')
    list_filter = ('business', 'frequency', 'is_active')


@admin.register(BizTask)
class BizTaskAdmin(admin.ModelAdmin):
    list_display = ('title', 'business', 'category', 'status', 'priority', 'due_date', 'amount')
    list_filter = ('business', 'status', 'priority')
    search_fields = ('title', 'notes')
