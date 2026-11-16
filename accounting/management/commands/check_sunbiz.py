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
        refused = looked = 0
        for p in profiles:
            ok, message = sunbiz.check(p)
            if message == sunbiz.REFUSED_NOTE:
                # SunBiz does not let a program look things up. Say it once, change nothing (a check a person made by hand stays), and stop asking.
                self.stdout.write(sunbiz.REFUSED_NOTE)
                refused = 1
                break
            self.stdout.write(f'{p.property.name}: {message}')
            looked += 1 if ok else 0
            time.sleep(2)     # be gentle with the state's site
        if not refused:
            self.stdout.write(f'{looked} of {profiles.count()} looked up.')
