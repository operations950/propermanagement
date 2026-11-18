"""The contact interaction ticker: a person's `last_interaction_at` and `interaction_count`, so the Contacts screen can put the people you actually deal with first.

`touch(contact)` is ONE cheap UPDATE (no read first, no extra query on a page load) and it never raises: a ticker failure must never break the real action (a ticket saved, a message sent).

Where it fires (all wired once, in `connect()`, from CoreConfig.ready, so no view or service has to remember to call it):
  * a ticket is given to a contact, or the contact on it changes (Ticket assigned_contact, on create or change)
  * a person is attached to a ticket (TicketContact created)
  * a follow-up email or text went out to a contact (FollowUpLog created, successful, with its contact)
  * a Quo text came in from, or went out to, a contact (QuoMessage created: the number is matched to a contact by phone)
  * an On-Site visit is given to a contact, or its contact changes (Visit assigned_contact, on create or change)
  * a vendor bill is entered for a contact (Bill created, not void), or a payment is made to one (Payment created, not void)
  * a legal document or letter is emailed to an address that belongs to a contact (legalforms.services.email_document calls touch_by_email)
Contacts that predate the ticker were given a head start by migration 0088 (see its note for the sources).
"""
import logging
import re

from django.db.models import F
from django.db.models.signals import post_init, post_save
from django.utils import timezone

logger = logging.getLogger(__name__)


def touch(contact, when=None):
    """Note one interaction with `contact` (a Contact or its id). One UPDATE; returns True if it was written; never raises."""
    try:
        pk = getattr(contact, 'pk', contact)
        if not pk:
            return False
        from .models import Contact
        Contact.objects.filter(pk=pk).update(last_interaction_at=when or timezone.now(), interaction_count=F('interaction_count') + 1)
        return True
    except Exception:
        logger.exception('Could not note a contact interaction')
        return False


def touch_by_phone(number):
    """Note an interaction with whoever has this phone number (a Quo text): the number is matched in the few ways we store phones. A number nobody has is ignored."""
    try:
        digits = re.sub(r'\D', '', number or '')[-10:]
        if len(digits) < 10:
            return 0
        a, b, c = digits[:3], digits[3:6], digits[6:]
        from .models import Contact
        ids = list(Contact.objects.filter(phone__in=[f'{a}-{b}-{c}', f'({a}) {b}-{c}', f'{a}.{b}.{c}', f'{a} {b} {c}', digits, f'+1{digits}', f'1{digits}', f'+1 {a}-{b}-{c}']).values_list('pk', flat=True))
        return sum(1 for pk in ids if touch(pk))
    except Exception:
        logger.exception('Could not note a contact interaction by phone')
        return 0


def touch_by_email(address):
    """Note an interaction with whoever has this email address (a document emailed to them). An address nobody has is ignored."""
    try:
        address = (address or '').strip()
        if not address:
            return 0
        from .models import Contact
        ids = list(Contact.objects.filter(email__iexact=address).values_list('pk', flat=True))
        return sum(1 for pk in ids if touch(pk))
    except Exception:
        logger.exception('Could not note a contact interaction by email')
        return 0


# ------------------------------------------------------------------ the wiring

def _remember(sender, instance, **kwargs):
    """Remember which contact a ticket / visit had when it was loaded, so a save can tell a change from a plain re-save. Reads the raw attribute so a deferred field is never fetched."""
    instance._ca_contact_id = instance.__dict__.get('assigned_contact_id')


def _assigned(sender, instance, created=False, update_fields=None, **kwargs):
    contact_id = instance.__dict__.get('assigned_contact_id')
    if update_fields is not None and 'assigned_contact' not in update_fields and 'assigned_contact_id' not in update_fields:
        return
    if contact_id and (created or contact_id != getattr(instance, '_ca_contact_id', None)):
        touch(contact_id)
    instance._ca_contact_id = contact_id


def _ticket_contact(sender, instance, created=False, **kwargs):
    if created and instance.contact_id:
        touch(instance.contact_id)


def _follow_up(sender, instance, created=False, **kwargs):
    if created and instance.contact_id and instance.success:
        touch(instance.contact_id)


def _quo_message(sender, instance, created=False, **kwargs):
    if not created:
        return
    if instance.direction == 'in':
        touch_by_phone(instance.from_number)
    else:
        for number in (instance.to_number or '').split(','):
            touch_by_phone(number)


def _vendor(sender, instance, created=False, **kwargs):
    if created and instance.vendor_id and getattr(instance, 'status', '') != 'void':
        touch(instance.vendor_id)


def connect():
    """Wire the signals (called once from CoreConfig.ready). Lazy 'app.Model' senders, so nothing is imported here."""
    for model in ('tickets.Ticket', 'onsite.Visit'):
        post_init.connect(_remember, sender=model, dispatch_uid=f'contact_activity_remember_{model}')
        post_save.connect(_assigned, sender=model, dispatch_uid=f'contact_activity_assigned_{model}')
    post_save.connect(_ticket_contact, sender='tickets.TicketContact', dispatch_uid='contact_activity_ticket_contact')
    post_save.connect(_follow_up, sender='tickets.FollowUpLog', dispatch_uid='contact_activity_follow_up')
    post_save.connect(_quo_message, sender='intake.QuoMessage', dispatch_uid='contact_activity_quo_message')
    post_save.connect(_vendor, sender='accounting.Bill', dispatch_uid='contact_activity_bill')
    post_save.connect(_vendor, sender='accounting.Payment', dispatch_uid='contact_activity_payment')
