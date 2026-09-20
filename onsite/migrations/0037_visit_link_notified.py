"""Visit.link_notified_key/at — which cleaner has been sent the visit's link,
for which date (see onsite/services/notify.py). The links now go out the day
before a visit via a timer that sends everything not yet marked as sent, so
the moment this ships every EXISTING assigned visit would look "not yet
sent" and its cleaner would be texted again. To prevent that, existing
assigned visits are marked as already told — assignments made through the
visit screen always texted at the time.

The exception: upcoming visits a recurring rule created. Until recently
those never texted anyone, so their cleaners have not been sent a link and
are left unmarked — the timer sends theirs when their day comes."""
from django.db import migrations, models
from django.utils import timezone


def mark_existing_as_notified(apps, schema_editor):
    Visit = apps.get_model('onsite', 'Visit')
    today = timezone.localdate()
    now = timezone.now()
    marked = left = 0
    for visit in Visit.objects.filter(status__in=['scheduled', 'in_progress', 'submitted', 'verified']):
        if visit.assigned_staff_id:
            who = f's{visit.assigned_staff_id}'
        elif visit.assigned_contact_id:
            who = f'c{visit.assigned_contact_id}'
        else:
            continue
        if visit.created_from_rule_id and visit.scheduled_date and visit.scheduled_date >= today:
            left += 1
            continue
        visit.link_notified_key = f'{who}|{visit.scheduled_date.isoformat() if visit.scheduled_date else ""}'
        visit.link_notified_at = now
        visit.save(update_fields=['link_notified_key', 'link_notified_at'])
        marked += 1
    print(f'Visit links: {marked} existing assigned visit(s) marked as already sent; '
          f'{left} upcoming recurring visit(s) left to send on their day.')


class Migration(migrations.Migration):

    dependencies = [
        ('onsite', '0036_visittype_fixed_price'),
    ]

    operations = [
        migrations.AddField(
            model_name='visit',
            name='link_notified_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='visit',
            name='link_notified_key',
            field=models.CharField(blank=True, max_length=60),
        ),
        migrations.RunPython(mark_existing_as_notified, migrations.RunPython.noop),
    ]
