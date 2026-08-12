"""One-time cleanup command: deletes every unfinished (non-Submitted,
non-Verified) onsite.Visit — everything that never became a real,
signed-off cleaning record. Submitted/Verified visits (with their
signatures, photos, submitted checklists) are the actual audit trail of
completed work and are never touched by this command.

Deleting a Visit cascades to its own VisitChecklistItem, VisitMedia, and
VisitIssue rows (all belong to the visit) and — confirmed by a full-
codebase FK sweep — to supplies.SupplyReading rows taken during that
visit. Nothing else references Visit. Booking history is completely
unaffected: Visit points at Booking, not the reverse, so deleting visits
never touches Booking rows. A Ticket created from a VisitIssue survives
untouched (VisitIssue.created_ticket is SET_NULL, not a real ownership
link) — only the now-stale link back to the deleted visit/issue is
cleared.

Does NOT touch any pushed Google Calendar event for a deleted visit —
that's a separate external side effect this command doesn't know how to
undo; expect stale calendar entries for anything that had
google_event_id set.

Backs up every deleted Visit + its cascaded children to a timestamped
JSON file first (see BACKUP_DIR) — restorable with Django's loaddata.
Dry-run by default; --apply is required to actually delete."""
from pathlib import Path

from django.conf import settings
from django.core import serializers
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from supplies.models import SupplyReading

from ...models import Visit, VisitChecklistItem, VisitIssue, VisitMedia

BACKUP_DIR = Path(settings.BASE_DIR) / 'backups'

KEEP_STATUSES = [Visit.Status.SUBMITTED, Visit.Status.VERIFIED]


class Command(BaseCommand):
    help = (
        'Deletes every onsite.Visit that is not Submitted or Verified (i.e. never became a real '
        'completed cleaning record), along with its checklist items, media, issues, and supply '
        'readings. Backs up everything to a JSON file first. Dry-run by default — pass --apply to '
        'actually delete.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='Actually delete (and back up) — without this, only reports what would be deleted.',
        )

    def handle(self, *args, **options):
        doomed = Visit.objects.exclude(status__in=KEEP_STATUSES).select_related('property', 'visit_type')
        kept_count = Visit.objects.filter(status__in=KEEP_STATUSES).count()
        count = doomed.count()

        if count == 0:
            self.stdout.write(self.style.SUCCESS('Nothing to wipe — no unfinished visits found.'))
            return

        by_status = {}
        for v in doomed:
            by_status[v.status] = by_status.get(v.status, 0) + 1

        checklist_items = VisitChecklistItem.objects.filter(visit__in=doomed)
        media = VisitMedia.objects.filter(visit__in=doomed)
        issues = VisitIssue.objects.filter(visit__in=doomed)
        readings = SupplyReading.objects.filter(visit__in=doomed)

        self.stdout.write(f'Visits to delete: {count}')
        for status, n in sorted(by_status.items()):
            self.stdout.write(f'  {status}: {n}')
        self.stdout.write(
            f'  -> cascades: {checklist_items.count()} checklist item(s), {media.count()} media row(s), '
            f'{issues.count()} issue(s), {readings.count()} supply reading(s)',
        )
        self.stdout.write(f'Visits KEPT (Submitted/Verified, never touched): {kept_count}')

        if not options['apply']:
            self.stdout.write(self.style.WARNING(
                f'Dry run — {count} visit(s) would be deleted. Nothing deleted, no backup written. '
                'Pass --apply to actually delete.',
            ))
            return

        BACKUP_DIR.mkdir(exist_ok=True)
        backup_path = BACKUP_DIR / f'wipe_unfinished_visits_{timezone.now():%Y%m%d_%H%M%S}.json'
        backup_rows = list(doomed) + list(checklist_items) + list(media) + list(issues) + list(readings)
        backup_path.write_text(serializers.serialize('json', backup_rows, indent=2))
        self.stdout.write(f'Backed up {len(backup_rows)} row(s) to {backup_path}')

        with transaction.atomic():
            doomed_ids = list(doomed.values_list('pk', flat=True))
            Visit.objects.filter(pk__in=doomed_ids).delete()

        self.stdout.write(self.style.SUCCESS(
            f'Deleted {count} unfinished visit(s) and their cascaded rows. '
            f'{kept_count} Submitted/Verified visit(s) preserved as history.',
        ))
