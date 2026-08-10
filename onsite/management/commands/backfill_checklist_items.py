"""One-off, idempotent, safe-to-re-run command to top up already-created
Visit checklists that were snapshotted before the standard reservoir grew
to its current size.

A Visit's checklist is frozen at creation and never re-synced as the
standard reservoir changes — see onsite/services/checklist.py's own
docstring: "the one place copying is correct... a submitted visit's
record must never change underneath it." That's exactly right for a
visit already in progress or already done. But a visit that hasn't
started yet and is just sitting on an outdated snapshot from before the
reservoir grew (e.g. turnover grew from ~15 items to 41 between two
commits early in this module's build) is stale, not "frozen for a good
reason" — nobody's mid-clean, nothing gets stranded.

Only touches visits that:
  - are still open (excludes cancelled/skipped/submitted/verified — no
    reason to touch history or something already wrapped up), and
  - have not started (started_at is null) — matches ONSITE_DESIGN.md's
    existing "one-off additions only before it starts" rule exactly; a
    visit already being worked never gets items added out from under the
    cleaner mid-clean.

For each such visit, re-resolves the checklist for (property, visit_type)
and adds any STANDARD-source items missing from the visit (matched by
text — VisitChecklistItem doesn't track a live FK back to
StandardChecklistItem, by design, since that's exactly the live-reference
the snapshot pattern exists to avoid). New items are marked
is_new_unreviewed=True, same as any standard item added after a property
was last reviewed, and are ordered to sort after everything already on
the visit (not interleaved at their "natural" position in the current,
larger reservoir — the exact ordering bug already documented in
_deep_clean_checklist_items's docstring, avoided here the same way).
Never removes or modifies an existing item; never touches
property-specific, deep-clean, or one-off items."""
from django.core.management.base import BaseCommand
from django.db import transaction

from ...models import Visit, VisitChecklistItem
from ...services.checklist import resolve_checklist


class Command(BaseCommand):
    help = (
        'Tops up not-yet-started open visits whose checklist snapshot predates growth in the '
        'standard reservoir. Safe to re-run (idempotent); use --dry-run to preview first.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would change without writing anything.',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        stale_statuses = [
            Visit.Status.CANCELLED, Visit.Status.SKIPPED, Visit.Status.SUBMITTED, Visit.Status.VERIFIED,
        ]
        candidates = (
            Visit.objects.exclude(status__in=stale_statuses)
            .filter(started_at__isnull=True)
            .select_related('property', 'visit_type')
            .prefetch_related('checklist_items')
            .order_by('pk')
        )

        visits_touched = 0
        items_added = 0
        for visit in candidates:
            existing_items = list(visit.checklist_items.all())
            existing_texts = {i.text for i in existing_items}
            resolved = resolve_checklist(visit.property, visit.visit_type)
            missing = [
                row for row in resolved
                if row['source'] == VisitChecklistItem.Source.STANDARD and row['text'] not in existing_texts
            ]
            if not missing:
                continue

            visits_touched += 1
            items_added += len(missing)
            self.stdout.write(f'{visit.property.name} — {visit.visit_type} (visit #{visit.pk}): +{len(missing)} item(s)')
            for row in missing:
                self.stdout.write(f'    - {row["text"]}')

            if dry_run:
                continue

            # Appended after everything already on the visit, not at the
            # resolved row's own 'order' from the current (larger)
            # reservoir — otherwise the new items interleave into the
            # middle of the existing list instead of landing as one clean
            # block at the end. See _deep_clean_checklist_items's docstring
            # for the same bug, caught there by a screenshot.
            offset = max((i.order for i in existing_items), default=-1) + 1
            missing.sort(key=lambda r: r['order'])
            with transaction.atomic():
                VisitChecklistItem.objects.bulk_create([
                    VisitChecklistItem(
                        visit=visit, source=row['source'], section=row['section'], order=offset + i,
                        text=row['text'], mandatory=row['mandatory'], requires_photo=row['requires_photo'],
                        requires_note=row['requires_note'], is_new_unreviewed=True,
                    )
                    for i, row in enumerate(missing)
                ])

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f'DRY RUN: would add {items_added} item(s) across {visits_touched} visit(s). Nothing written.'
            ))
        elif items_added:
            self.stdout.write(self.style.SUCCESS(f'Added {items_added} item(s) across {visits_touched} visit(s).'))
        else:
            self.stdout.write('No stale checklists found — nothing to do.')
