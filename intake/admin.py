from django.contrib import admin

from core.admin_utils import mask_secret

from .models import GmailInboxToken, GmailThreadState, PollCursor, QuoMessage, QuoThreadState, Reservation


@admin.register(Reservation)
class ReservationAdmin(admin.ModelAdmin):
    list_display = ['external_reservation_id', 'source', 'property', 'guest', 'check_in', 'check_out', 'status']
    list_filter = ['source', 'status']
    search_fields = ['external_reservation_id']


@admin.register(PollCursor)
class PollCursorAdmin(admin.ModelAdmin):
    list_display = ['key', 'value', 'updated_at']


@admin.register(QuoThreadState)
class QuoThreadStateAdmin(admin.ModelAdmin):
    list_display = ['conversation_id', 'participant', 'last_message_id', 'last_classified_at', 'updated_at']
    search_fields = ['conversation_id', 'participant']


@admin.register(QuoMessage)
class QuoMessageAdmin(admin.ModelAdmin):
    list_display = ['conversation_id', 'direction', 'from_number', 'to_number', 'body', 'quo_created_at']
    list_filter = ['direction']
    search_fields = ['conversation_id', 'from_number', 'to_number', 'body']
    ordering = ['-quo_created_at']


@admin.register(GmailInboxToken)
class GmailInboxTokenAdmin(admin.ModelAdmin):
    list_display = ['mailbox_email', 'is_send_from', 'connected_at', 'updated_at']
    # See core.admin.GoogleCalendarTokenAdmin's matching comment — exclude
    # is required alongside readonly_fields, not just readonly_fields
    # alone, or the real token fields still render as plain editable text.
    exclude = ['refresh_token', 'access_token']
    readonly_fields = [
        'refresh_token_masked', 'access_token_masked', 'access_token_expires_at', 'connected_at', 'updated_at',
    ]

    @admin.display(description='Refresh token')
    def refresh_token_masked(self, obj):
        return mask_secret(obj.refresh_token)

    @admin.display(description='Access token')
    def access_token_masked(self, obj):
        return mask_secret(obj.access_token)


@admin.register(GmailThreadState)
class GmailThreadStateAdmin(admin.ModelAdmin):
    list_display = ['mailbox_email', 'thread_id', 'last_message_id', 'last_classified_at', 'updated_at']
    search_fields = ['mailbox_email', 'thread_id']
