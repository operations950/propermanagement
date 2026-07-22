import logging

from django.core.management.base import BaseCommand

from intake.adapters.fake import FakeAdapter
from intake.classifier import process_event

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Simulate inbound events from every reactive source (dev/demo only) and run them through the classifier.'

    def handle(self, *args, **options):
        events = FakeAdapter().pull()
        processed = 0
        for event in events:
            try:
                result = process_event(event)
                self.stdout.write(f'{event.event_type} {event.external_id}: {result}')
                processed += 1
            except Exception:
                logger.exception('Fake: failed to process event %s', event.external_id)
        self.stdout.write(self.style.SUCCESS(f'Processed {processed}/{len(events)} simulated event(s).'))
