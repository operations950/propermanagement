"""Look every association up on SunBiz and keep its official record and alerts current (runs weekly from the scheduler).

  python manage.py check_sunbiz             every active association that has a state document number
"""
import time

from django.core.management.base import BaseCommand

from accounting import sunbiz
from accounting.models import AssociationProfile


class Command(BaseCommand):
    help = "Look each association up on SunBiz and update its official record and alerts."

    def handle(self, *args, **options):
        profiles = AssociationProfile.objects.filter(property__is_active=True).exclude(state_doc_number='').select_related('property').order_by('property__name')
        refused = 0
        for p in profiles:
            ok, message = sunbiz.check(p)
            self.stdout.write(f'{p.property.name}: {message}')
            refused += 0 if ok else 1
            time.sleep(2)     # be gentle with the state's site
        self.stdout.write(f'{profiles.count()} looked up, {refused} could not be checked.')
