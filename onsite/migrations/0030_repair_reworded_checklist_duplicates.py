"""Repairs a real, confirmed side effect of 0029's own backfill: a live
cleaner's checklist showed BOTH "Clean shower/tub, removing hair and soap
scum" AND "Clean shower/tub, removing hair, soap scum and used soap left
behind." on the same visit — reported directly with a screenshot, and
confirmed by the user that the second wording was a rewording of the
first, not a deliberate separate addition.

Root cause: 0029 added any active StandardChecklistItem missing (by exact
text) from an already-created, not-yet-started visit's checklist — meant
to backfill items that had been silently excluded by the amenity-gating
bug it was fixing. But "missing by exact text" also matches a visit that
was simply created BEFORE a since-edited wording change and never
resolved-and-reset since (a same-day-checkin rebuild, a re-import, etc.) —
0029 had no way to tell "this text was never resolved due to gating" apart
from "this text just changed since this visit was frozen," and for the
latter case it planted a second, duplicate row instead of recognizing the
old one as simply stale wording of the same task. This migration undoes
that specific side effect, generically, not just for the one reported
pair — any other visit 0029 touched the same way is repaired here too.

Scoped to every visit NOT YET cancelled/skipped/submitted/verified (the
reported one had already been started, so 0029's own "not yet started"
scope doesn't bound the repair — a duplicate planted on a visit that's
since been started is just as real). For each one, standard-sourced
checklist items are grouped by section; within a section, a "current"
item (text matches a live active StandardChecklistItem) and an "orphaned"
item (text matches none) are treated as the same duplicated task only
when their words overlap heavily (>=60% of the shorter text's word set) —
precise enough to catch "wording was extended/tightened" without
misfiring on two genuinely different tasks that happen to share a few
words. Whichever side actually has cleaner progress (completed, skipped,
noted, or has an attached photo) survives; the other is removed. If BOTH
sides have progress, neither is touched and the pair is printed for
manual review instead of guessing.

Idempotent: nothing left to repair on a second run finds no candidate
pairs at all."""
from django.db import migrations


def _has_progress(item):
    return bool(item.is_completed or item.note or item.skip_reason or item.media.exists())


def _word_overlap(text_a, text_b):
    words_a = set(text_a.lower().split())
    words_b = set(text_b.lower().split())
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / min(len(words_a), len(words_b))


def repair(apps, schema_editor):
    Visit = apps.get_model('onsite', 'Visit')
    VisitChecklistItem = apps.get_model('onsite', 'VisitChecklistItem')
    StandardChecklistItem = apps.get_model('onsite', 'StandardChecklistItem')

    visits = Visit.objects.exclude(status__in=['cancelled', 'skipped', 'submitted', 'verified'])
    removed = 0
    flagged = 0

    for visit in visits:
        items = list(VisitChecklistItem.objects.filter(visit=visit, source='standard').prefetch_related('media'))
        if len(items) < 2:
            continue
        current_texts = set(
            StandardChecklistItem.objects.filter(visit_type_id=visit.visit_type_id, is_active=True)
            .values_list('text', flat=True)
        )

        by_section = {}
        for item in items:
            by_section.setdefault(item.section, []).append(item)

        for section, section_items in by_section.items():
            current_in_section = [i for i in section_items if i.text in current_texts]
            orphaned_in_section = [i for i in section_items if i.text not in current_texts]
            if not current_in_section or not orphaned_in_section:
                continue
            matched_current_pks = set()
            for orphan in orphaned_in_section:
                match = next(
                    (c for c in current_in_section if c.pk not in matched_current_pks and _word_overlap(orphan.text, c.text) >= 0.6),
                    None,
                )
                if not match:
                    continue
                matched_current_pks.add(match.pk)

                orphan_progress = _has_progress(orphan)
                match_progress = _has_progress(match)
                if orphan_progress and match_progress:
                    print(
                        f'Visit #{visit.pk} ({visit.property_id}): BOTH "{orphan.text}" and "{match.text}" '
                        'have progress — left as-is, needs manual review.',
                    )
                    flagged += 1
                elif orphan_progress:
                    print(f'Visit #{visit.pk}: kept stale-worded "{orphan.text}" (has progress), removed duplicate "{match.text}".')
                    match.delete()
                    removed += 1
                else:
                    print(f'Visit #{visit.pk}: removed stale duplicate "{orphan.text}", kept current "{match.text}".')
                    orphan.delete()
                    removed += 1

    print(f'Checklist duplicate repair: {removed} item(s) removed, {flagged} pair(s) flagged for manual review.')


class Migration(migrations.Migration):

    dependencies = [
        ('onsite', '0029_backfill_ungated_checklist_items'),
    ]

    operations = [
        migrations.RunPython(repair, migrations.RunPython.noop),
    ]
