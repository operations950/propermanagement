"""Migration step for the "standard list + exceptions" supply redesign:
marks every currently-active SupplyItem as the actual portfolio-wide
standard list (SupplyItem.is_standard=True), per the user's own
confirmation that "the current list is the standard list" — every item
already in the catalog (including anything added by hand beyond this
command's own original seed set, and every walmart_item_id already
painstakingly filled in) becomes standard by default, automatically
applied to every property/unit going forward (see
supplies.services.resolve_supplies). Idempotent and non-destructive:
never touches name/unit_label/walmart_item_id/standard_reorder_quantity,
and never re-marks an item someone already deliberately set
is_standard=False on unless --force is passed.

On a genuinely empty catalog (a fresh dev environment with no SupplyItem
rows at all), seeds the same starting kit this command used to seed
before the redesign — straight in as is_standard=True — so a new
environment gets a real standard list on first run instead of zero
items."""
from django.core.management.base import BaseCommand

from supplies.models import SupplyItem

# (name, unit_label) — only used to seed a genuinely empty catalog (a
# fresh dev environment). Production's real list is whatever already
# exists in SupplyItem — this fixed list is not authoritative over that.
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
    help = "Marks every existing SupplyItem as the portfolio-wide standard list; seeds STANDARD_KIT on an empty catalog."

    def add_arguments(self, parser):
        parser.add_argument(
            '--force', action='store_true',
            help='Also re-mark items someone already explicitly set is_standard=False on.',
        )

    def handle(self, *args, **options):
        if not SupplyItem.objects.exists():
            created = []
            for name, unit_label in STANDARD_KIT:
                SupplyItem.objects.create(name=name, unit_label=unit_label, is_standard=True)
                created.append(name)
            self.stdout.write(self.style.SUCCESS(f'Empty catalog — seeded {len(created)} starting standard item(s).'))
            return

        qs = SupplyItem.objects.all() if options['force'] else SupplyItem.objects.filter(is_standard=False)
        marked = qs.update(is_standard=True)
        if marked:
            self.stdout.write(self.style.SUCCESS(
                f'Marked {marked} existing catalog item(s) as standard — every property/unit now follows them '
                'automatically. Names, units, Walmart ids, and reorder quantities were not touched.',
            ))
        else:
            self.stdout.write(
                'Every item is already marked standard — nothing to do. '
                'Pass --force to re-mark items someone explicitly unmarked.',
            )
