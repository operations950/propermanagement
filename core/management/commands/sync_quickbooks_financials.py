"""Daily refresh of the QuickBooks YTD Profit & Loss snapshot the Owner
Dashboard's Company Financials box reads (see core/quickbooks.py). A
no-op if QuickBooks isn't connected — same degrade-gracefully shape as
every other optional integration in this app."""
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import QuickBooksToken
from core.quickbooks import fetch_profit_and_loss


class Command(BaseCommand):
    help = 'Refreshes the cached QuickBooks YTD financial snapshot.'

    def handle(self, *args, **options):
        token = QuickBooksToken.objects.first()
        if not token:
            self.stdout.write('No QuickBooks connection — skipping sync.')
            return

        result = fetch_profit_and_loss(token)
        if not result:
            self.stdout.write(self.style.WARNING('QuickBooks sync failed — keeping last known snapshot.'))
            return

        token.ytd_revenue = result['revenue']
        token.ytd_expenses = result['expenses']
        token.ytd_net_income = result['net_income']
        token.last_synced_at = timezone.now()
        token.save(update_fields=['ytd_revenue', 'ytd_expenses', 'ytd_net_income', 'last_synced_at'])
        self.stdout.write(self.style.SUCCESS('QuickBooks financial snapshot synced.'))
