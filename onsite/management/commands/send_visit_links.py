"""Sends the cleaner's link for every assigned visit that has come due — the
day before the visit from ONSITE_LINK_SEND_HOUR on, or straight away for one
dated today. Run by the scheduler every ONSITE_LINK_SEND_INTERVAL_MINUTES;
anything assigned when it's already due was sent at assignment time (see
onsite/services/notify.py::dispatch_link). Safe to run any time: a visit
that's been sent for its current assignee and date is skipped."""
from django.core.management.base import BaseCommand

from onsite.services.notify import send_due_visit_links


class Command(BaseCommand):
    help = "Sends cleaners the link for on-site visits that are due (the day before, or today)."

    def handle(self, *args, **options):
        counts = send_due_visit_links()
        if counts['sent'] or counts['failed']:
            self.stdout.write(f"Visit links: {counts['sent']} sent, {counts['failed']} failed (will retry).")
        else:
            self.stdout.write('No visit links due.')
