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


class QuoWebhookLog(models.Model):
    """Raw capture of every inbound Quo webhook POST — kept around as a
    plain audit trail / debugging aid (see /webhooks/quo/log/), separate
    from QuoMessage which is the actual structured store the app reads
    from."""

    received_at = models.DateTimeField(auto_now_add=True)
    raw_body = models.TextField(blank=True)
    parsed = models.JSONField(null=True, blank=True)
    headers = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-received_at']

    def __str__(self):
        return f'Quo webhook @ {self.received_at}'


class QuoMessage(models.Model):
    """One row per Quo SMS message, populated live by the message.received/
    message.delivered webhook (see intake/views.py::quo_webhook) — the
    local mirror of a Quo conversation's content, so a ticket's Contractor
    Communication thread (tickets/views.py::_contractor_thread) can read
    straight from our own DB instead of hitting Quo's API on every page
    load once a ticket is bound to a specific conversation_id
    (Ticket.source_reference — see messaging/services.py::send_via_quo)."""

    class Direction(models.TextChoices):
        IN = 'in', 'Incoming'
        OUT = 'out', 'Outgoing'

    conversation_id = models.CharField(max_length=100, db_index=True, blank=True)
    message_id = models.CharField(max_length=100, unique=True)
    phone_number_id = models.CharField(max_length=100, blank=True)
    direction = models.CharField(max_length=3, choices=Direction.choices)
    from_number = models.CharField(max_length=30, blank=True)
    to_number = models.CharField(max_length=30, blank=True)
    body = models.TextField(blank=True)
    quo_created_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['quo_created_at']

    def __str__(self):
        return f'{self.direction} {self.message_id} in {self.conversation_id}'
