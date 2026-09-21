"""Bookings that a calendar poll created (from_feed) are, by definition, on the
calendar; mark them so the switch to "calendars are the source of truth for
cleanings" doesn't hide them from the board until the next poll re-marks them.
Bookings a CSV report created are marked by the next calendar poll, when it
matches them to an event by listing and dates."""
from django.db import migrations
from django.utils import timezone


def mark(apps, schema_editor):
    Booking = apps.get_model('onsite', 'Booking')
    Booking.objects.filter(from_feed=True, status='active', check_out__gte=timezone.now()).update(
        on_calendar=True, calendar_seen_at=timezone.now(),
    )


class Migration(migrations.Migration):
    dependencies = [('onsite', '0045_booking_on_calendar')]
    operations = [migrations.RunPython(mark, migrations.RunPython.noop)]
