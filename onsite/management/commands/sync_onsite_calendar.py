"""Retries the Google Calendar push for any Visit whose last attempt
failed (google_sync_pending=True) — see onsite/google_calendar_push.py.
No-ops cleanly if the calendar isn't configured or no staff member has a
connected Google account."""
from django.core.management.base import BaseCommand

from onsite.google_calendar_push import retry_pending


class Command(BaseCommand):
    help = 'Retries pending Google Calendar pushes for on-site visits.'

    def handle(self, *args, **options):
        retry_pending()
        self.stdout.write(self.style.SUCCESS('Onsite calendar sync retry complete.'))
