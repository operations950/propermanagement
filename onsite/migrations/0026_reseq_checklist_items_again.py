"""Re-runs 0025's exact repair once more. That migration fixed the
section-scrambling from the old add_item bug, but move_item itself
(fixed in the same commit as this migration — see its own comment in
onsite/views.py) had no transaction/locking at all: two overlapping
move_item requests on the same checklist — a double-click, or several
rapid clicks repositioning a freshly-added item, both realistic given
this same checklist had just been edited a lot while chasing the
original bug — could read the same pre-swap order values and write back
a result that leaves two rows sharing one order value. That produces
exactly the two follow-up reports this fixes: order__lt/order__gt
against a tied value can miss a real neighbor entirely ("can't move
up"), or a swap can land on a value shared by more than one row ("moved
two positions in one click").

Identical logic to 0025 (see its own docstring for the full
explanation) — a fresh migration number because Django only runs each
migration once, and re-sequencing to a fully unique, gap-free 0..N-1
order per visit type is exactly what clears out any duplicate, however
it arose, same as it did the first time. Idempotent, safe to re-run
again if needed."""
from django.db import migrations

CANONICAL_SECTIONS_BY_SLUG = {
    'turnover': ['Entry & Safety', 'Kitchen', 'Bathrooms', 'Bedrooms', 'Living Areas', 'Laundry', 'Exterior', 'Final Walkthrough'],
    'deep-clean': ['Kitchen', 'Bathrooms', 'Bedrooms', 'Living Areas', 'General'],
    'inspection': ['Safety', 'Systems', 'Exterior', 'General'],
}


def fix_order(apps, schema_editor):
    VisitType = apps.get_model('onsite', 'VisitType')
    StandardChecklistItem = apps.get_model('onsite', 'StandardChecklistItem')

    for visit_type in VisitType.objects.all():
        canonical = CANONICAL_SECTIONS_BY_SLUG.get(visit_type.slug, [])
        items = list(StandardChecklistItem.objects.filter(visit_type=visit_type).order_by('order', 'pk'))
        if not items:
            continue

        section_rank = {name: i for i, name in enumerate(canonical)}
        next_rank = len(canonical)
        seen_unknown = {}

        def rank_for(section):
            nonlocal next_rank
            if section in section_rank:
                return section_rank[section]
            if section not in seen_unknown:
                seen_unknown[section] = next_rank
                next_rank += 1
            return seen_unknown[section]

        items.sort(key=lambda item: rank_for(item.section))

        changed = 0
        for new_order, item in enumerate(items):
            if item.order != new_order:
                item.order = new_order
                item.save(update_fields=['order'])
                changed += 1
        print(f'{visit_type.name}: re-sequenced {changed}/{len(items)} item(s) '
              f'({len(seen_unknown)} section(s) not in the canonical list: {list(seen_unknown)})')


class Migration(migrations.Migration):

    dependencies = [
        ('onsite', '0025_fix_scrambled_checklist_item_order'),
    ]

    operations = [
        migrations.RunPython(fix_order, migrations.RunPython.noop),
    ]
