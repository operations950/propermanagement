"""Polls every active BookingFeed (Airbnb/VRBO calendar links) and applies
what they show — see onsite/services/feeds.py. Run by the scheduler on a
timer (BOOKING_FEED_POLL_INTERVAL_MINUTES) and once at startup. A no-op
when no feeds are set up."""
from django.core.management.base import BaseCommand

from onsite.services.feeds import poll_all


class Command(BaseCommand):
    help = 'Polls the Airbnb/VRBO calendar links and applies reservation changes.'

    def handle(self, *args, **options):
        feeds = poll_all()
        if not feeds:
            self.stdout.write('No booking calendars set up — nothing to poll.')
            return
        for feed in feeds:
            line = f'{feed.get_source_display()} — {feed.label()}: {feed.last_error or feed.last_summary}'
            self.stdout.write(self.style.ERROR(line) if feed.last_error else line)
