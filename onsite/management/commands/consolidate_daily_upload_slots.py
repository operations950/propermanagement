"""One-time transition off the old 5-report DailyUploadSlot set (Airbnb -
Upcoming Page 1/Page 2/Cancellations, VRBO - Proper Realty/Patrick) onto
the new 2-slot set (one per platform) — see seed_daily_upload_slots.py's
own docstring for why one box per platform is enough.

Deactivates the 5 known old slots by their exact label (is_active=False,
not deleted — keeps their last_uploaded_at/last_batch history intact,
same as every other soft-delete in this app) and ensures the 2 new ones
exist (get_or_create, same as seed_daily_upload_slots). Only ever touches
those 5 specific labels — any other slot an admin added by hand is left
alone. Dry-run by default; --apply required to actually change anything."""
from django.core.management.base import BaseCommand

from onsite.models import DailyUploadSlot

from .seed_daily_upload_slots import SLOTS as NEW_SLOTS

OLD_LABELS = [
    'Airbnb - Upcoming Page 1',
    'Airbnb - Upcoming Page 2',
    'Airbnb Cancellations',
    'VRBO - Proper Realty',
    'VRBO - Patrick',
]


class Command(BaseCommand):
    help = 'Retires the old 5 named daily-upload slots and ensures the new 2 (one per platform) exist.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Actually make the change (default: dry run).')

    def handle(self, *args, **options):
        old_slots = list(DailyUploadSlot.objects.filter(label__in=OLD_LABELS, is_active=True))
        new_existing = {s.label for s in DailyUploadSlot.objects.filter(label__in=[row[0] for row in NEW_SLOTS])}

        self.stdout.write(f'Old slots to retire ({len(old_slots)}):')
        for slot in old_slots:
            last = f', last used {slot.last_uploaded_at:%Y-%m-%d}' if slot.last_uploaded_at else ', never used'
            self.stdout.write(f'  {slot.label}{last}')

        self.stdout.write('\nNew slots:')
        for label, source, hint, required_columns in NEW_SLOTS:
            status = 'already exists' if label in new_existing else 'will be created'
            self.stdout.write(f'  {label} ({source}, requires "{required_columns}") — {status}')

        if not options['apply']:
            self.stdout.write(self.style.WARNING('\nDry run — pass --apply to actually make this change.'))
            return

        DailyUploadSlot.objects.filter(pk__in=[s.pk for s in old_slots]).update(is_active=False)
        for order, (label, source, hint, required_columns) in enumerate(NEW_SLOTS):
            DailyUploadSlot.objects.get_or_create(
                label=label,
                defaults={
                    'source': source, 'filename_hint': hint, 'order': order,
                    'required_columns': required_columns,
                },
            )

        self.stdout.write(self.style.SUCCESS(
            f'\nDone — retired {len(old_slots)} old slot(s), {len(NEW_SLOTS)} new platform slot(s) in place.',
        ))
