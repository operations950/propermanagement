"""Repairs already-created visits affected by the amenity-gating bug fixed
in the previous migration (0028). Before that fix, 8 real
StandardChecklistItems (across Turnover Clean and Deep Clean) silently
never resolved into ANY visit's checklist, because they required a
PropertyAttribute tag ("Pool", "Washer/Dryer In-Unit", etc.) that zero
properties had ever actually been assigned — see
onsite/services/checklist.py::resolve_checklist's docstring for the full
story. Removing the gate (0028) only changes what NEW visits get; a visit
created before this fix has its checklist already frozen into
VisitChecklistItem rows (by design — see VisitChecklistItem's own
docstring), so it stays missing those items until repaired here.

Only touches visits that are safe to add to: started_at IS NULL (nothing
has been worked on yet — the exact same rule set_deep_clean() already uses
for "is this visit's checklist still allowed to change"), excluding
cancelled/skipped visits. For each such visit, adds any active
StandardChecklistItem for its visit_type whose text isn't already present
on the visit (covers the 8 known items and, harmlessly, anything else that
happens to be missing) — unless a PropertyChecklistOverride hides it for
that specific property, which stays respected. Uses each item's own
`order` value directly (with the property's order_override applied same as
resolve_checklist()) rather than shifting anything else: since these
items were always excluded before, no existing item on the visit already
holds that order value, so inserting at it is a plain gap-fill, not a
collision — nothing else needs to move.

Idempotent — re-running finds nothing left to add. Safe to re-run if
needed."""
from decimal import Decimal

from django.db import migrations


def _resolve_multiplier(property, unit, scales_by):
    """Mirrors onsite/services/checklist.py::_resolve_multiplier exactly —
    duplicated rather than imported since a migration should stay
    self-contained against the historical model state, not live app code
    that can keep changing after this migration is written."""
    if scales_by == 'bedrooms':
        value = unit.bedroom_count if unit and unit.bedroom_count is not None else property.bedroom_count
        return value or 0
    if scales_by == 'beds':
        value = unit.bed_count if unit and unit.bed_count is not None else property.bed_count
        return value or 0
    if scales_by == 'bathrooms':
        value = unit.bathroom_count if unit and unit.bathroom_count is not None else property.bathroom_count
        return value if value is not None else Decimal('0')
    if scales_by == 'sqft':
        value = unit.square_footage if unit and unit.square_footage is not None else property.square_footage
        return (Decimal(value) / Decimal('1000')) if value else Decimal('0')
    return 1  # flat


def _item_minutes(property, unit, minutes, scales_by):
    return int(round(minutes * _resolve_multiplier(property, unit, scales_by)))


def backfill(apps, schema_editor):
    Visit = apps.get_model('onsite', 'Visit')
    VisitChecklistItem = apps.get_model('onsite', 'VisitChecklistItem')
    StandardChecklistItem = apps.get_model('onsite', 'StandardChecklistItem')
    PropertyChecklistOverride = apps.get_model('onsite', 'PropertyChecklistOverride')

    candidates = (
        Visit.objects.filter(started_at__isnull=True)
        .exclude(status__in=['cancelled', 'skipped'])
        .select_related('property', 'unit', 'visit_type')
    )
    if not candidates.exists():
        print('No not-yet-started visits to check — nothing to backfill.')
        return

    added_total = 0
    visits_touched = 0
    for visit in candidates:
        standard_items = StandardChecklistItem.objects.filter(visit_type=visit.visit_type, is_active=True)
        if not standard_items.exists():
            continue
        existing_texts = set(visit.checklist_items.values_list('text', flat=True))
        overrides_by_item = {
            o.standard_item_id: o
            for o in PropertyChecklistOverride.objects.filter(property=visit.property, visit_type=visit.visit_type)
        }
        added_here = 0
        for item in standard_items:
            if item.text in existing_texts:
                continue
            override = overrides_by_item.get(item.id)
            if override and override.is_hidden:
                continue
            VisitChecklistItem.objects.create(
                visit=visit,
                source='standard',
                section=item.section,
                order=override.order_override if override and override.order_override is not None else item.order,
                text=item.text,
                mandatory=override.mandatory_override if override and override.mandatory_override is not None else item.mandatory,
                requires_photo=item.requires_photo,
                requires_note=item.requires_note,
                is_new_unreviewed=False,
                minutes=_item_minutes(visit.property, visit.unit, item.minutes, item.scales_by),
            )
            added_here += 1
        if added_here:
            added_total += added_here
            visits_touched += 1
            print(f'Visit #{visit.pk} ({visit.property.name}, {visit.visit_type.name}): added {added_here} item(s).')

    print(f'Backfill complete: {added_total} item(s) added across {visits_touched} visit(s) '
          f'({candidates.count()} not-yet-started visits checked).')


class Migration(migrations.Migration):

    dependencies = [
        ('onsite', '0028_remove_standardchecklistitem_required_attributes'),
        ('core', '0040_remove_property_cleaning_fee_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
