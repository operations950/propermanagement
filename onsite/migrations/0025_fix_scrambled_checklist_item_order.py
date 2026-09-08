"""One-time repair for data corrupted by the add_item bug fixed in the
same commit as this migration (onsite/views.py::checklist_template_detail):
a new item's `order` was computed as (max order WITHIN its own section) + 1
— correct only when that section already held the highest order values in
the whole list. Adding to any section positioned earlier in the intended
flow than the current highest section (or to a section string that didn't
exactly match any existing item — a stray space or a genuinely new section
both hit this) landed the new item's order INSIDE or BEFORE another
section's block, breaking StandardChecklistItem.Meta's "single order sort
keeps sections contiguous" invariant — worst case (a brand new section
name, or a typo'd variant of an existing one) put the new item at order=1,
jumping it to the very front of the whole checklist. Traced from a real
user report ("the arrows don't move items") backed by a screen recording
showing an "Exterior" item displayed ahead of "Entry & Safety"/"Kitchen"
on the real Turnover Clean checklist.

Repairs every VisitType's items (not just Turnover Clean — Deep Clean and
Property Inspection could have the identical corruption if anything was
ever added to them the same way) by re-sequencing order values grouped by
a canonical section order (read directly off this environment's own dev
database, which was never touched by the bug — the exact section list
seeded by seed_checklist_templates.py, still contiguous and correct
there), then by each item's EXISTING order within its own section — that
inner ordering is presumably still correct, since the bug only ever
affected where a section's block landed in the GLOBAL sequence, never the
relative order of items already inside one section. Any section name
encountered that isn't in the canonical list for that VisitType (a
genuinely new section, or the string-mismatch case above) is appended
after every canonical section, in whichever order it was first
encountered — nothing is silently dropped.

Purely a reassignment of the `order` column; no item's text/section/
mandatory/etc. is touched. Idempotent — safe to re-run (a second run just
recomputes the same already-correct sequence)."""
from django.db import migrations

# Read directly from this environment's own (uncorrupted) dev database at
# the time this migration was written — see the migration's own docstring.
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
        items = list(StandardChecklistItem.objects.filter(visit_type=visit_type).order_by('order'))
        if not items:
            continue

        section_rank = {name: i for i, name in enumerate(canonical)}
        # Any section not in the canonical list sorts after all of them,
        # in first-seen order (stable sort preserves that automatically
        # since these items are already order'd going in).
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

        # Stable sort: within the same section, items keep their existing
        # relative order (Python's sort is guaranteed stable, and `items`
        # is already order'd, so this is exactly "by section rank, then by
        # existing order" without needing a second explicit sort key).
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
        ('onsite', '0024_alter_visit_paid_amount'),
    ]

    operations = [
        migrations.RunPython(fix_order, migrations.RunPython.noop),
    ]
