"""Keeps each Visit's Google Calendar event in step with the visit — see
onsite/google_calendar_push.py. Signals rather than explicit calls at each
call site because visits are changed from many places (the visit screen,
booking imports, cancellations, recurring rules, admin) and every one of
them used to have to remember to push; the ones that didn't (reassigning a
cleaner, editing the date by hand) silently left the calendar stale."""
from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from . import google_calendar_push
from django.db.models import Q

from .models import GuestRequest, Visit

# Fields that appear on (or decide whether there is) a calendar event. A
# save that names update_fields and touches none of these — the sync's own
# bookkeeping writes, photo uploads, payment batches — can't change the
# event and is skipped.
CALENDAR_FIELDS = {
    'property', 'property_id', 'unit', 'unit_id', 'visit_type', 'visit_type_id',
    'scheduled_date', 'scheduled_start', 'ready_by',
    'assigned_staff', 'assigned_staff_id', 'assigned_contact', 'assigned_contact_id',
    'status', 'is_deep_clean', 'booking', 'booking_id', 'next_booking', 'next_booking_id', 'access_token',
}


@receiver(post_save, sender=Visit)
def visit_saved(sender, instance, created, update_fields, raw=False, **kwargs):
    if raw:
        return
    if update_fields is not None and not (set(update_fields) & CALENDAR_FIELDS):
        return
    if not google_calendar_push.is_configured():
        return
    pk = instance.pk
    # After commit: it's a network call, and the event should describe what
    # was actually saved (a rolled-back change must never reach the calendar).
    transaction.on_commit(lambda: google_calendar_push.sync_visit(pk))


@receiver(post_delete, sender=Visit)
def visit_deleted(sender, instance, **kwargs):
    event_id = instance.google_event_id
    if event_id:
        transaction.on_commit(lambda: google_calendar_push.delete_orphaned_event(event_id))


@receiver(post_save, sender=GuestRequest)
@receiver(post_delete, sender=GuestRequest)
def guest_request_changed(sender, instance, **kwargs):
    """The agreed checkout/check-in times are in the calendar event, so
    approving, declining or removing a request must refresh the event of
    every visit it affects: the one for the checkout, and the one whose
    "next guest" is this booking."""
    if kwargs.get('raw') or not google_calendar_push.is_configured():
        return
    booking_id = instance.booking_id
    pks = list(Visit.objects.filter(Q(booking_id=booking_id) | Q(next_booking_id=booking_id)).values_list('pk', flat=True))
    for pk in pks:
        transaction.on_commit(lambda pk=pk: google_calendar_push.sync_visit(pk))
