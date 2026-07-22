"""One-off: replace the messy, inconsistent property names that accumulated
from manual entry and Quo auto-matching (LAP, SAG, SAGRAND, SA GRAND, GRAND,
GLEN, SA GLEN, ...) with the real canonical property list the user provided,
reassign every ticket/reservation/contact/supply-request pointing at an old
name, then delete the old rows. Also retires the fictional demo properties
(Sunset Villa, Lakeside Cabin, Downtown Loft) now that real data exists.

Short-term rentals are named by their short address per instruction ("we
should refer to these by their short address"); the Airbnb listing name is
kept in Property.notes for reference.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import Contact, Property
from intake.models import Reservation
from supplies.models import SupplyOrderBatch, SupplyRequest
from tickets.models import Ticket

ASSOCIATIONS = ['La Pensee', 'St Andrews Grand', 'Lakeside Point 2', 'Lakeside Point 8', 'St Andrews Glen', 'Linton Woods']

COMMERCIAL = [
    ('324 Lofts', '324 NE 3rd, 33444'),
    ('Del Park LLC', '325 NE 3rd, 33444'),
    ('Red Barn 381', '381 NE 3rd, 33444'),
]

# (short address / canonical name, Airbnb listing name for notes)
STRS = [
    ('712 NE 8th (Modern)', 'Modern'),
    ('706 NE 7th Ct (Seashell)', 'Seashell'),
    ('710 NE 7th Ct (Blue Ocean)', 'Blue Ocean'),
    ('712 NE 8th (Stylish)', 'Stylish'),
    ('708 NE 7th Ct (Pearl)', 'Pearl'),
    ('712 NE 8th (Bamboo)', 'Bamboo House'),
    ('803 NE 7th Ave', 'Blue Beach Cottage'),
    ('716 Kittyhawk', 'Boat + Pool: Aqua Escape'),
    ('100 Neptune', 'Mannatee Way'),
    ('4606 Brady', 'Green Palms Getaway'),
    ('111 NW 1st Ave', '111 NW 1st Ave'),
    ('800 Tropic', '800 Tropic'),
    ('323/325 Decarie', 'De Carie'),
    ('224 NW 4th Ave', 'Big Blue'),
    ('702 NW 1st St', "Kelvin's"),
    ('2919 Cormorant Rd', 'Canal'),
    ('2821 Frederick', 'Frederick'),
    ('300 Ross', 'Jungle Paradise'),
    ('2036 Alta Meadows', 'Alta Medows'),
    ('1038 E Heritage', 'Terry Hunter'),
    ('324 Harmon Ct', '324 Harmon Court'),
    ('1291 Laing St', '1291 Laing St'),
]

# old messy name -> canonical name. Anything mapping to None gets moved back
# to pending (property=None) instead of guessed, for a human to resolve.
OLD_TO_NEW = {
    'LAP': 'La Pensee',
    'LAP SAG': 'La Pensee',  # covered both La Pensee and St Andrews Grand — noted in description
    'SAG': 'St Andrews Grand',
    'SAGRAND': 'St Andrews Grand',
    'SA GRAND': 'St Andrews Grand',
    'GRAND': 'St Andrews Grand',
    'Grand': 'St Andrews Grand',
    'SA GLEN': 'St Andrews Glen',
    'GLEN': 'St Andrews Glen',
    'Linton': 'Linton Woods',
    '324': '324 Lofts',
    'Neptune': '100 Neptune',
    'Cormorant': '2919 Cormorant Rd',
    '111': '111 NW 1st Ave',
    'Alta Meadows': '2036 Alta Meadows',
    'Abrams': None,   # not in the real property list — no confident match
    'Joe': None,      # looks like a person's name mistakenly in the property column
    'STR': None,      # generic "short-term rental" label, not a specific unit
}

DEMO_PROPERTY_NAMES = ['Sunset Villa', 'Lakeside Cabin', 'Downtown Loft']


class Command(BaseCommand):
    help = 'Replace messy property names with the real canonical list and retire fictional demo properties.'

    @transaction.atomic
    def handle(self, *args, **options):
        canonical = {}
        for name in ASSOCIATIONS:
            prop, _ = Property.objects.get_or_create(name=name)
            canonical[name] = prop
        for name, address in COMMERCIAL:
            prop, _ = Property.objects.get_or_create(name=name, defaults={'address': address})
            canonical[name] = prop
        for name, airbnb_name in STRS:
            prop, created = Property.objects.get_or_create(
                name=name, defaults={'address': name, 'notes': f'Airbnb listing: {airbnb_name}'},
            )
            canonical[name] = prop
        self.stdout.write(f'Canonical properties ready: {len(canonical)}')

        moved = 0
        for old_name, new_name in OLD_TO_NEW.items():
            try:
                old_prop = Property.objects.get(name=old_name)
            except Property.DoesNotExist:
                continue

            if new_name is None:
                n = Ticket.objects.filter(property=old_prop).update(property=None)
                self.stdout.write(f'{old_name!r}: {n} ticket(s) moved back to pending (no confident match)')
            else:
                new_prop = canonical[new_name]
                if old_name == 'LAP SAG':
                    Ticket.objects.filter(property=old_prop).update(
                        property=new_prop, description="Covers both La Pensee and St Andrews Grand.",
                    )
                    n = Ticket.objects.filter(property=new_prop, description__startswith='Covers both La Pensee').count()
                else:
                    n = Ticket.objects.filter(property=old_prop).update(property=new_prop)
                self.stdout.write(f'{old_name!r} -> {new_name!r}: {n} ticket(s)')
                moved += n

            new_prop = canonical.get(new_name)
            Contact.objects.filter(property=old_prop).update(property=new_prop)
            Reservation.objects.filter(property=old_prop).update(property=new_prop)
            SupplyRequest.objects.filter(property=old_prop).update(property=new_prop)
            if new_prop is not None:
                SupplyOrderBatch.objects.filter(property=old_prop).update(property=new_prop)
            # (no unreassigned SupplyOrderBatch rows exist for the unmapped
            # names — verified before writing this command — so it's safe
            # to fall through to delete() below even when new_prop is None.)

            old_prop.delete()

        # Resolve two pending tickets we can now confidently match by address.
        addr_resolves = {
            '716 Kittyhawk': ['Kittyhawk'],
            '4606 Brady': ['Brady'],
        }
        for prop_name, needles in addr_resolves.items():
            prop = canonical[prop_name]
            for needle in needles:
                n = Ticket.objects.filter(property__isnull=True, title__icontains=needle).update(property=prop)
                if n:
                    self.stdout.write(f'Resolved {n} pending ticket(s) matching {needle!r} -> {prop_name!r}')
            for needle in needles:
                n = SupplyRequest.objects.filter(property__isnull=True, raw_text__icontains=needle).update(property=prop)
                if n:
                    self.stdout.write(f'Resolved {n} pending supply request(s) matching {needle!r} -> {prop_name!r}')

        # Retire fictional demo properties now that real data exists.
        demo_tickets = Ticket.objects.filter(property__name__in=DEMO_PROPERTY_NAMES)
        demo_count = demo_tickets.count()
        demo_tickets.delete()
        for name in DEMO_PROPERTY_NAMES:
            Property.objects.filter(name=name).delete()
        self.stdout.write(f'Removed {demo_count} demo ticket(s) and {len(DEMO_PROPERTY_NAMES)} demo properties.')

        self.stdout.write(self.style.SUCCESS(
            f'Done. {moved} ticket(s) migrated to canonical properties. {Property.objects.count()} properties remain.'
        ))
