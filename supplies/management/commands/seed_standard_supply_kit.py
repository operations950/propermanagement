"""Idempotent seed for a reasonable starting SupplyItem catalog — the
build brief's own "someone has to enter ~20 items with Walmart IDs for
every property, and if that's manual per property, this never gets set
up" adoption-risk concern. Seeds the ITEMS (get_or_create by name, safe to
re-run), not walmart_item_id — nobody but a human picking the exact
product on Walmart can resolve that (see SupplyItem's own docstring: "the
whole point is that product identity is resolved once, by a human"), so
every seeded item starts with a blank id until someone fills it in.

Once seeded, clone_kit_onto_property (supplies/services.py) is what
actually gets a property stocked — see the blind spots page's "Clone
standard kit" button. Safe to run repeatedly; re-running never touches an
item that already exists (so a walmart_item_id someone already filled in
is never clobbered) and never duplicates."""
from django.core.management.base import BaseCommand

from supplies.models import SupplyItem

# (name, unit_label) — a generic short-term-rental turnover kit. Real par
# levels/reorder quantities are set per-property (PropertySupply), not
# here; Walmart ids are filled in by a human from Admin or the catalog
# link on the cart page once this seeds the item.
STANDARD_KIT = [
    ('Toilet Paper', 'roll'),
    ('Paper Towels', 'roll'),
    ('Dish Soap', 'ea'),
    ('Hand Soap', 'ea'),
    ('Laundry Detergent Pods', 'ct'),
    ('Dishwasher Pods', 'ct'),
    ('All-Purpose Cleaner', 'ea'),
    ('Glass Cleaner', 'ea'),
    ('Trash Bags', 'ct'),
    ('Sponges', 'ct'),
    ('Coffee Pods', 'ct'),
    ('Hand Towels', 'ea'),
]


class Command(BaseCommand):
    help = 'Seeds a reusable "standard STR kit" of SupplyItem rows — real Walmart ids added by a human afterward.'

    def handle(self, *args, **options):
        created_count = 0
        for name, unit_label in STANDARD_KIT:
            _, created = SupplyItem.objects.get_or_create(name=name, defaults={'unit_label': unit_label})
            if created:
                created_count += 1
                self.stdout.write(f'Created: {name}')
        if created_count == 0:
            self.stdout.write('Standard kit already seeded — nothing new to create.')
        else:
            self.stdout.write(self.style.SUCCESS(
                f'Seeded {created_count} new item(s). Set each one\'s Walmart item id from Admin before relying '
                'on it for a cart — a blank id gets skipped on send, flagged as a catalog problem.',
            ))
