"""One-time retroactive migration: for every multi-unit property, splits
its existing building-wide (unit=None) PropertySupply rows into one
independent row per active Unit — the data-side follow-up to per-unit
supply tracking (see PropertySupply.unit's own docstring and
supplies/services.py::supply_check_context, which already scopes a
cleaner's visit to their own unit's rows now that this field exists).
Before this runs, every existing multi-unit property's supply list is
still one shared list — this is what actually makes each unit
independently trackable.

For each qualifying building-wide row:
- Creates one new PropertySupply(unit=<unit>, ...) per active Unit at
  that property, copying reorder_quantity/display_order — skipped
  (idempotent) if that (unit, item) pair already exists, e.g. from a
  partial prior run or someone having already manually added it.
- Deactivates the original building-wide row (is_active=False, never
  hard-deleted) rather than leaving it active alongside the new
  per-unit ones, which would otherwise show as a confusing duplicate on
  every screen. Deactivating (not deleting) keeps every historical
  SupplyReading/SupplyOrderLine tied to that row fully intact and
  queryable — nothing about past par checks or past orders is lost or
  reattributed to a specific unit that never actually reported them
  (there's no way to know, retroactively, which unit each PAST reading
  was really about, since the old code showed every unit the same
  list). The new per-unit rows start with a clean slate going forward.

Scoped to properties that actually have at least one active Unit AND at
least one active building-wide PropertySupply row — a single-unit
property (zero Units) is completely untouched, as is a multi-unit
property with nothing currently in its supply list.

Backs up every affected row (before/after) to a timestamped JSON file
first. Dry-run by default; --apply required to actually write. Pass
--property <id> to scope to just one property (e.g. to review/test one
building before running the rest)."""
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import Property
from supplies.models import PropertySupply

BACKUP_DIR = Path(settings.BASE_DIR) / 'backups'


class Command(BaseCommand):
    help = (
        'Splits each multi-unit property\'s existing building-wide supply list into one independent '
        'list per unit. Dry-run by default.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Actually write the change (default: dry run).')
        parser.add_argument(
            '--property', type=int, default=None, metavar='PROPERTY_ID',
            help='Only process this one property (its pk) instead of every qualifying multi-unit property.',
        )

    def handle(self, *args, **options):
        apply_changes = options['apply']
        property_id = options['property']

        properties = Property.objects.filter(units__is_active=True).distinct().order_by('name')
        if property_id:
            properties = properties.filter(pk=property_id)
            if not properties.exists():
                raise CommandError(f'Property {property_id} not found, or has no active units.')

        # Phase 1: build the full plan, read-only — no writes yet regardless
        # of --apply, so a crash mid-computation can never leave a
        # half-applied change with no backup describing what happened.
        plan = []  # list of (ps, to_create_units) pairs
        backup_rows = []
        total_new_rows = 0

        for prop in properties:
            units = list(prop.units.filter(is_active=True))
            building_wide = list(PropertySupply.objects.filter(property=prop, unit__isnull=True, is_active=True))
            if not building_wide:
                continue

            self.stdout.write(f'\n{prop.name} ({len(units)} unit(s)):')
            for ps in building_wide:
                existing_unit_ids = set(
                    PropertySupply.objects.filter(property=prop, supply_item=ps.supply_item, unit__in=units)
                    .values_list('unit_id', flat=True),
                )
                to_create_units = [u for u in units if u.pk not in existing_unit_ids]
                self.stdout.write(
                    f'  {ps.supply_item.name}: split onto {len(to_create_units)} unit(s)'
                    + (f' (already exists at {len(units) - len(to_create_units)})' if len(to_create_units) < len(units) else ''),
                )
                plan.append((ps, to_create_units))
                backup_rows.append({
                    'property': prop.name, 'supply_item': ps.supply_item.name,
                    'old_property_supply_id': ps.pk, 'reorder_quantity': ps.reorder_quantity,
                    'split_onto_units': [u.label for u in to_create_units],
                })
                total_new_rows += len(to_create_units)

        self.stdout.write(f'\n{"-" * 40}')
        self.stdout.write(f'Building-wide rows to deactivate: {len(plan)}')
        self.stdout.write(f'New per-unit rows to create: {total_new_rows}')

        if not plan:
            self.stdout.write(self.style.SUCCESS('\nNothing to split — no qualifying multi-unit property has an active building-wide item.'))
            return

        if not apply_changes:
            self.stdout.write(self.style.WARNING('\nDry run — pass --apply to actually make this change.'))
            return

        # Phase 2: everything above resolved cleanly — back up first, then
        # actually write.
        BACKUP_DIR.mkdir(exist_ok=True)
        backup_path = BACKUP_DIR / f'split_property_supplies_by_unit_{timezone.now():%Y%m%d_%H%M%S}.json'
        with open(backup_path, 'w') as f:
            json.dump(backup_rows, f, indent=2)

        for ps, to_create_units in plan:
            PropertySupply.objects.bulk_create([
                PropertySupply(
                    property=ps.property, unit=u, supply_item=ps.supply_item,
                    reorder_quantity=ps.reorder_quantity, display_order=ps.display_order,
                )
                for u in to_create_units
            ])
            ps.is_active = False
            ps.save(update_fields=['is_active'])

        self.stdout.write(self.style.SUCCESS(f'\nDone — {len(plan)} row(s) split, {total_new_rows} new per-unit row(s) created. Backup: {backup_path}'))
