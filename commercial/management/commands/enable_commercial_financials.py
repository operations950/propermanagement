"""Switch on "We do the financials" for every commercial property (or the one named) and give each its chart of accounts. Safe to run again.

  python manage.py enable_commercial_financials
  python manage.py enable_commercial_financials --property "Del Park LLC"
"""
from django.core.management.base import BaseCommand

from accounting import ledger
from core.models import Property


class Command(BaseCommand):
    help = 'Turn the books on for the commercial properties and make their charts of accounts.'

    def add_arguments(self, parser):
        parser.add_argument('--property', help='Only this property (by name).')

    def handle(self, *args, **options):
        qs = Property.objects.filter(property_type=Property.Type.COMMERCIAL, is_active=True).order_by('name')
        if options.get('property'):
            qs = qs.filter(name__iexact=options['property'])
        for prop in qs:
            profile = ledger.profile_for(prop)
            changed = not profile.we_do_financials
            if changed:
                profile.we_do_financials = True
                profile.save(update_fields=['we_do_financials'])
            added = ledger.ensure_chart(prop)
            self.stdout.write(f'{prop.name}: books {"switched on" if changed else "already on"}, {added} account{"s" if added != 1 else ""} added')
        if not qs.exists():
            self.stdout.write('No commercial property found.')
