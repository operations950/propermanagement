"""Posts the recurring charges and recurring bills that have come due, for each association we keep the books for that has automatic posting on."""
from django.core.management.base import BaseCommand
from django.utils import timezone

from accounting import charges, closing, contracts, delinquency, payables, scheduled


class Command(BaseCommand):
    help = 'Post recurring owner charges and recurring bills that are due.'

    def handle(self, *args, **options):
        today = timezone.localdate()
        alerts = contracts.ensure_alerts(today)       # contract alerts that are two weeks away go on the compliance screen
        if alerts:
            self.stdout.write(f'{alerts} contract alerts')
        n = delinquency.sync_reviews(today)          # a delinquency review ticked on the compliance screen moves its case on
        if n:
            self.stdout.write(f'{n} delinquency reviews done on the compliance screen')
        for profile in closing.associations():
            if not profile.auto_post:
                continue
            prop = profile.property
            done = charges.post_due(prop, today)
            made = payables.post_recurring(prop, today)
            entries = scheduled.post_due(prop, today, automatic=True)         # each recurring entry also has its own switch
            if prop.property_type == 'commercial':
                from commercial import rent
                rented = rent.post_rent_due(prop, today)               # rent and CAM from the leases, posted ahead of the 1st (default 10 days), and the tenants' invoices made
                if rented['posted'] or rented['invoices']:
                    self.stdout.write(f'{prop.name}: {rented["posted"]} rent charges, {rented["invoices"]} invoices, {rented["sent"]} sent')
            if done or made or entries:
                self.stdout.write(f'{prop.name}: {sum(n for _, _, n in done)} charges, {len(made)} bills, {len(entries)} scheduled entries')
