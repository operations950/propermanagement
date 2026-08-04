"""Idempotent seed for the small, fixed set of reports staff pull every day
for the booking import screen — see DailyUploadSlot. Safe to run repeatedly:
get_or_create on label means adding a slot here later and re-running only
adds the new one, never duplicates existing ones. Wired into the Procfile
alongside this app's other idempotent seed commands.

Edit or add slots from Admin (DailyUploadSlot) without a deploy if the
daily routine changes — this list is only the starting set."""
from django.core.management.base import BaseCommand

from onsite.models import DailyUploadSlot, ImportBatch

SLOTS = [
    ('Airbnb - Upcoming Page 1', ImportBatch.Source.AIRBNB, 'page1'),
    ('Airbnb - Upcoming Page 2', ImportBatch.Source.AIRBNB, 'page2'),
    ('Airbnb Cancellations', ImportBatch.Source.AIRBNB, 'cancel'),
    ('VRBO - Proper Realty', ImportBatch.Source.VRBO, 'proper'),
    ('VRBO - Patrick', ImportBatch.Source.VRBO, 'patrick'),
]


class Command(BaseCommand):
    help = 'Seeds the fixed daily upload slots for the on-site booking import screen.'

    def handle(self, *args, **options):
        for order, (label, source, hint) in enumerate(SLOTS):
            slot, created = DailyUploadSlot.objects.get_or_create(
                label=label, defaults={'source': source, 'filename_hint': hint, 'order': order},
            )
            self.stdout.write(f'{"Created" if created else "Already exists"}: {slot.label}')
