"""One-off: re-classify existing Quo-sourced tickets that predate the
concise-title fields on the classifier's output. Recovers the original
transcript from the old description format (which jammed the full
transcript + reasoning together before `raw_context` existed), re-runs
classification for a short title/summary, and moves the transcript into
raw_context. Only touches title/description/raw_context — leaves
property/role/priority/status alone in case staff already corrected those
by hand.
"""
import logging

from django.core.management.base import BaseCommand

from tickets.models import Ticket

logger = logging.getLogger(__name__)

OLD_MARKER = '\n\n--- Claude verdict ---\n'


class Command(BaseCommand):
    help = 'Re-classify existing Quo tickets to backfill short titles/summaries.'

    def handle(self, *args, **options):
        from intake.thread_classifier import classify_thread

        tickets = Ticket.objects.filter(source='quo', raw_context='')
        updated = 0
        skipped = 0

        for ticket in tickets:
            if OLD_MARKER in ticket.description:
                transcript = ticket.description.split(OLD_MARKER)[0]
            else:
                # Already-short description from a source without the old
                # marker — nothing to recover, treat description itself as
                # the closest thing to a transcript.
                transcript = ticket.description

            if not transcript.strip():
                skipped += 1
                continue

            verdict = classify_thread(transcript)
            if verdict is None:
                self.stdout.write(self.style.WARNING(f'Skipped ticket {ticket.pk}: classification unavailable.'))
                skipped += 1
                continue

            ticket.title = verdict.title
            ticket.description = verdict.summary
            ticket.raw_context = transcript
            ticket.save(update_fields=['title', 'description', 'raw_context'])
            self.stdout.write(f'{ticket.pk}: {ticket.title}')
            updated += 1

        self.stdout.write(self.style.SUCCESS(f'Updated {updated} ticket(s), skipped {skipped}.'))
