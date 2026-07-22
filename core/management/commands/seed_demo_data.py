"""Populates a small, coherent demo dataset: a few properties, contacts
(tenant/vendor/guest), a staff login, a few recurring TicketTemplates, and a
couple of example tickets (one staff-assigned, one vendor-assigned) so the
whole staff UI has something to look at immediately.

Uses get_or_create throughout so it's safe to run more than once — it won't
duplicate rows on a second run, unlike a destructive reset.
"""
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Contact, Property, StaffProfile
from tickets.models import Frequency, Priority, Ticket, TicketContact, TicketTemplate


class Command(BaseCommand):
    help = 'Seed a small demo dataset (properties, contacts, staff logins, recurring templates, example tickets).'

    def handle(self, *args, **options):
        properties = self._seed_properties()
        contacts = self._seed_contacts(properties)
        staff = self._seed_staff()
        self._seed_templates(properties, staff)
        self._seed_example_tickets(properties, contacts, staff)
        self.stdout.write(self.style.SUCCESS('Demo data seeded.'))

    def _seed_properties(self):
        names = ['Sunset Villa', 'Lakeside Cabin', 'Downtown Loft']
        properties = {}
        for name in names:
            prop, _ = Property.objects.get_or_create(name=name, defaults={'address': f'{name} address TBD'})
            properties[name] = prop
        self.stdout.write(f'  {len(properties)} properties')
        return properties

    def _seed_contacts(self, properties):
        contacts = {}
        contacts['tenant'], _ = Contact.objects.get_or_create(
            email='tenant.demo@example.com',
            defaults={
                'name': 'Taylor Tenant', 'contact_type': Contact.ContactType.TENANT,
                'phone': '555-0110', 'property': properties['Downtown Loft'],
            },
        )
        contacts['vendor'], _ = Contact.objects.get_or_create(
            email='handyman.demo@example.com',
            defaults={
                'name': "Bob's Handyman Service", 'contact_type': Contact.ContactType.VENDOR,
                'trade': 'general handyman', 'phone': '555-0120',
            },
        )
        contacts['guest'], _ = Contact.objects.get_or_create(
            email='guest.demo@example.com',
            defaults={
                'name': 'Gabby Guest', 'contact_type': Contact.ContactType.GUEST,
                'phone': '555-0130', 'property': properties['Sunset Villa'],
            },
        )
        self.stdout.write(f'  {len(contacts)} contacts')
        return contacts

    def _seed_staff(self):
        user, created = User.objects.get_or_create(
            username='staff', defaults={'email': 'staff@example.com', 'is_staff': True},
        )
        if created:
            user.set_password('staff12345')
            user.save()
        staff_profile, _ = StaffProfile.objects.get_or_create(
            user=user, defaults={'role': StaffProfile.Role.PROPERTY_MANAGER, 'phone': '555-0100'},
        )

        admin_user, admin_created = User.objects.get_or_create(
            username='admin', defaults={'email': 'admin@example.com', 'is_staff': True, 'is_superuser': True},
        )
        if admin_created:
            admin_user.set_password('admin12345')
            admin_user.save()
        StaffProfile.objects.get_or_create(user=admin_user, defaults={'role': StaffProfile.Role.ADMIN})

        self.stdout.write('  staff logins: staff/staff12345, admin/admin12345')
        return staff_profile

    def _seed_templates(self, properties, staff):
        today = timezone.localdate()
        templates = [
            dict(
                title='Walk property grounds check', frequency=Frequency.DAILY, property=properties['Sunset Villa'],
                role=StaffProfile.Role.MAINTENANCE,
            ),
            dict(
                title='Pool chemical check', frequency=Frequency.WEEKLY, property=properties['Lakeside Cabin'],
                role=StaffProfile.Role.MAINTENANCE,
            ),
            dict(
                title='Deep clean common areas', frequency=Frequency.QUARTERLY, property=properties['Downtown Loft'],
                role=StaffProfile.Role.CLEANER,
            ),
            dict(
                title='Fire extinguisher inspection', frequency=Frequency.YEARLY, property=None,
                role=StaffProfile.Role.MAINTENANCE,
            ),
        ]
        count = 0
        for spec in templates:
            _, created = TicketTemplate.objects.get_or_create(
                title=spec['title'],
                defaults={
                    'frequency': spec['frequency'],
                    'property': spec['property'],
                    'next_run_date': today,
                    'default_assigned_role': spec['role'],
                    'default_assigned_staff': staff,
                    'default_priority': Priority.MEDIUM,
                },
            )
            if created:
                count += 1
        self.stdout.write(f'  {count} new recurring templates (run `generate_recurring_tickets` to materialize them)')

    def _seed_example_tickets(self, properties, contacts, staff):
        ticket1, created1 = Ticket.objects.get_or_create(
            title='Replace broken porch light', property=properties['Sunset Villa'], source=Ticket.Source.MANUAL,
            defaults={
                'description': 'Guest mentioned the porch light by the front door is out.',
                'priority': Priority.MEDIUM, 'status': Ticket.Status.ASSIGNED,
                'assigned_role': StaffProfile.Role.MAINTENANCE, 'assigned_staff': staff,
            },
        )
        if created1:
            TicketContact.objects.get_or_create(
                ticket=ticket1, contact=contacts['guest'], role=TicketContact.Role.REPORTER,
            )

        Ticket.objects.get_or_create(
            title='Fix leaking bathroom sink', property=properties['Downtown Loft'], source=Ticket.Source.MANUAL,
            defaults={
                'description': 'Tenant reported a slow leak under the bathroom sink.',
                'priority': Priority.HIGH, 'status': Ticket.Status.ASSIGNED,
                'assigned_role': StaffProfile.Role.CONTRACTOR, 'assigned_contact': contacts['vendor'],
            },
        )
        self.stdout.write('  2 example tickets (one staff-assigned, one vendor-assigned)')
