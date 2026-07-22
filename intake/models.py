from django.db import models

from core.models import Contact, Property


class Reservation(models.Model):
    class Source(models.TextChoices):
        AIRBNB = 'airbnb', 'Airbnb'
        VRBO = 'vrbo', 'VRBO'
        FAKE = 'fake', 'Simulated (dev)'

    class Status(models.TextChoices):
        BOOKED = 'booked', 'Booked'
        CANCELLED = 'cancelled', 'Cancelled'

    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='reservations')
    source = models.CharField(max_length=20, choices=Source.choices)
    external_reservation_id = models.CharField(
        max_length=200, help_text="The platform's stable confirmation code — the natural key.",
    )
    guest = models.ForeignKey(Contact, on_delete=models.SET_NULL, null=True, blank=True, related_name='reservations')
    check_in = models.DateField(null=True, blank=True)
    check_out = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.BOOKED)
    raw_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('source', 'external_reservation_id')]

    def __str__(self):
        return f'{self.source} #{self.external_reservation_id} — {self.property}'


class PollCursor(models.Model):
    """Generic 'where did I leave off' marker for a pull-based adapter —
    e.g. Quo's conversations list is filtered by `updatedAfter` so each
    poll only asks for what's changed since the last one."""

    key = models.CharField(max_length=100, unique=True)
    value = models.CharField(max_length=200, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.key} = {self.value}'


class QuoThreadState(models.Model):
    """Tracks the last message we've seen per Quo conversation, so we only
    re-fetch/re-classify a thread when it actually has new activity — full-
    thread classification is an LLM call and shouldn't re-run on unchanged
    threads every poll."""

    conversation_id = models.CharField(max_length=100, unique=True)
    phone_number_id = models.CharField(max_length=100, blank=True)
    participant = models.CharField(max_length=30, blank=True)
    last_message_id = models.CharField(max_length=100, blank=True)
    last_classified_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'Quo thread {self.conversation_id}'


class GmailInboxToken(models.Model):
    """OAuth credentials for the ONE shared mailbox this adapter reads (e.g.
    admin@proper-realty.com) — connected once via intake/views.py's
    gmail_connect flow (admin-only, since it grants read access to the
    whole inbox). Deliberately separate from core.GoogleCalendarToken,
    which is many individual staff calendars, not one shared inbox."""

    mailbox_email = models.EmailField(unique=True)
    refresh_token = models.TextField()
    access_token = models.TextField(blank=True)
    access_token_expires_at = models.DateTimeField(null=True, blank=True)
    connected_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.mailbox_email


class GmailThreadState(models.Model):
    """Same purpose as QuoThreadState, one row per Gmail thread instead of
    per Quo conversation."""

    thread_id = models.CharField(max_length=100, unique=True)
    last_message_id = models.CharField(max_length=100, blank=True)
    last_classified_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'Gmail thread {self.thread_id}'
