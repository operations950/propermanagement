import logging

from django.core.management.base import BaseCommand

from intake.adapters.airbnb import AirbnbAdapter
from intake.classifier import process_event

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Poll Airbnb for bookings/cancellations/messages (no-op until AIRBNB_API_KEY is configured).'

    def handle(self, *args, **options):
        events = AirbnbAdapter().pull()
        processed = 0
        for event in events:
            try:
                process_event(event)
                processed += 1
            except Exception:
                logger.exception('Airbnb: failed to process event %s', event.external_id)
        self.stdout.write(self.style.SUCCESS(f'Processed {processed}/{len(events)} event(s).'))
