"""The contact interaction ticker: when somebody last did something with a contact, and how many times (see core/contact_activity.py).

The backfill gives the ticker a head start from what the system already knows, one aggregate query per source, with the counts added up:
  * tickets assigned to the contact (when: the ticket's last update)
  * people attached to a ticket (TicketContact; when: the ticket's creation)
  * follow-ups (emails and texts) sent to the contact that went out (when: sent)
  * On-Site visits assigned to the contact (when: the visit's last update)
  * vendor bills entered for the contact that are not void (when: entered)
  * payments made to the contact that are not void (when: the payment's date)
  * a contact somebody typed in by hand (source "manual") counts once, when it was made; imported contacts (Yardi, Quo, Gmail, documents) do not, so an import does not flood "Recent".
Reversing only clears the two fields."""
from datetime import datetime, time

from django.db import migrations, models
from django.db.models import Count, Max
from django.utils import timezone


def backfill(apps, schema_editor):
    Contact = apps.get_model('core', 'Contact')
    seen = {}        # contact id -> [latest moment, count]

    def merge(rows):
        for contact_id, n, last in rows:
            if contact_id is None or last is None:
                continue
            if isinstance(last, datetime) is False:               # a plain date (a payment): noon of that day, in the site's time zone
                last = timezone.make_aware(datetime.combine(last, time(12, 0)))
            row = seen.setdefault(contact_id, [last, 0])
            row[0] = max(row[0], last)
            row[1] += n

    def rows(qs, id_field, when_field):
        return [(r[id_field], r['n'], r['last']) for r in qs.values(id_field).annotate(n=Count('pk'), last=Max(when_field))]

    try:
        Ticket = apps.get_model('tickets', 'Ticket')
        TicketContact = apps.get_model('tickets', 'TicketContact')
        FollowUpLog = apps.get_model('tickets', 'FollowUpLog')
        Visit = apps.get_model('onsite', 'Visit')
        Bill = apps.get_model('accounting', 'Bill')
        Payment = apps.get_model('accounting', 'Payment')
    except LookupError:
        return
    merge(rows(Ticket.objects.filter(assigned_contact__isnull=False), 'assigned_contact_id', 'updated_at'))
    merge(rows(TicketContact.objects.all(), 'contact_id', 'ticket__created_at'))
    merge(rows(FollowUpLog.objects.filter(contact__isnull=False, success=True), 'contact_id', 'sent_at'))
    merge(rows(Visit.objects.filter(assigned_contact__isnull=False), 'assigned_contact_id', 'updated_at'))
    merge(rows(Bill.objects.filter(vendor__isnull=False).exclude(status='void'), 'vendor_id', 'created_at'))
    merge(rows(Payment.objects.filter(vendor__isnull=False).exclude(status='void'), 'vendor_id', 'date'))
    merge([(c['pk'], 1, c['created_at']) for c in Contact.objects.filter(source='manual').values('pk', 'created_at')])
    if not seen:
        return
    batch = []
    for contact in Contact.objects.filter(pk__in=list(seen)).only('pk'):
        last, count = seen[contact.pk]
        contact.last_interaction_at, contact.interaction_count = last, count
        batch.append(contact)
    Contact.objects.bulk_update(batch, ['last_interaction_at', 'interaction_count'], batch_size=500)


def clear(apps, schema_editor):
    apps.get_model('core', 'Contact').objects.update(last_interaction_at=None, interaction_count=0)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0087_staff_theme'),
        ('tickets', '0045_remove_old_recurring'),
        ('onsite', '0055_calendar_truth'),
        ('accounting', '0036_document_jobs'),
    ]

    operations = [
        migrations.AddField(
            model_name='contact',
            name='last_interaction_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='contact',
            name='interaction_count',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.RunPython(backfill, clear),
    ]
