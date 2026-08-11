"""Migrates existing "fake unit" Short-Term Rental properties — a multi-
unit building that today has one Property row PER unit, distinguished only
by a "(Unit Name)" suffix on the name (the real production example this
was built for: "712 NE 8th (Bamboo)"/"(Modern)"/"(Stylish)") — into one
real Property (the building) with a Unit row per former sibling, re-
parenting every Booking/Visit/Ticket/etc. that referenced the old rows.

Dry-run by default: with no flags, only detects and lists candidate groups
— nothing is written. Two ways to actually apply:
    manage.py merge_property_units --apply "712 NE 8th"
    manage.py merge_property_units --apply-all

Mirrors two existing patterns in this codebase rather than inventing a new
one: core/duplicates.py::merge_contacts's "bulk-reassign every referencing
model inside one transaction.atomic()" shape, and
tickets/management/commands/wipe_recurring_tickets.py's "back up to a
timestamped JSON file first, dry-run by default" shape.

For every model with a unique constraint that includes `property` (a
sibling and the new primary could each already have a conflicting row —
e.g. both already have a PropertySupply row for the same SupplyItem), the
sibling's row is dropped rather than raised on, exactly like
merge_contacts's TicketContact special case: the primary's existing row
already covers it, nothing is lost.

Booking/Visit/PropertyListingName are the only models with a `unit` field
as of this writing — they get both `property` and `unit` reassigned.
Everything else only gets `property` reassigned; if Ticket/TicketTemplate/
SessionLine ever gain their own `unit` field, this command should be
revisited to set it too rather than leaving those rows building-level."""
import re
from pathlib import Path

from django.conf import settings
from django.core import serializers
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from core.models import Property, PropertyListingName, Unit
from onsite.models import (
    Booking, ImportBatch, PropertyChecklistItem, PropertyChecklistOverride,
    PropertyChecklistReview, Visit, VisitRule,
)
from processes.models import ProcessRun
from supplies.models import PropertySupply, SupplyOrder, SupplyOrderBatch, SupplyRequest
from tickets.models import FollowUpLog, PropertyPackage, PropertyTemplateOverride, Ticket, TicketTemplate
from worksessions.models import SessionLine

BACKUP_DIR = Path(settings.BASE_DIR) / 'backups'

_SUFFIX_RE = re.compile(r'\s*\([^)]*\)\s*$')

# Models whose `property` FK carries a unique constraint alongside another
# field — a sibling and the eventual primary could each already have a
# conflicting row, so these are reassigned row-by-row (drop-on-collision)
# rather than with a single bulk .update(). Each entry is
# (model, [unique fields besides `property`]).
CONFLICT_SAFE_MODELS = [
    (SupplyRequest, ['source_reference', 'item_guess']),
    (SupplyOrderBatch, ['date']),
    (PropertySupply, ['supply_item']),
    (PropertyPackage, ['package']),
    (PropertyTemplateOverride, ['template']),
    (PropertyChecklistOverride, ['standard_item']),
    (PropertyChecklistReview, ['visit_type']),
]

# Plain bulk-reassignable models — a `property` FK with no unique
# constraint referencing it, so one .update() per source property is safe.
# Ticket and SessionLine are handled alongside Booking/Visit/
# PropertyListingName instead (see merge_group) since they also carry a
# `unit` field to backfill, not just `property`.
PLAIN_REASSIGN_MODELS = [
    PropertyChecklistItem, VisitRule, ImportBatch, SupplyOrder,
    TicketTemplate, ProcessRun, FollowUpLog,
]


def _normalize_base_name(name):
    """"712 NE 8th (Bamboo)" -> "712 NE 8th". A name with no trailing
    "(...)" is returned unchanged, so it only ever groups with something
    that shares its exact full name — never a false-positive match."""
    return _SUFFIX_RE.sub('', name).strip()


