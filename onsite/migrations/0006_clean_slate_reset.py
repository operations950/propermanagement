# One-shot data migration — not a repeatable backfill/seed command, deliberately
# NOT chained into Procfile. Wipes every Visit (cascades to VisitChecklistItem/
# VisitMedia/VisitIssue), Booking, ImportBatch, and BookingFeedHealth row, since
# the booking-file format validation added right after this needed a genuinely
# clean slate to start enforcing against — the existing Visit/Booking data was
# built from imports that predate that check. Deliberately leaves VisitType/
# StandardChecklistItem/PropertyChecklistOverride/PropertyChecklistItem/
# DailyUploadSlot/VisitRule alone — that's configuration, not events, and
# clearing it would mean redoing setup for no reason. Runs once, here, then
# never again — re-running this migration on an already-empty table is a
# harmless no-op, same as any other migration.
from django.db import migrations


def wipe_onsite_events(apps, schema_editor):
    Visit = apps.get_model('onsite', 'Visit')
    Booking = apps.get_model('onsite', 'Booking')
    ImportBatch = apps.get_model('onsite', 'ImportBatch')
    BookingFeedHealth = apps.get_model('onsite', 'BookingFeedHealth')
    DailyUploadSlot = apps.get_model('onsite', 'DailyUploadSlot')

    Visit.objects.all().delete()
    Booking.objects.all().delete()
    ImportBatch.objects.all().delete()
    BookingFeedHealth.objects.all().delete()
    DailyUploadSlot.objects.update(last_uploaded_at=None, last_uploaded_by=None, last_batch=None)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('onsite', '0005_bookingfeedhealth'),
    ]

    operations = [
        migrations.RunPython(wipe_onsite_events, noop_reverse),
    ]
