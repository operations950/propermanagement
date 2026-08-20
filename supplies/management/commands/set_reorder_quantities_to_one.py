"""One-time bulk update: sets every existing PropertySupply.reorder_quantity
to 1 across the whole portfolio — the user always orders exactly 1 of
whatever package size they pick at Walmart directly, rather than relying on
a per-item quantity multiplier.

This only touches rows that already exist. The DEFAULT for any future
PropertySupply row was already changed to 1 in the same change as this
command (see clone_kit_onto_property/push_item_to_adopted_properties in
supplies/services.py, and reset_supply_catalog's own DEFAULT_REORDER_
QUANTITY) — those all used to default to 4.

Backs up every changed row's prior reorder_quantity to a timestamped JSON
file first. Dry-run by default; --apply required to actually write."""
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from ...models import PropertySupply

BACKUP_DIR = Path(settings.BASE_DIR) / 'backups'


class Command(BaseCommand):
    help = 'Sets every PropertySupply.reorder_quantity to 1 across the whole portfolio. Dry-run by default.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Actually write the change (default: dry run).')

    def handle(self, *args, **options):
        to_change = PropertySupply.objects.exclude(reorder_quantity=1).select_related('property', 'supply_item')
        count = to_change.count()
        already_one = PropertySupply.objects.filter(reorder_quantity=1).count()

        self.stdout.write(f'{count} row(s) not already at 1:')
        for ps in to_change[:50]:
            self.stdout.write(f'  {ps.property.name} — {ps.supply_item.name}: {ps.reorder_quantity} -> 1')
        if count > 50:
            self.stdout.write(f'  ... and {count - 50} more')
        self.stdout.write(f'\n{already_one} row(s) already at 1 — untouched either way.')

        if count == 0:
            self.stdout.write(self.style.SUCCESS('\nNothing to change — every row is already at 1.'))
            return

        if not options['apply']:
            self.stdout.write(self.style.WARNING('\nDry run — pass --apply to actually make this change.'))
            return

        backup_rows = [
            {
                'pk': ps.pk, 'property': ps.property.name, 'supply_item': ps.supply_item.name,
                'prior_reorder_quantity': ps.reorder_quantity,
            }
            for ps in to_change
        ]
        BACKUP_DIR.mkdir(exist_ok=True)
        backup_path = BACKUP_DIR / f'set_reorder_quantities_to_one_{timezone.now():%Y%m%d_%H%M%S}.json'
        with open(backup_path, 'w') as f:
            json.dump(backup_rows, f, indent=2)

        updated = to_change.update(reorder_quantity=1)
        self.stdout.write(self.style.SUCCESS(f'\nUpdated {updated} row(s) to reorder_quantity=1. Backup of prior values: {backup_path}'))