def _extract_label(original_name, base_name):
    """"712 NE 8th (Bamboo)" -> "Bamboo". Falls back to the full original
    name on the rare row with no parenthetical suffix at all (still a
    perfectly usable, if less tidy, Unit label)."""
    match = _SUFFIX_RE.search(original_name)
    if match:
        label = original_name[match.start():].strip().strip('()').strip()
        if label:
            return label
    return original_name


def detect_groups():
    """Read-only. Groups active, non-general Short-Term Rental properties
    by normalized base name — the exact shape of the real
    712 NE 8th (Bamboo)/(Modern)/(Stylish) pattern. A group of 1 (nothing
    to merge) never appears. Restricted to Short-Term Rentals: the
    "(Unit Name)" suffix is a booking-platform-listing naming convention,
    not something that legitimately happens by coincidence on Association/
    Commercial/other property names."""
    candidates = {}
    properties = Property.objects.filter(
        is_active=True, is_general=False, property_type=Property.Type.SHORT_TERM_RENTAL,
    ).order_by('pk')
    for prop in properties:
        base = _normalize_base_name(prop.name)
        candidates.setdefault(base, []).append(prop)
    return {base: props for base, props in candidates.items() if len(props) > 1}


def _reassign_conflict_safe(model, unique_fields, source, target):
    """Every row of `model` currently pointing at `source` either moves to
    `target` or, if `target` already has an equivalent row (same unique
    fields), is dropped — the primary's existing row already covers it, so
    nothing is actually lost, exactly like merge_contacts's TicketContact
    special case."""
    moved = skipped = 0
    for row in list(model.objects.filter(property=source)):
        key = {field: getattr(row, field) for field in unique_fields}
        if model.objects.filter(property=target, **key).exists():
            row.delete()
            skipped += 1
        else:
            row.property = target
            row.save(update_fields=['property'])
            moved += 1
    return moved, skipped


def _collect_backup_rows(properties):
    """Everything actually at risk of being destroyed by a merge of this
    group: the Property rows themselves (one gets renamed, the rest get
    deactivated) and every row from the conflict-safe models scoped to
    them (the only rows a merge can ever .delete() outright, when they
    collide with something the primary already has)."""
    rows = list(properties)
    for model, _unique_fields in CONFLICT_SAFE_MODELS:
        rows.extend(model.objects.filter(property__in=properties))
    return rows


@transaction.atomic
def merge_group(base_name, properties):
    """The actual merge for one detected group. Picks the earliest-created
    property (lowest pk) as the survivor, renames it to the shared base
    name (it now represents the whole building, not just its own former
    unit), creates one Unit per original sibling — including the primary's
    own former identity, since its existing history needs re-tagging with
    a real unit too, not left implicitly "whichever unit has no unit set"
    — and re-parents every referencing row. Returns a summary dict for
    reporting; writes nothing if this raises partway through, since the
    whole thing is one transaction."""
    properties = sorted(properties, key=lambda p: p.pk)
    primary = properties[0]
    original_names = {p.pk: p.name for p in properties}

    if primary.name != base_name:
        primary.name = base_name
        primary.save(update_fields=['name'])

    units_by_property_id = {}
    for prop in properties:
        label = _extract_label(original_names[prop.pk], base_name)
        unit, _created = Unit.objects.get_or_create(property=primary, label=label)
        units_by_property_id[prop.pk] = unit

    summary = {'primary': primary.name, 'units': [], 'moved': {}, 'skipped': {}}

    for prop in properties:
        unit = units_by_property_id[prop.pk]
        summary['units'].append({'label': unit.label, 'source_property': original_names[prop.pk]})

        # The unit-aware models — both `property` and `unit` reassigned.
        # Also correct for prop == primary: its own existing bookings/
        # visits/tickets/listing names/session lines get tagged with the
        # new Unit representing what used to be its whole identity.
        Booking.objects.filter(property=prop).update(property=primary, unit=unit)
        Visit.objects.filter(property=prop).update(property=primary, unit=unit)
        PropertyListingName.objects.filter(property=prop).update(property=primary, unit=unit)
        Ticket.objects.filter(property=prop).update(property=primary, unit=unit)
        SessionLine.objects.filter(property=prop).update(property=primary, unit=unit)

        # Everything below is a reassignment FROM a sibling TO the primary
        # — meaningless (and, for the conflict-safe models, actively wrong:
        # a row would "collide" with itself and get deleted) when prop IS
        # the primary, since its rows already point at itself.
        if prop.pk == primary.pk:
            continue

        for model in PLAIN_REASSIGN_MODELS:
            model.objects.filter(property=prop).update(property=primary)

        for model, unique_fields in CONFLICT_SAFE_MODELS:
            moved, skipped = _reassign_conflict_safe(model, unique_fields, prop, primary)
            name = model.__name__
            summary['moved'][name] = summary['moved'].get(name, 0) + moved
            summary['skipped'][name] = summary['skipped'].get(name, 0) + skipped

        # Contact.properties M2M — additive only, no conflict possible.
        for contact in prop.contacts.all():
            contact.properties.add(primary)

        prop.is_active = False
        prop.save(update_fields=['is_active'])

    return summary


