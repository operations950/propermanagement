import logging

from django.core.management.base import BaseCommand

from intake.adapters.gmail import GmailAdapter
from intake.classifier import process_event

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        'Poll the connected shared Gmail inbox for email threads with new activity, classify each '
        'whole thread with Claude, and create tickets/supply requests for actionable ones. '
        'No-op until Gmail is connected (see /integrations/gmail/connect/, admin-only); thread '
        'classification itself no-ops until ANTHROPIC_API_KEY is also set.'
    )

    def handle(self, *args, **options):
        events = GmailAdapter().pull()
        processed = 0
        for event in events:
            try:
                process_event(event)
                processed += 1
            except Exception as exc:
                # Don't let one bad event abort the whole batch — see
                # poll_quo.py for the identical rationale (retry-bucket
                # logic in the classifier already covers re-offering it).
                # Exception type/message embedded directly in this line —
                # see poll_quo.py's matching change for why.
                logger.exception(
                    'Gmail: failed to process event for %s (%s: %s)', event.external_id, type(exc).__name__, exc,
                )
        self.stdout.write(self.style.SUCCESS(f'Processed {processed}/{len(events)} event(s).'))
