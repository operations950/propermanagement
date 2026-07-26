"""Read-only audit for the "Airbnb emails created bogus cleaning tickets"
bug: re-runs the (now Subject-aware, see intake/booking_classifier.py)
extraction against every existing airbnb-source cleaning ticket's stored
raw_context, and reports which ones Claude would now flag as NOT a real
booking confirmation.

Deliberately does not cancel anything automatically — unlike
backfill_dedupe_thread_tickets (which only merged duplicates of the exact
same underlying thing), a wrong auto-cancel here could stand down a real
cleaning job. Also note the retroactive check runs without the Subject
line (raw_context only stores the message transcript, not headers), so
it's less precise than the fixed live pipeline going forward — treat
flagged tickets as "worth a human look," not a final verdict."""
from django.core.management.base import BaseCommand

from intake.booking_classifier import extract_airbnb_booking
from tickets.models import Ticket


class Command(BaseCommand):
    help = 'Reports existing Airbnb cleaning tickets Claude would now classify as not a real booking confirmation.'

    def handle(self, *args, **options):
        tickets = Ticket.objects.filter(
            source='airbnb', kind='cleaning', raw_context__gt='',
        ).exclude(status=Ticket.Status.CANCELLED).order_by('created_at')

        flagged = 0
        for ticket in tickets:
            extract = extract_airbnb_booking(ticket.raw_context)
            if extract is None:
                self.stdout.write(f'  ? #{ticket.pk} "{ticket.title}" — extraction failed/unconfigured, skipped.')
                continue
            if not extract.is_booking_confirmation:
                flagged += 1
                self.stdout.write(self.style.WARNING(
                    f'  ~ #{ticket.pk} "{ticket.title}" (status={ticket.status}) — '
                    f'Claude now reads this as NOT a new booking confirmation.'
                ))

        self.stdout.write(self.style.SUCCESS(
            f'Checked {tickets.count()} ticket(s), flagged {flagged} for review. Nothing was changed.'
        ))
