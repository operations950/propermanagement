"""Idempotent decommission command for the old TicketTemplate-based
recurring system, replaced by the sessions app (see the "Recurring work
overhaul — sessions" build brief, Phase 6). Deletes every TicketTemplate,
PropertyTemplateOverride, and TaskPackage row (which cascades TaskGroup,
TaskPackageTemplate, PropertyPackage, TemplateOccurrence, TemplateChecklistItem,
TicketTemplateDocument along with them — all of it is rule configuration,
none of it is a record of work done), plus every source='recurring' Ticket
that never reached a real completion. Completed/verified recurring tickets
are kept untouched — they're the only history available for judging
whether the old rules were working, per the brief: "Do not delete them."

Backs up everything it's about to delete to a timestamped JSON file first
(see BACKUP_DIR) — restorable with Django's loaddata if anything here ever
needs to be recovered. Re-runnable: once the matching rows are gone, a
second run reports nothing left to do and exits cleanly.

Deliberately NOT chained into Procfile the way this app's other idempotent
commands are — a one-time decommission of real configured business rules
needs a human to review the counts and choose to run it, not to fire
automatically on the next deploy. Run with --dry-run first to see the
counts without deleting or writing a backup."""
import json
from pathlib import Path

from django.conf import settings
from django.core import serializers
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from ...models import PropertyTemplateOverride, TaskPackage, Ticket, TicketTemplate

BACKUP_DIR = Path(settings.BASE_DIR) / 'backups'


class Command(BaseCommand):
    help = (
        'Deletes the old recurring-ticket system (TicketTemplate, PropertyTemplateOverride, '
        'TaskPackage, and non-completed source=recurring Tickets), keeping completed/verified '
        'recurring tickets as history. Backs up everything it deletes to a JSON file first.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would be deleted without deleting anything or writing a backup file.',
        )

    def handle(self, *args, **options):
        templates = TicketTemplate.objects.all()
        overrides = PropertyTemplateOverride.objects.all()
        packages = TaskPackage.objects.all()
        doomed_tickets = Ticket.objects.filter(source=Ticket.Source.RECURRING).exclude(
            status__in=Ticket.TRUE_COMPLETION_STATUSES,
        )
        kept_tickets = Ticket.objects.filter(
            source=Ticket.Source.RECURRING, status__in=Ticket.TRUE_COMPLETION_STATUSES,
        )

        counts = {
            'TicketTemplate': templates.count(),
            'PropertyTemplateOverride': overrides.count(),
            'TaskPackage': packages.count(),
            'Ticket (recurring, not completed — will delete)': doomed_tickets.count(),
        }
        for label, n in counts.items():
            self.stdout.write(f'  {label}: {n}')
        self.stdout.write(f'  Ticket (recurring, completed/verified — KEPT, never deleted): {kept_tickets.count()}')

        if not any(counts.values()):
            self.stdout.write(self.style.SUCCESS('Nothing to wipe — already clean.'))
            return

        if options['dry_run']:
            self.stdout.write(self.style.WARNING('Dry run — nothing deleted, no backup written.'))
            return

        BACKUP_DIR.mkdir(exist_ok=True)
        backup_path = BACKUP_DIR / f'wipe_recurring_tickets_{timezone.now():%Y%m%d_%H%M%S}.json'
        backup_rows = list(templates) + list(overrides) + list(packages) + list(doomed_tickets)
        backup_path.write_text(serializers.serialize('json', backup_rows, indent=2))
        self.stdout.write(f'Backed up {len(backup_rows)} row(s) to {backup_path}')

        with transaction.atomic():
            doomed_count = doomed_tickets.count()
            doomed_tickets.delete()
            templates.delete()
            overrides.delete()
            packages.delete()

        self.stdout.write(self.style.SUCCESS(
            f'Wipe complete: deleted {counts["TicketTemplate"]} template(s), '
            f'{counts["PropertyTemplateOverride"]} override(s), {counts["TaskPackage"]} package(s), '
            f'{doomed_count} non-completed recurring ticket(s). '
            f'{kept_tickets.count()} completed recurring ticket(s) preserved.'
        ))
