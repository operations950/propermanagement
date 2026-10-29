"""Take an association's books out of the system: the ledger, owner balances and charges, bank accounts and reconciliations, bills and payments, owners' ownership
records, unit shares and board terms. The property page, its units, the people, uploaded documents, the budget, the settings and the official record all stay.

  python manage.py wipe_association_books "St Andrews Glen"                       shows what would be removed, changes NOTHING
  python manage.py wipe_association_books "St Andrews Glen" --apply --confirm "St Andrews Glen"     does it (the name must be typed again, exactly)

To start an association over, prefer: python manage.py load_yardi_sep30 glen --wipe-first --apply  (it wipes and loads in one step).
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounting import yardi_load as Y
from core.models import Property


class Command(BaseCommand):
    help = "Take an association's books out of the system. Looks only, unless --apply and --confirm."

    def add_arguments(self, parser):
        parser.add_argument('name', help='the association exactly as it is named in the app')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--confirm', default='', help='type the association name again to go ahead')

    def handle(self, *args, **opts):
        props = list(Property.objects.filter(property_type=Property.Type.ASSOCIATION, name__iexact=opts['name']))
        if len(props) != 1:
            raise CommandError(f'Found {len(props)} associations named "{opts["name"]}". Type the name exactly as the app shows it.')
        prop = props[0]
        counts = Y.wipe_counts(prop)
        self.stdout.write(f'{prop.name}: this is what is in the books now')
        for label, n in counts:
            self.stdout.write(f'  {n:6} {label}')
        if not opts['apply']:
            self.stdout.write('Nothing was changed. To remove it: add --apply --confirm "' + prop.name + '"')
            return
        if opts['confirm'] != prop.name:
            raise CommandError(f'Nothing was changed. To go ahead, type the name again: --confirm "{prop.name}"')
        with transaction.atomic():
            Y.wipe(prop)
        self.stdout.write(self.style.SUCCESS(f'{prop.name}: the books are removed. The property, units, people, documents and settings are untouched.'))
