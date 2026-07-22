from django.db import models

from core.models import Property
from tickets.models import Ticket


class SupplyRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        ORDERED = 'ordered', 'Ordered'
        CANCELLED = 'cancelled', 'Cancelled'

    property = models.ForeignKey(
        Property, on_delete=models.CASCADE, related_name='supply_requests', null=True, blank=True,
        help_text='Blank when the source (e.g. a shared Quo phone line) can\'t determine which property.',
    )
    raw_text = models.TextField(help_text='The original message text this request was parsed from.')
    source_reference = models.CharField(
        max_length=200, blank=True,
        help_text='External event id (e.g. email message id) this was parsed from, for idempotent re-polling.',
    )
    item_guess = models.CharField(max_length=200, blank=True)
    quantity_guess = models.CharField(max_length=50, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    source_ticket = models.ForeignKey(
        Ticket, on_delete=models.SET_NULL, null=True, blank=True, related_name='supply_requests',
    )
    order_batch = models.ForeignKey(
        'SupplyOrderBatch', on_delete=models.SET_NULL, null=True, blank=True, related_name='requests',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = [('property', 'source_reference', 'item_guess')]

    def __str__(self):
        return f'{self.item_guess or self.raw_text[:40]} ({self.property})'


class SupplyOrderBatch(models.Model):
    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='supply_order_batches')
    date = models.DateField()
    notes = models.TextField(blank=True)
    exported_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date']
        unique_together = [('property', 'date')]
        verbose_name_plural = 'supply order batches'

    def __str__(self):
        return f'{self.property} order list — {self.date}'
