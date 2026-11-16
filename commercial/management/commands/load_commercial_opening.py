"""Load a commercial property's opening balances from the Yardi trial balance (accrual, and cash if you have it).

  python manage.py load_commercial_opening --property "Del Park LLC" --as-of 2026-09-30 --accrual "TB accrual.xlsx" [--cash "TB cash.xlsx"]          (a dry run: shows what would load)
  python manage.py load_commercial_opening ... --apply                                                                                            (loads it)

Nothing is written without --apply, and nothing loads unless the file adds up and the property's books are still empty.
"""
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError

from commercial import opening
from core.models import Property


class Command(BaseCommand):
    help = 'Load opening balances for a commercial property from a Yardi trial balance.'

    def add_arguments(self, parser):
        parser.add_argument('--property', required=True)
        parser.add_argument('--as-of', required=True, help='YYYY-MM-DD: the day the balances are as of; the books begin the next day.')
        parser.add_argument('--accrual', required=True, help='The accrual trial balance (xlsx or csv).')
        parser.add_argument('--cash', help='The cash trial balance, if you have it (else the accrual figures are used for both books).')
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **o):
        prop = Property.objects.filter(property_type=Property.Type.COMMERCIAL, name__iexact=o['property']).first()
        if prop is None:
            raise CommandError(f'No commercial property is called "{o["property"]}".')
        as_of = datetime.strptime(o['as_of'], '%Y-%m-%d').date()
        try:
            accrual = opening.parse_trial_balance(open(o['accrual'], 'rb').read(), o['accrual'])
            cash = opening.parse_trial_balance(open(o['cash'], 'rb').read(), o['cash']) if o.get('cash') else None
            self.stdout.write(f'{prop.name}: {len(accrual)} accounts with a balance in the accrual file' + (f', {len(cash)} in the cash file' if cash else '') + f', as of {as_of}.')
            if not o['apply']:
                self.stdout.write('Dry run: nothing was loaded. Add --apply to load it.')
                return
            result = opening.load_opening(prop, accrual, cash, as_of)
        except opening.OpeningError as e:
            raise CommandError(str(e))
        self.stdout.write(f'Loaded: {result["entries"]} opening entr{"y" if result["entries"] == 1 else "ies"}, {result["accounts_made"]} accounts added to the chart.')
