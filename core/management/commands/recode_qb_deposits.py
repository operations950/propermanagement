"""Recodes the bank deposits sitting in "Uncategorized Airbnb Income" / "Uncategorized VRBO Income" from the uploaded payout
files (see core/qb_recode.py). Without --apply it only reports what it would do."""
from django.core.management.base import BaseCommand

from core import qb_recode
from core.models import QuickBooksToken


class Command(BaseCommand):
    help = 'Show (or with --apply, send) the QuickBooks recoding of Airbnb / VRBO deposits from the payout files.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Actually change the deposits in QuickBooks.')
        parser.add_argument('--days', type=int, default=qb_recode.LOOKBACK_DAYS, help='How far back to look for deposits.')

    def handle(self, *args, **options):
        token = QuickBooksToken.objects.first()
        if token is None:
            self.stdout.write('QuickBooks is not connected.')
            return
        result = qb_recode.plan(token, days=options['days'])
        for error in result['errors']:
            self.stdout.write(self.style.ERROR(error))
        for item in result['items']:
            detail = item['reason'] if item['reason'] else f'{len(item["lines"])} line(s)'
            self.stdout.write(f'{item["date"]} ${item["amount"]} {item["source"]}: {item["status"].upper()} - {detail}')
        if options['apply']:
            applied, failed = qb_recode.apply_ready(token, result, automatic=True)
            self.stdout.write(self.style.SUCCESS(f'{applied} deposit(s) recoded.'))
            for item, why in failed:
                self.stdout.write(self.style.ERROR(f'{item["date"]} ${item["amount"]}: {why}'))
        else:
            self.stdout.write('Nothing was changed (add --apply to send the ready ones).')
