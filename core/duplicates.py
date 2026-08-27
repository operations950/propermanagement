import itertools

from django.db import transaction
from django.db.models import Count

from .models import Contact, DuplicateDismissal


def _group_survives(contacts):
    """A group only disappears from the scan once every pair inside it has
    been explicitly dismissed — for the common 2-contact case that's one
    dismissal; a 3+ group (e.g. several people sharing one office line)
    needs each pair addressed, but the review UI's Dismiss button records
    every pair in the group at once, so in practice it's still one click."""
    if len(contacts) < 2:
        return False
    for c1, c2 in itertools.combinations(contacts, 2):
        if not DuplicateDismissal.is_dismissed(c1, c2):
            return True
    return False


def find_duplicate_groups():
    """Groups of Contacts sharing the same phone or the same email,
    excluding groups where every pair has already been dismissed as
    not-actually-duplicates. Each group is independent — the same Contact
    can appear in both a phone-match group and an email-match group if it
    duplicates different people on each axis."""
    groups = []

    phones = (
        Contact.objects.exclude(phone='').values('phone')
        .annotate(n=Count('id')).filter(n__gt=1).values_list('phone', flat=True)
    )
    for phone in phones:
        contacts = list(Contact.objects.filter(phone=phone).order_by('created_at'))
        if _group_survives(contacts):
            groups.append({'key_type': 'Phone', 'key': phone, 'contacts': contacts})

    emails = (
        Contact.objects.exclude(email='').values('email')
        .annotate(n=Count('id')).filter(n__gt=1).values_list('email', flat=True)
    )
    for email in emails:
        contacts = list(Contact.objects.filter(email=email).order_by('created_at'))
        if _group_survives(contacts):
            groups.append({'key_type': 'Email', 'key': email, 'contacts': contacts})

    return groups


def merge_contacts(primary, loser):
    """Reassigns every known reference from loser onto primary, then
    deletes loser. A loser TicketContact row that would collide with an
    (ticket, contact, role) row primary already has is dropped instead of
    raising — primary is already linked to that ticket in that role, so
    nothing is lost."""
    from core.models import ContactDocument, ContactImportCandidate, ContactUpdateCandidate
    from intake.models import Reservation
    from processes.models import ProcessRun
    from tickets.models import (
        FollowUpLog, Ticket, TicketAssignmentLog, TicketAttachment, TicketContact, TicketTemplate,
    )

    with transaction.atomic():
        primary.properties.add(*loser.properties.all())
        primary.units.add(*loser.units.all())

        for tc in TicketContact.objects.filter(contact=loser):
            if TicketContact.objects.filter(ticket=tc.ticket, contact=primary, role=tc.role).exists():
                tc.delete()
            else:
                tc.contact = primary
                tc.save(update_fields=['contact'])

        ContactImportCandidate.objects.filter(resolved_contact=loser).update(resolved_contact=primary)
        ContactUpdateCandidate.objects.filter(contact=loser).update(contact=primary)
        Reservation.objects.filter(guest=loser).update(guest=primary)
        TicketTemplate.objects.filter(contact=loser).update(contact=primary)
        Ticket.objects.filter(assigned_contact=loser).update(assigned_contact=primary)
        TicketAttachment.objects.filter(uploaded_by_contact=loser).update(uploaded_by_contact=primary)
        TicketAssignmentLog.objects.filter(from_contact=loser).update(from_contact=primary)
        TicketAssignmentLog.objects.filter(to_contact=loser).update(to_contact=primary)
        FollowUpLog.objects.filter(contact=loser).update(contact=primary)
        # Both on_delete=CASCADE from Contact — without reassigning these
        # first, loser.delete() below silently destroys any document
        # (W9, insurance cert, ...) or in-progress process run (with all
        # its steps/attachments) attached to the losing side, the same
        # bug class as the units/properties fix above, for two relations
        # added after that fix landed.
        ContactDocument.objects.filter(contact=loser).update(contact=primary)
        ProcessRun.objects.filter(contact=loser).update(contact=primary)

        loser.delete()


def merge_all_into(primary_id, all_ids):
    primary = Contact.objects.get(pk=primary_id)
    for other_id in all_ids:
        other_id = int(other_id)
        if other_id == primary.pk:
            continue
        loser = Contact.objects.filter(pk=other_id).first()
        if loser is not None:
            merge_contacts(primary, loser)
    return primary
