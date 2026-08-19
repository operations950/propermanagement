"""One-time load of the initial internal-cleaner Cleaning pay figures the
user provided for the STR portfolio, onto Property.cleaning_fee (and, for
the two real multi-unit buildings in the list, Unit.cleaning_fee — created
if the Unit doesn't exist yet). Exists so these ~20 numbers don't have to
be typed in one at a time through property_detail's UI.

Matches by exact Property.name — a few of the user's rows used a
shorthand or slightly different spelling than what's actually stored (see
each entry's own comment below); those were resolved against the real
portfolio before writing this file, not guessed at runtime. One row
("712 — Joe's Unit", $85) had no confident match against any existing
Property or Unit and is deliberately left out — see NOTE below.

Dry-run by default; --apply required to actually write, same convention
as merge_property_units/migrate_local_media_to_cloudinary. Backs up every
row's prior cleaning_fee (and whether a Unit already existed) to a JSON
file before writing. Idempotent — safe to re-run; a second --apply just
re-sets the same values (and updates a since-existing Unit's price
in place rather than duplicating it).

Deliberately NOT wired into Procfile — a one-time load of a specific
pricing table someone handed over in chat isn't something that should
silently re-fire on every future deploy."""
import json
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from core.models import Property, Unit

BACKUP_DIR = Path(settings.BASE_DIR) / 'backups'

# NOTE: the user's original table also listed "712 — Joe's Unit  $85" —
# there is no Property or Unit anywhere in the portfolio that this
# confidently matches (it isn't one of the six existing "712 NE 8th" /
# "70x NE 7th Ct" rows, all of which are already accounted for below under
# their own names). Left out on purpose rather than guessed. Price it
# manually once it's clear which real property/unit this refers to.
PRICES = [
    {'property': '100 Neptune', 'price': '325.00'},
    {'property': '111 NW 1st Ave', 'price': '250.00'},
    {'property': '224 NW 4th Ave', 'price': '185.00'},
    # User's table said "2821 Frederick Blvd" — the portfolio's actual name is "2821 Frederick" (only one match).
    {'property': '2821 Frederick', 'price': '195.00'},
    {'property': '2919 Cormorant Rd', 'price': '235.00'},
    # Property has no Units yet — created here from the user's "Unit 1"/"Unit 2"/"Chic Cottage" labels.
    {'property': '323/325 Decarie', 'unit': 'Unit 1', 'price': '125.00'},
    {'property': '323/325 Decarie', 'unit': 'Unit 2', 'price': '125.00'},
    {'property': '323/325 Decarie', 'unit': 'Chic Cottage', 'price': '85.00'},
    {'property': '4606 Brady', 'price': '200.00'},
    # User's table said "702 SW 1st St" — the portfolio's actual name is "702 NW 1st St" (only one match;
    # worth double-checking this property's real street direction is correct in the system).
    {'property': '702 NW 1st St', 'price': '200.00'},
    # "712 NE 8th"'s Modern/Stylish units are their own Property rows today (the multi-unit merge into
    # real Unit records — see the Unit-model build plan's Phase 2 — hasn't been run on this building yet).
    {'property': '712 NE 8th (Modern)', 'price': '70.00'},
    {'property': '712 NE 8th (Stylish)', 'price': '70.00'},
    # Same situation for the "706-710 NE 7th Ct" trio the user's table grouped under "712 NE 8th."
    {'property': '710 NE 7th Ct (Blue Ocean)', 'price': '80.00'},
    {'property': '708 NE 7th Ct (Pearl)', 'price': '80.00'},
    {'property': '706 NE 7th Ct (Seashell)', 'price': '80.00'},
    {'property': '716 Kittyhawk', 'price': '325.00'},
    # Property has no Units yet — created here from the user's "A"/"B"/"C"/"D" labels.
    {'property': '800 Tropic', 'unit': 'A', 'price': '110.00'},
    {'property': '800 Tropic', 'unit': 'B', 'price': '110.00'},
    {'property': '800 Tropic', 'unit': 'C', 'price': '110.00'},
    {'property': '800 Tropic', 'unit': 'D', 'price': '110.00'},
    # User's table said "803 NE 7th" — the portfolio's actual name is "803 NE 7th Ave" (only one match).
    {'property': '803 NE 7th Ave', 'price': '100.00'},
]


class Command(BaseCommand):
    help = 'Loads the initial STR Cleaning pay figures onto Property/Unit.cleaning_fee. Dry-run by default.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Actually write the prices (default is dry-run).')

    def handle(self, *args, **options):
        apply = options['apply']
        backup_rows = []
        errors = []
        planned = []

        # Phase 1: resolve every row against the real database and describe
        # what would happen — no writes yet, regardless of --apply, so a
        # crash mid-resolution can never leave a half-applied backup file.
        for row in PRICES:
            price = Decimal(row['price'])
            try:
                prop = Property.objects.get(name=row['property'])
            except Property.DoesNotExist:
                errors.append(f"No Property named {row['property']!r} — skipped (${price}).")
                continue

            unit_label = row.get('unit')
            if unit_label:
                unit = Unit.objects.filter(property=prop, label=unit_label).first()
                if unit:
                    backup_rows.append({
                        'property': prop.name, 'unit': unit_label, 'unit_id': unit.pk,
                        'existed': True, 'prior_cleaning_fee': str(unit.cleaning_fee) if unit.cleaning_fee is not None else None,
                    })
                    line = f'  {prop.name} — {unit_label}: update existing unit -> ${price}'
                else:
                    backup_rows.append({'property': prop.name, 'unit': unit_label, 'existed': False, 'prior_cleaning_fee': None})
                    line = f'  {prop.name} — {unit_label}: CREATE unit -> ${price}'
            else:
                backup_rows.append({
                    'property': prop.name,
                    'prior_cleaning_fee': str(prop.cleaning_fee) if prop.cleaning_fee is not None else None,
                })
                line = f'  {prop.name}: ${prop.cleaning_fee if prop.cleaning_fee is not None else "unset"} -> ${price}'
            planned.append((prop, unit_label, price, line))

        for _, _, _, line in planned:
            self.stdout.write(line)
        for err in errors:
            self.stdout.write(self.style.ERROR(err))
        self.stdout.write(
            "NOTE: \"712 — Joe's Unit\" ($85) from the original table has no confident match and was NOT "
            'applied — see this command\'s own module docstring/PRICES comment.',
        )

        if not apply:
            self.stdout.write(self.style.WARNING(f'\nDry run — {len(planned)} row(s) would be written. Re-run with --apply to write them.'))
            return

        # Phase 2: everything resolved cleanly above — now actually write,
        # all in one transaction (either every row lands, or none do).
        with transaction.atomic():
            for prop, unit_label, price, _ in planned:
                if unit_label:
                    Unit.objects.update_or_create(property=prop, label=unit_label, defaults={'cleaning_fee': price})
                else:
                    prop.cleaning_fee = price
                    prop.save(update_fields=['cleaning_fee'])

        BACKUP_DIR.mkdir(exist_ok=True)
        backup_path = BACKUP_DIR / f'set_initial_cleaning_prices_{timezone.now():%Y%m%d_%H%M%S}.json'
        with open(backup_path, 'w') as f:
            json.dump(backup_rows, f, indent=2)
        self.stdout.write(self.style.SUCCESS(f'\nApplied {len(planned)} row(s). Backup of prior state: {backup_path}'))
        if errors:
            self.stdout.write(self.style.ERROR(f'{len(errors)} row(s) could not be matched — see above.'))
