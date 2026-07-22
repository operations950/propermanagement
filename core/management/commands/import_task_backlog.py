"""One-time import of the real current task backlog (pasted by the user
2026-07-20) into Ticket rows. Idempotent — safe to re-run, keyed by a stable
per-row source_reference so it won't duplicate.

Creates a StaffProfile+User for each real person named in the "Assigned to"
column if one doesn't already exist, with a random generated password
(printed once at import time — these are real staff, not demo accounts, so
there's no fixed password like the seed_demo_data command uses).
"""
import secrets
from datetime import datetime

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Property, StaffProfile
from tickets.models import Priority, Ticket

STAFF = {
    'ana': 'Ana',
    'alexis': 'Alexis',
    'gia': 'Gia',
    'justin': 'Justin',
}

YEAR = 2026

# (property_name_or_blank, task, (month, day), assigned_to_raw, follow_up, notes)
ROWS = [
    ('', 'Track vendor contract renewal dates process', (6, 30), 'Ana/Alexis', '', ''),
    ('LAP', 'Citizens update for board - opening another acct', (7, 3), 'Gia', '', ''),
    ('SA GLEN', 'Reserve transfer - $25k', (7, 6), 'Gia', 'Form through',
     "Use John Lee's login, confirm with Randy first; CALL TRUIST to confirm how to setup wire "
     "6/9 - called and email Truist. Waiting on response."),
    ('LAP SAG', 'Year end Audit', (6, 30), 'Gia', '', ''),
    ('LAP', 'Install two door openers', (7, 1), 'Gia', '', 'Followed up with Chris'),
    ('324', 'Elevator renewals', (7, 6), 'Gia', '', 'Yearly elevator inspection and renewal with state'),
    ('SAG', 'Sidewalk project', (7, 6), 'Gia', '', ''),
    ('LAP', 'Value Issue', (7, 7), 'Gia', '', ''),
    ('Abrams', 'CAM Recon', (6, 26), 'Gia', '', ''),
    ('LAP', 'Opening another acct than Citizens', (6, 26), 'Justin', 'Board members',
     "In final stages with City National - Dan needs to talk to Margarita (banker) directly to finalize."),
    ('SAGRAND', 'Transfer half of reserve funds to City National', (6, 26), 'Justin', '',
     "Need to verify City National Account on Citizens Website (2 small deposits made) 7.11.26"),
    ('', 'Tree trimming quotes for Michelle', (7, 7), 'Alexis', '',
     "Ft Lauderdale Tree Service, Service Queen Tree Service (700), Sky High Tree Service ($900)"),
    ('', 'Michael Caruso - website', (7, 8), 'Alexis/Ana', '', 'Send him new wording'),
    ('LAP', 'Reserve cleanup', (7, 10), 'Justin', '', 'Need to send out a plan and set up a meeting'),
    ('SAG', 'Revised Budget Meeting - Schedule', (7, 6), 'Gia', '', ''),
    ('Neptune', 'Heater is broken', (7, 6), 'Alexis', 'Barefoot Pools?', 'Aqua Pure Pool Services'),
    ('GLEN', 'Call Bee Removal Company', (7, 7), 'Alexis', 'Thomas Benso', 'Today/Thursday (call back)'),
    ('', 'Onboarding Association Process', (7, 8), 'Ana', '',
     'We should be sending a physical letter at the beginning introducing ourselves'),
    ('', 'YARDI PAYMENTS', (7, 15), 'Ana', '',
     'Email Sales Rep leah.lhlemseldt@yardi.com , jennifer.ocampo@yardi.com'),
    ('Alta Meadows', 'Fix AC Blower/Leak in Ceiling', (7, 17), 'Alexis', 'Jan',
     'Roof is leaking, Email sent to AC company'),
    ('Grand', 'Call concrete companies', (7, 10), 'Alexis', '',
     'Requote sidewalks, walkways, driveways. Fix any cracks'),
    ('Joe', 'Coordinate with Rodger about Furniture', (7, 31), 'Alexis', 'Joe', 'U Haul reserved'),
    ('Linton', 'Mailbox Quotes', (7, 13), 'Alexis', '', 'American Mailbox, Excellent Mailbox, Dream Mailboxes'),
    ('Linton', 'Quote for re-seal/stripe parking lot', (7, 13), 'Alexis', '', 'All County Paving, All Pro Striping'),
    ('Cormorant', 'Pool pump repair or replacement', (7, 8), 'Gia', '',
     'Does the owner want to get this fixed? May continue to be an issue if not'),
    ('111', 'Place thermometer in upstairs living room', (7, 17), 'Gia', 'Jed', 'Next to router, Please do on Friday'),
    ('Alta Meadows', 'Create Info Sheet', (7, 17), 'Ana/Alexis', '', ''),
    ('SA GRAND', 'Automatic Reserve Transfer', (7, 20), 'Justin', '',
     'Turn off Truist automatic reserve transfer; determine new way to auto-transfer from Truist to '
     'Citizens/City National'),
    ('STR', 'Property Information sheets', (7, 25), 'Alexis', '', ''),
    ('', "New Josh's DBPR lic: 108136", (7, 20), 'Gia', '',
     "What license is this? Elevator? Does Gia have a login for Josh? It's not linked to our main "
     "DBPR portal and probably should be."),
    ('', 'ReadyRESALE Pending Order Needs Approval BD5FD5BEA', (7, 21), 'Gia', '', ''),
    ('GRAND', 'Send out Budget Meeting Notice/Post in Community', (7, 25), 'Alexis', '',
     'Waiting for new date to be confirmed'),
]


