"""Refreshes the QuickBooks YTD Profit & Loss snapshot the Owner Dashboard's
Company Financials box reads (see core/quickbooks.py). Run daily and once at
startup by the scheduler, and once right after connecting. A no-op if
QuickBooks isn't connected — same degrade-gracefully shape as every other
optional integration in this app."""
from django.core.management.base import BaseCommand

from core import ledger
from core.models import QuickBooksToken
from core.quickbooks import sync_accounts, sync_snapshot


class Command(BaseCommand):
    help = 'Refreshes the cached QuickBooks YTD financial snapshot.'

    def handle(self, *args, **options):
        token = QuickBooksToken.objects.first()
        if not token:
            self.stdout.write('No QuickBooks connection — skipping sync.')
            return

        if sync_snapshot(token):
            self.stdout.write(self.style.SUCCESS('QuickBooks financial snapshot synced.'))
        else:
            self.stdout.write(self.style.WARNING(
                f'QuickBooks sync failed ({token.last_sync_error}) — keeping last known snapshot.'
            ))
        # The chart of accounts (what each property is tied to) rides along on the same schedule.
        count, error = sync_accounts(token)
        if error:
            self.stdout.write(self.style.WARNING(f'QuickBooks account list not refreshed ({error}).'))
        else:
            self.stdout.write(self.style.SUCCESS(f'QuickBooks chart of accounts synced ({count} accounts).'))
        # ... and each rental's transactions for the month-end close (closed months stay frozen).
        try:
            done, ledger_error = ledger.sync_all()
        except Exception:
            self.stdout.write(self.style.WARNING('QuickBooks transactions not refreshed (unexpected error).'))
        else:
            if ledger_error:
                self.stdout.write(self.style.WARNING(f'QuickBooks transactions partly refreshed: {ledger_error}'))
            else:
                self.stdout.write(self.style.SUCCESS(f'QuickBooks transactions synced for {done} rentals.'))
