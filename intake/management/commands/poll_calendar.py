import logging

from django.core.management.base import BaseCommand

from intake.adapters.calendar import GoogleCalendarAdapter
from intake.classifier import process_event

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        'Poll the shared Google Calendar for new reactive tasks '
        '(no-op until GOOGLE_CALENDAR_CREDENTIALS_PATH is configured).'
    )

    def handle(self, *args, **options):
        events = GoogleCalendarAdapter().pull()
        processed = 0
        for event in events:
            try:
                process_event(event)
                processed += 1
            except Exception:
                logger.exception('Calendar: failed to process event %s', event.external_id)
        self.stdout.write(self.style.SUCCESS(f'Processed {processed}/{len(events)} event(s).'))
