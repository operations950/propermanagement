"""Idempotent: for every ticket ever completed with a Vendor/Contractor
assigned, ensures that contact is linked to the ticket's property via
Contact.properties — see tickets.views._link_vendor_to_property, which
does the same thing going forward the moment a ticket reaches Completed.
Runs on every deploy (see Procfile) so the invariant self-heals for any
completed ticket that predates that hook (i.e. everything up to now)."""
from django.core.management.base import BaseCommand

from core.models import Contact
from tickets.models import Ticket


class Command(BaseCommand):
    help = 'Links every vendor/contractor to every property where they have a completed ticket.'

    def handle(self, *args, **options):
        completed = (
            Ticket.objects.filter(
                status=Ticket.Status.COMPLETED, property__isnull=False, assigned_contact__isnull=False,
                assigned_contact__contact_type=Contact.ContactType.VENDOR,
            )
            .select_related('assigned_contact')
            .only('property_id', 'assigned_contact_id')
        )
        linked = 0
        seen = set()
        for ticket in completed:
            key = (ticket.assigned_contact_id, ticket.property_id)
            if key in seen:
                continue
            seen.add(key)
            if not ticket.assigned_contact.properties.filter(pk=ticket.property_id).exists():
                ticket.assigned_contact.properties.add(ticket.property_id)
                linked += 1
        self.stdout.write(self.style.SUCCESS(f'Linked {linked} new vendor-property association(s).'))
