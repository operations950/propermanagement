"""Idempotent: ensures every StaffProfile has a linked Contact with
contact_type=Staff, matched by email — so staff show up when searching
Contacts anywhere in the app (Related Contacts, ticket pickers, the
Contacts list itself). Covers two gaps: a StaffProfile created directly in
Django admin (bypassing core.views.staff_create, which handles this itself
at creation time) and any StaffProfile created before this backfill/the
staff-creation flow's Contact-merge behavior existed. Runs on every deploy
(see Procfile) so the invariant self-heals regardless of how a StaffProfile
came to exist.
"""
from django.core.management.base import BaseCommand

from core.models import Contact, StaffProfile


class Command(BaseCommand):
    help = 'Ensures every StaffProfile has a matching Contact with contact_type=Staff.'

    def handle(self, *args, **options):
        created = 0
        converted = 0
        for profile in StaffProfile.objects.select_related('user'):
            user = profile.user
            if not user.email:
                continue
            contact = Contact.objects.filter(email__iexact=user.email).first()
            if contact is None:
                Contact.objects.create(
                    name=user.get_full_name() or user.username,
                    contact_type=Contact.ContactType.STAFF_ADJACENT,
                    phone=profile.phone,
                    email=user.email,
                )
                created += 1
            elif contact.contact_type != Contact.ContactType.STAFF_ADJACENT:
                contact.contact_type = Contact.ContactType.STAFF_ADJACENT
                contact.secondary_types = []
                contact.save(update_fields=['contact_type', 'secondary_types'])
                converted += 1
        self.stdout.write(self.style.SUCCESS(f'Staff contacts: {created} created, {converted} converted.'))
