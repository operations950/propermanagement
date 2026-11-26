"""Attach an owner contact (and their email) to commercial buildings so the owner portal can find them.

    python manage.py attach_commercial_owner --email owner@example.com --name "Josh Abrams"                 DRY RUN: says what it would do, writes nothing
    python manage.py attach_commercial_owner --email owner@example.com --name "Josh Abrams" --apply         does it
    ... --properties "324 Lofts" "Del Park LLC" "Red Barn 381"                                               (those three are the default)

The owner contact is the one of type Owner with that name if there is one (its email is filled in, or replaced only with --replace-email), otherwise a new one is made. It is linked to each
building; nothing is switched on here (the portal for a building is switched on from Admin Tools > Owner portal) and nobody is emailed."""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import Contact, Property

DEFAULT_PROPERTIES = ['324 Lofts', 'Del Park LLC', 'Red Barn 381']


class Command(BaseCommand):
    help = 'Attach an owner contact (with an email) to commercial buildings for the owner portal. Dry run unless --apply.'

    def add_arguments(self, parser):
        parser.add_argument('--email', required=True)
        parser.add_argument('--name', required=True, help='The owner contact: an existing Owner contact with this name is used, else one is created.')
        parser.add_argument('--properties', nargs='+', default=DEFAULT_PROPERTIES)
        parser.add_argument('--replace-email', action='store_true', help='Replace an email the contact already has.')
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        email, name = options['email'].strip().lower(), options['name'].strip()
        if '@' not in email:
            raise CommandError('That is not an email address.')
        props = []
        for wanted in options['properties']:
            found = list(Property.objects.filter(name__iexact=wanted, property_type=Property.Type.COMMERCIAL))
            if len(found) != 1:
                raise CommandError(f'Expected one commercial building called "{wanted}", found {len(found)}.')
            props.append(found[0])
        contacts = list(Contact.objects.filter(name__iexact=name, contact_type=Contact.ContactType.OWNER))
        if len(contacts) > 1:
            raise CommandError(f'More than one owner contact is called "{name}": {[c.pk for c in contacts]}. Merge them first.')
        contact = contacts[0] if contacts else None
        self.stdout.write(f'{"Using" if contact else "Would create"} owner contact: {name}' + (f' (id {contact.pk}, email now "{contact.email}")' if contact else ''))
        if contact and contact.email and contact.email.lower() != email and not options['replace_email']:
            raise CommandError(f'{name} already has the email {contact.email}. Use --replace-email to change it to {email}.')
        for p in props:
            self.stdout.write(f'  link to {p.name}' + (' (already linked)' if contact and contact.properties.filter(pk=p.pk).exists() else ''))
        if not options['apply']:
            self.stdout.write('DRY RUN: nothing was written. Add --apply to do it.')
            return
        with transaction.atomic():
            if contact is None:
                contact = Contact.objects.create(name=name, contact_type=Contact.ContactType.OWNER, email=email)
            elif contact.email.lower() != email:
                contact.email = email
                contact.save(update_fields=['email'])
            for p in props:
                contact.properties.add(p)
        self.stdout.write(self.style.SUCCESS(f'{name} <{email}> is now an owner contact of {", ".join(p.name for p in props)}.'))
