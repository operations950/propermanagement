from django.contrib import admin

from .models import GmailInboxToken, GmailThreadState, PollCursor, QuoThreadState, Reservation


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


@admin.register(GmailInboxToken)
class GmailInboxTokenAdmin(admin.ModelAdmin):
    list_display = ['mailbox_email', 'connected_at', 'updated_at']
    readonly_fields = ['refresh_token', 'access_token', 'access_token_expires_at', 'connected_at', 'updated_at']


@admin.register(GmailThreadState)
class GmailThreadStateAdmin(admin.ModelAdmin):
    list_display = ['thread_id', 'last_message_id', 'last_classified_at', 'updated_at']
    search_fields = ['thread_id']
