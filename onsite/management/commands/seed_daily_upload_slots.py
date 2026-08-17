"""Idempotent seed for the small, fixed set of reports staff pull every day
for the booking import screen — see DailyUploadSlot. Safe to run repeatedly:
get_or_create on label means adding a slot here later and re-running only
adds the new one, never duplicates existing ones. Wired into the Procfile
alongside this app's other idempotent seed commands.

One slot per platform — not one per report. diff_bookings/apply_bookings_
for_property already reconcile by (source, external_uid) globally (the
reservation/confirmation code, not which page or export it came from), and
cancellation detection is explicit-status-based, not absence-based — so
nothing about correctness depends on staff keeping "Page 1"/"Page 2"/
"Cancellations"/whichever export separate. Drop every report of a given
platform onto that platform's one box, in whatever order; each upload
reconciles against whatever's already there. See
consolidate_daily_upload_slots for the one-time migration off the old
5-slot set this replaced.

Edit or add slots from Admin (DailyUploadSlot) without a deploy if the
daily routine changes — this list is only the starting set."""
from django.core.management.base import BaseCommand

from onsite.models import DailyUploadSlot, ImportBatch

SLOTS = [
    # required_columns picks the one header each platform always has and
    # the other never does — 'reservation id' is VRBO's own term, Airbnb
    # calls the same field 'confirmation code' (see importers.py's
    # _CSV_FIELD_ALIASES, where both already resolve to the same internal
    # external_uid field). That's what stops a file from silently
    # importing under the wrong platform if it's dropped on the wrong box.
    ('Airbnb', ImportBatch.Source.AIRBNB, 'airbnb', 'Confirmation Code'),
    ('VRBO', ImportBatch.Source.VRBO, 'vrbo', 'Reservation ID'),
]


class Command(BaseCommand):
    help = 'Seeds the fixed daily upload slots for the on-site booking import screen.'

    def handle(self, *args, **options):
        for order, (label, source, hint, required_columns) in enumerate(SLOTS):
            slot, created = DailyUploadSlot.objects.get_or_create(
                label=label,
                defaults={
                    'source': source, 'filename_hint': hint, 'order': order,
                    'required_columns': required_columns,
                },
            )
            self.stdout.write(f'{"Created" if created else "Already exists"}: {slot.label}')
