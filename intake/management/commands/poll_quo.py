import logging

from django.core.management.base import BaseCommand

from intake.adapters.quo import QuoAdapter
from intake.classifier import process_event

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        'Poll the shared Quo phone line for conversation threads with new activity, classify each '
        'whole thread with Claude, and create tickets/supply requests for actionable ones. '
        'No-op until QUO_API_KEY is configured; thread classification itself no-ops until '
        'ANTHROPIC_API_KEY is also set.'
    )

    def handle(self, *args, **options):
        events = QuoAdapter().pull()
        processed = 0
        for event in events:
            try:
                process_event(event)
                processed += 1
            except Exception:
                # Don't let one bad event (e.g. a transient SQLite lock
                # under concurrent scheduler activity) abort the whole
                # batch — the classifier's retry-bucket logic already
                # ensures a thread that fails here gets offered again next
                # poll, so it's safe to just log and move on.
                logger.exception('Quo: failed to process event for %s', event.external_id)
        self.stdout.write(self.style.SUCCESS(f'Processed {processed}/{len(events)} event(s).'))