class Command(BaseCommand):
    help = 'One-time import of the real current task backlog into Ticket rows (idempotent).'

    def handle(self, *args, **options):
        staff_profiles, created_creds = self._ensure_staff()
        for username, password in created_creds:
            self.stdout.write(self.style.WARNING(f'Created login: {username} / {password}'))

        created_count = 0
        skipped_names = set()
        for i, (prop_name, task, (month, day), assigned_raw, follow_up, notes) in enumerate(ROWS):
            prop_name = prop_name.strip()
            prop = None
            if prop_name:
                prop, _ = Property.objects.get_or_create(name=prop_name)

            names = [n.strip() for n in assigned_raw.split('/')]
            primary_key = names[0].lower()
            primary_staff = staff_profiles.get(primary_key)
            if primary_staff is None:
                skipped_names.add(names[0])
            secondary_name = names[1] if len(names) > 1 else None

            description_parts = []
            if notes:
                description_parts.append(f'Notes: {notes}')
            if follow_up:
                description_parts.append(f'Follow-up: {follow_up}')
            if secondary_name:
                description_parts.append(f'Also involves: {secondary_name}')
            description = '\n'.join(description_parts)

            due_date = timezone.make_aware(datetime(YEAR, month, day))

            _, was_created = Ticket.objects.get_or_create(
                source=Ticket.Source.MANUAL, source_reference=f'backlog-{i:03d}', kind='backlog',
                defaults={
                    'title': task,
                    'description': description,
                    'property': prop,
                    'assigned_staff': primary_staff,
                    'priority': Priority.MEDIUM,
                    'status': Ticket.Status.ASSIGNED if primary_staff else Ticket.Status.OPEN,
                    'due_date': due_date,
                },
            )
            if was_created:
                created_count += 1

        if skipped_names:
            self.stdout.write(self.style.WARNING(f'No staff match found for: {", ".join(skipped_names)}'))
        self.stdout.write(self.style.SUCCESS(f'Imported {created_count} new ticket(s) (of {len(ROWS)} rows).'))

    def _ensure_staff(self):
        profiles = {}
        created_creds = []
        for username, display_name in STAFF.items():
            user, created = User.objects.get_or_create(
                username=username, defaults={'first_name': display_name, 'is_staff': True},
            )
            if created:
                password = secrets.token_urlsafe(9)
                user.set_password(password)
                user.save()
                created_creds.append((username, password))
            profile, _ = StaffProfile.objects.get_or_create(user=user)
            profiles[username] = profile
        return profiles, created_creds
