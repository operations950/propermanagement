from django.contrib import admin

from .models import (
    Booking,
    BookingFeedHealth,
    CleaningPaymentBatch,
    CleaningPricingSettings,
    DailyUploadSlot,
    ImportBatch,
    PropertyChecklistItem,
    PropertyChecklistOverride,
    PropertyChecklistReview,
    StandardChecklistItem,
    Visit,
    VisitChecklistItem,
    VisitIssue,
    VisitMedia,
    VisitRule,
    VisitType,
)


class StandardChecklistItemInline(admin.TabularInline):
    model = StandardChecklistItem
    extra = 1
    fields = ['section', 'order', 'text', 'mandatory', 'requires_photo', 'requires_note', 'is_active']


@admin.register(VisitType)
class VisitTypeAdmin(admin.ModelAdmin):
    list_display = ['name', 'is_addon', 'default_duration_minutes', 'requires_deadline', 'is_active']
    list_editable = ['is_addon']
    inlines = [StandardChecklistItemInline]


@admin.register(StandardChecklistItem)
class StandardChecklistItemAdmin(admin.ModelAdmin):
    list_display = ['visit_type', 'section', 'text', 'minutes', 'scales_by', 'mandatory', 'requires_photo', 'is_active']
    list_filter = ['visit_type', 'section', 'mandatory', 'is_active']


@admin.register(PropertyChecklistOverride)
class PropertyChecklistOverrideAdmin(admin.ModelAdmin):
    list_display = ['property', 'standard_item', 'is_hidden']
    list_filter = ['visit_type', 'is_hidden']


@admin.register(PropertyChecklistItem)
class PropertyChecklistItemAdmin(admin.ModelAdmin):
    list_display = ['property', 'visit_type', 'text', 'minutes', 'scales_by', 'mandatory', 'is_active']
    list_filter = ['visit_type', 'is_active']


@admin.register(PropertyChecklistReview)
class PropertyChecklistReviewAdmin(admin.ModelAdmin):
    list_display = ['property', 'visit_type', 'reviewed_at']
    list_filter = ['visit_type']


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ['property', 'source', 'check_in', 'check_out', 'status']
    list_filter = ['source', 'status']
    search_fields = ['property__name', 'guest_name', 'external_uid']


@admin.register(ImportBatch)
class ImportBatchAdmin(admin.ModelAdmin):
    list_display = [
        'property', 'source', 'covers_start', 'covers_end',
        'new_count', 'changed_count', 'reactivated_count', 'cancelled_count', 'applied_at',
    ]
    list_filter = ['source']


@admin.register(DailyUploadSlot)
class DailyUploadSlotAdmin(admin.ModelAdmin):
    list_display = ['label', 'source', 'order', 'is_active', 'last_uploaded_at']
    list_filter = ['source', 'is_active']
    ordering = ['order', 'label']


@admin.register(BookingFeedHealth)
class BookingFeedHealthAdmin(admin.ModelAdmin):
    list_display = ['source', 'last_upload_at', 'newest_booked_date', 'coverage_through']


class VisitChecklistItemInline(admin.TabularInline):
    model = VisitChecklistItem
    extra = 0


class VisitMediaInline(admin.TabularInline):
    model = VisitMedia
    extra = 0


@admin.register(Visit)
class VisitAdmin(admin.ModelAdmin):
    list_display = ['property', 'visit_type', 'scheduled_date', 'status', 'assignee_label', 'ready_by']
    list_filter = ['visit_type', 'status']
    search_fields = ['property__name']
    inlines = [VisitChecklistItemInline, VisitMediaInline]


@admin.register(CleaningPricingSettings)
class CleaningPricingSettingsAdmin(admin.ModelAdmin):
    list_display = ['hourly_rate', 'updated_at']

    def has_add_permission(self, request):
        # Singleton — get() creates the one row lazily; no reason to ever
        # add a second one from here.
        return not CleaningPricingSettings.objects.exists()


@admin.register(CleaningPaymentBatch)
class CleaningPaymentBatchAdmin(admin.ModelAdmin):
    list_display = ['paid_at', 'paid_by', 'total_amount', 'note']
    readonly_fields = ['paid_at']


@admin.register(VisitIssue)
class VisitIssueAdmin(admin.ModelAdmin):
    list_display = ['visit', 'description', 'created_ticket', 'created_at']


@admin.register(VisitRule)
class VisitRuleAdmin(admin.ModelAdmin):
    list_display = ['property', 'unit', 'visit_type', 'interval_months', 'default_assignee', 'is_active']
    list_filter = ['visit_type', 'is_active']
