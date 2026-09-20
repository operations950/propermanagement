"""Re-syncs upcoming on-site visits to the CURRENT checklist.

A visit copies ("snapshots") its checklist when it's created, so one made
weeks ago — before the standard checklist was edited — still carries the old
list. This replaces that copy with the current one for every visit that
hasn't started (see onsite/services/checklist.py::refresh_checklist for the
exact rules: a visit with any progress, and anything started, finished or
cancelled, is left alone; hand-added one-off items are kept).

Dry run by default — it reports what WOULD change. Pass --apply to change
it. --type limits it to one visit type by slug (e.g. --type turnover).
The scheduler also runs it with --apply at startup and every few hours, and
a visit is refreshed again the instant its cleaner starts it, so in normal
running nothing needs doing by hand."""
from django.core.management.base import BaseCommand

from onsite.models import Visit
from onsite.services.checklist import REFRESHABLE_STATUSES, refresh_checklist


class Command(BaseCommand):
    help = 'Replaces the checklist on every not-yet-started visit with the current one. Dry run unless --apply.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Actually replace the checklists (default: report only).')
        parser.add_argument('--type', dest='visit_type', default='', help='Only visits of this visit type slug, e.g. turnover.')

    def handle(self, *args, **options):
        apply = options['apply']
        visits = Visit.objects.filter(
            status__in=REFRESHABLE_STATUSES, started_at__isnull=True,
        ).select_related('property', 'visit_type').order_by('scheduled_date', 'pk')
        if options['visit_type']:
            visits = visits.filter(visit_type__slug=options['visit_type'])

        tally, with_progress = {}, []
        for visit in visits:
            result = refresh_checklist(visit, apply=apply)
            tally[result] = tally.get(result, 0) + 1
            if result == 'skipped_progress':
                with_progress.append(f'{visit.property.name} — {visit.visit_type} ({visit.scheduled_date or "no date"}) [visit {visit.pk}]')

        changed = tally.get('refreshed', 0) + tally.get('would_refresh', 0)
        verb = 'replaced' if apply else 'would be replaced'
        self.stdout.write(
            f'Checked {sum(tally.values())} not-yet-started visit(s): {changed} checklist(s) {verb}, '
            f'{tally.get("unchanged", 0)} already current, {tally.get("skipped_progress", 0)} skipped (already has progress).'
        )
        for line in with_progress:
            self.stdout.write(f'  has progress, left as is: {line}')
        if not apply and changed:
            self.stdout.write(self.style.WARNING('Dry run — nothing changed. Re-run with --apply to replace them.'))