class Command(BaseCommand):
    help = (
        'Detects Short-Term Rental properties that are really units of one building (same address, '
        'split only by a "(Unit Name)" suffix — e.g. "712 NE 8th (Bamboo)"/"(Modern)"/"(Stylish)") and '
        'merges each group into one Property + N Units, re-parenting every row that referenced the old '
        'per-unit Property rows. Dry-run by default.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', metavar='BASE_NAME', default=None,
            help='Apply only the one detected group whose base name exactly matches this.',
        )
        parser.add_argument('--apply-all', action='store_true', help='Apply every detected group.')

    def handle(self, *args, **options):
        groups = detect_groups()
        if not groups:
            self.stdout.write(self.style.SUCCESS('No candidate multi-unit groups detected.'))
            return

        self.stdout.write(f'Detected {len(groups)} candidate group(s):\n')
        for base, props in sorted(groups.items()):
            self.stdout.write(f'  "{base}" ({len(props)} properties):')
            for p in props:
                self.stdout.write(f'    - [{p.pk}] {p.name}')

        apply_all = options['apply_all']
        apply_one = options['apply']
        if not apply_all and not apply_one:
            self.stdout.write(self.style.WARNING(
                '\nDry run — nothing merged. Re-run with --apply "<base name>" for one group above, '
                'or --apply-all for every group listed above.'
            ))
            return

        if apply_all:
            targets = list(groups.items())
        elif apply_one in groups:
            targets = [(apply_one, groups[apply_one])]
        else:
            self.stderr.write(self.style.ERROR(f'No detected group matches "{apply_one}" — see the list above.'))
            return

        BACKUP_DIR.mkdir(exist_ok=True)
        backup_path = BACKUP_DIR / f'merge_property_units_{timezone.now():%Y%m%d_%H%M%S}.json'
        backup_rows = []
        for _base, props in targets:
            backup_rows.extend(_collect_backup_rows(props))
        backup_path.write_text(serializers.serialize('json', backup_rows, indent=2))
        self.stdout.write(f'\nBacked up {len(backup_rows)} row(s) to {backup_path}')

        for base, props in targets:
            summary = merge_group(base, props)
            self.stdout.write(self.style.SUCCESS(f'\nMerged "{base}":'))
            for u in summary['units']:
                self.stdout.write(f'  Unit "{u["label"]}" <- {u["source_property"]}')
            for name, moved in summary['moved'].items():
                skipped = summary['skipped'].get(name, 0)
                if moved or skipped:
                    self.stdout.write(f'  {name}: moved {moved}, skipped {skipped} (already existed on the primary)')

        self.stdout.write(self.style.SUCCESS(f'\nDone. {len(targets)} group(s) merged.'))
