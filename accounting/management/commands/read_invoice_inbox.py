"""Reads new invoice emails from the shared mailbox into draft bills (nothing is approved or paid). Does nothing unless the inbox is switched on and connected."""
from django.core.management.base import BaseCommand

from accounting import invoice_inbox


class Command(BaseCommand):
    help = 'Read invoice PDFs from the Gmail label into draft bills.'

    def handle(self, *args, **options):
        made, note = invoice_inbox.run()
        self.stdout.write(note)
