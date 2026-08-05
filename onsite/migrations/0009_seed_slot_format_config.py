# One-time data migration — populates DailyUploadSlot.required_columns and
# is_cancellations_only with the REAL values confirmed against actual
# Airbnb/VRBO export files the user provided (not guesses). Matches slots by
# label, same as seed_daily_upload_slots. Safe to run once; if staff later
# tweak these from Admin, re-running this migration (it won't run again on
# its own, but if it ever did) would clobber that — that's fine, this is a
# genuine one-shot seed, same pattern as 0006_clean_slate_reset.
from django.db import migrations

AIRBNB_COLUMNS = 'Confirmation code, Status, Start date, End date, Listing'
VRBO_COLUMNS = 'Reservation ID, Status, Check-in date, Check-out date, Property name'

# label -> (required_columns, is_cancellations_only)
SLOT_CONFIG = {
    'Airbnb - Upcoming Page 1': (AIRBNB_COLUMNS, False),
    'Airbnb - Upcoming Page 2': (AIRBNB_COLUMNS, False),
    # The only one of the 5 that's a PARTIAL file — every row in it is
    # already cancelled, unlike the other 4 which are full listings. See
    # ImportBatch.is_cancellations_only's help text for why that matters.
    'Airbnb Cancellations': (AIRBNB_COLUMNS, True),
    'VRBO - Proper Realty': (VRBO_COLUMNS, False),
    'VRBO - Patrick': (VRBO_COLUMNS, False),
}


def seed_slot_config(apps, schema_editor):
    DailyUploadSlot = apps.get_model('onsite', 'DailyUploadSlot')
    for label, (required_columns, is_cancellations_only) in SLOT_CONFIG.items():
        DailyUploadSlot.objects.filter(label=label).update(
            required_columns=required_columns, is_cancellations_only=is_cancellations_only,
        )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('onsite', '0008_dailyuploadslot_is_cancellations_only_and_more'),
    ]

    operations = [
        migrations.RunPython(seed_slot_config, noop_reverse),
    ]
