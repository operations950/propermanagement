# One-time data migration — the bug this fixes: "Airbnb - Upcoming Page 1"
# and "Airbnb - Upcoming Page 2" are each only PART of Airbnb's full
# upcoming-reservations list (split across pages), not a comprehensive
# listing on their own. diff_bookings' absence-based cancellation inference
# assumes a comprehensive file — so uploading Page 2 was wrongly cancelling
# every booking that only appears on Page 1 (and vice versa), since neither
# page alone contains the other's rows. "Airbnb Cancellations" was already
# marked (then called is_cancellations_only) for the same underlying reason
# — it's this same "partial listing" bug, just previously only recognized
# for the Cancellations slot specifically.
from django.db import migrations

PAGE_LABELS = ['Airbnb - Upcoming Page 1', 'Airbnb - Upcoming Page 2']


def mark_pages_partial(apps, schema_editor):
    DailyUploadSlot = apps.get_model('onsite', 'DailyUploadSlot')
    DailyUploadSlot.objects.filter(label__in=PAGE_LABELS).update(is_partial_listing=True)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('onsite', '0010_rename_is_cancellations_only_to_is_partial_listing'),
    ]

    operations = [
        migrations.RunPython(mark_pages_partial, noop_reverse),
    ]
