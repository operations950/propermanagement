import logging

from django.core.management.base import BaseCommand

from intake.adapters.vrbo import VrboAdapter
from intake.classifier import process_event

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Poll VRBO for bookings/cancellations/messages (no-op until VRBO_API_KEY is configured).'

    def handle(self, *args, **options):
        events = VrboAdapter().pull()
        processed = 0
        for event in events:
            try:
                process_event(event)
                processed += 1
            except Exception:
                logger.exception('VRBO: failed to process event %s', event.external_id)
        self.stdout.write(self.style.SUCCESS(f'Processed {processed}/{len(events)} event(s).'))
