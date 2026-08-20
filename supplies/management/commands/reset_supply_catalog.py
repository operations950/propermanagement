"""One-time reset: replaces the entire supply catalog and every
property's supply list with a fixed new 15-item standard list (see
NEW_CATALOG below), pushed to every property that currently has a supply
list — i.e. the same set of properties being managed under this system
today, not a newly-expanded set.

Deleting every SupplyItem cascades (see supplies/models.py's own FKs) to
every PropertySupply row referencing it, which in turn cascades to every
SupplyReading (the full historical par-level check-in log) and every
SupplyOrderLine tied to those assignments. SupplyOrder rows themselves
are NOT deleted — they're left in place, just emptied of their line
items, exactly like Booking survives Visit deletion in
wipe_unfinished_visits. This is real historical data loss, not just "the
list" — the backup below is what makes it recoverable.

Backs up every deleted SupplyItem/PropertySupply/SupplyReading/
SupplyOrderLine row to a timestamped JSON file first (restorable with
Django's loaddata). Dry-run by default; --apply is required to actually
wipe and replace."""
from pathlib import Path

from django.conf import settings
from django.core import serializers
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from core.models import Property

from ...models import PropertySupply, SupplyItem, SupplyOrderLine, SupplyReading

BACKUP_DIR = Path(settings.BASE_DIR) / 'backups'

NEW_CATALOG = [
    'Toilet paper', 'Paper towels', 'Trash bags', 'Hand soap', 'Dish soap',
    'Dishwasher pods', 'Laundry detergent', 'Sponges', 'Coffee', 'Coffee filters',
    'Coffee pods', 'Sugar / sweetener', 'Shampoo', 'Conditioner', 'Body wash',
]

DEFAULT_REORDER_QUANTITY = 1


class Command(BaseCommand):
    help = (
        'Wipes the entire supply catalog and every property\'s supply list, replacing them with a '
        'fixed new standard list pushed to every property currently managed under supplies. Backs '
        'up everything deleted to a JSON file first. Dry-run by default — pass --apply to actually '
        'wipe and replace.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='Actually wipe and replace (and back up first) — without this, only reports what would happen.',
        )

    def handle(self, *args, **options):
        existing_items = SupplyItem.objects.all()
        existing_supplies = PropertySupply.objects.select_related('property', 'supply_item')
        readings = SupplyReading.objects.filter(property_supply__in=existing_supplies)
        order_lines = SupplyOrderLine.objects.filter(property_supply__in=existing_supplies)

        property_ids = set(existing_supplies.values_list('property_id', flat=True).distinct())
        properties = list(Property.objects.filter(pk__in=property_ids, is_active=True).order_by('name'))

        item_count = existing_items.count()
        supply_count = existing_supplies.count()
        reading_count = readings.count()
        order_line_count = order_lines.count()

        self.stdout.write(f'Current catalog: {item_count} item(s)')
        self.stdout.write(f'Current assignments: {supply_count} across {len(properties)} propert{"y" if len(properties) == 1 else "ies"}:')
        for p in properties:
            self.stdout.write(f'  {p.name}')
        self.stdout.write(
            f'This ALSO permanently deletes {reading_count} historical par-level reading(s) and '
            f'{order_line_count} order line record(s) tied to those assignments — SupplyOrder shells '
            f'themselves are kept, just emptied of their line items.',
        )
        self.stdout.write(f'\nNew catalog ({len(NEW_CATALOG)} items), pushed to all {len(properties)} propert{"y" if len(properties) == 1 else "ies"} above:')
        for name in NEW_CATALOG:
            self.stdout.write(f'  {name}')

        if not options['apply']:
            self.stdout.write(self.style.WARNING(
                '\nDry run — nothing deleted, no backup written, no new items created. Pass --apply to actually wipe and replace.',
            ))
            return

        BACKUP_DIR.mkdir(exist_ok=True)
        backup_path = BACKUP_DIR / f'reset_supply_catalog_{timezone.now():%Y%m%d_%H%M%S}.json'
        backup_rows = list(existing_items) + list(existing_supplies) + list(readings) + list(order_lines)
        backup_path.write_text(serializers.serialize('json', backup_rows, indent=2))
        self.stdout.write(f'Backed up {len(backup_rows)} row(s) to {backup_path}')

        with transaction.atomic():
            existing_items.delete()  # cascades PropertySupply -> SupplyReading + SupplyOrderLine

            new_items = [SupplyItem.objects.create(name=name) for name in NEW_CATALOG]

            new_supplies = [
                PropertySupply(
                    property=prop, supply_item=item,
                    reorder_quantity=DEFAULT_REORDER_QUANTITY, display_order=i,
                )
                for prop in properties
                for i, item in enumerate(new_items)
            ]
            PropertySupply.objects.bulk_create(new_supplies)

        self.stdout.write(self.style.SUCCESS(
            f'\nDone — old catalog and every assignment wiped. {len(new_items)} new item(s) created, '
            f'pushed to {len(properties)} propert{"y" if len(properties) == 1 else "ies"} '
            f'(reorder qty defaulted to {DEFAULT_REORDER_QUANTITY} — adjust per property from that '
            f'property\'s own page).',
        ))
