"""READ-ONLY report (changes nothing): on-site visits, recurring visit rules
and booking calendars that point at a *general placeholder* property
("Short-Term Rentals (general)", "No specific property", ...).

Going forward such a placeholder can't get visits at all — they can't be
scheduled by hand, generated from a rule, or created from a booking import
— because it stands for "properties we don't schedule on-site visits for",
not for a place anyone can go. Anything created BEFORE that rule may still
exist; this prints what, to the deploy log, so it can be dealt with on
purpose rather than found later. Same shape as the ticket-creator report."""
from django.db import migrations


def report(apps, schema_editor):
    Visit = apps.get_model('onsite', 'Visit')
    VisitRule = apps.get_model('onsite', 'VisitRule')
    Booking = apps.get_model('onsite', 'Booking')
    Property = apps.get_model('core', 'Property')

    general = list(Property.objects.filter(is_general=True).values_list('pk', 'name'))
    if not general:
        print('General-property report: no general placeholder properties exist.')
        return
    ids = [pk for pk, _ in general]
    print('General-property report (read-only; nothing was changed):')
    for pk, name in general:
        visits = Visit.objects.filter(property_id=pk)
        by_status = {}
        for status in visits.values_list('status', flat=True):
            by_status[status] = by_status.get(status, 0) + 1
        rules = VisitRule.objects.filter(property_id=pk)
        bookings = Booking.objects.filter(property_id=pk)
        print(
            f'  {name!r}: {visits.count()} visit(s) {dict(sorted(by_status.items())) or ""}, '
            f'{rules.filter(is_active=True).count()} active recurring rule(s) ({rules.count()} total), '
            f'{bookings.count()} booking(s)'
        )
    print(f'  Totals across {len(ids)} placeholder(s): {Visit.objects.filter(property_id__in=ids).count()} visit(s), '
          f'{VisitRule.objects.filter(property_id__in=ids, is_active=True).count()} active rule(s). '
          'Active rules on a placeholder are now skipped by the generator.')


class Migration(migrations.Migration):

    dependencies = [
        ('onsite', '0033_bookingfeed_not_listed'),
    ]

    operations = [
        migrations.RunPython(report, migrations.RunPython.noop),
    ]
