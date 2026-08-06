"""One-time content swap: removes the original placeholder turnover/deep-
clean StandardChecklistItem rows (seed_checklist_templates.py's very first
content, from before this app had any real usage) so they don't sit
alongside the new, comprehensive lists as duplicates. Matched by exact old
text, so this can never touch anything added later through the checklist
editor or by a re-run of the (now purely additive) seed command.

Safe regardless of whether these rows still have their original text (they
do in every environment that's only ever run the old seed content) or have
already been edited/deleted by hand — this is a plain delete-if-present,
not an assert. Deleting a StandardChecklistItem has no effect on any
already-created Visit (its checklist was already copied into its own
VisitChecklistItem rows at creation — see onsite/services/checklist.py);
it only cascades to PropertyChecklistOverride rows referencing these
specific old items, of which there were none as of this migration (the
per-property checklist review/override feature had no real usage yet)."""
from django.db import migrations

OLD_TURNOVER_TEXT = [
    'Strip and remake all beds',
    'Check under beds and in closets for guest items',
    'Clean toilets, showers, and sinks',
    'Restock toilet paper and toiletries',
    'Wash dishes and wipe down counters',
    'Empty and wipe refrigerator',
    'Take out trash and replace liners',
    'Vacuum/sweep and mop all floors',
    'Dust surfaces and wipe down furniture',
    'Check smoke/CO detectors are present',
    'Lock all doors and windows before leaving',
]

OLD_DEEP_CLEAN_TEXT = [
    'Clean inside oven and microwave',
    'Descale coffee maker/kettle',
    'Scrub grout and tile',
    'Rotate/flip mattresses',
    'Wash baseboards and door frames',
    'Clean interior windows',
    'Wipe down light fixtures and ceiling fans',
    'Launder all linens, towels, and throw blankets',
]


def remove_old_placeholder_items(apps, schema_editor):
    StandardChecklistItem = apps.get_model('onsite', 'StandardChecklistItem')
    VisitType = apps.get_model('onsite', 'VisitType')

    turnover = VisitType.objects.filter(slug='turnover').first()
    if turnover:
        StandardChecklistItem.objects.filter(visit_type=turnover, text__in=OLD_TURNOVER_TEXT).delete()

    deep_clean = VisitType.objects.filter(slug='deep-clean').first()
    if deep_clean:
        StandardChecklistItem.objects.filter(visit_type=deep_clean, text__in=OLD_DEEP_CLEAN_TEXT).delete()
        # deep-clean stops being a schedulable visit type as of this change
        # (its items now get layered onto a turnover via Visit.is_deep_clean
        # instead) — see onsite/services/checklist.py::set_deep_clean.
        deep_clean.is_addon = True
        deep_clean.default_duration_minutes = 0
        deep_clean.save(update_fields=['is_addon', 'default_duration_minutes'])


def noop_reverse(apps, schema_editor):
    # Deliberately not reversible — the old placeholder text is gone for
    # good (by design, see this migration's docstring); re-adding it would
    # just reintroduce the exact duplication problem this migration exists
    # to fix. A real rollback path is "run seed_checklist_templates" if the
    # new content also needs to go, not resurrecting the placeholders.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('onsite', '0014_visit_is_deep_clean_visittype_is_addon_and_more'),
    ]

    operations = [
        migrations.RunPython(remove_old_placeholder_items, noop_reverse),
    ]
