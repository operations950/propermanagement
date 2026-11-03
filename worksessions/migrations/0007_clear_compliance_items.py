"""One-time clean-out the owner asked for: every compliance item that has been opened so far is deleted (with its lines and
the process runs that hung off those lines), and the rule "Association Monthly Charges to Owners" is retired (the books now
post owner charges automatically). Tickets that were promoted from a line are left alone (the link just clears).

Runs once, as a migration, so it can never delete items that are opened after this deploy."""
from django.db import migrations


def clear_items(apps, schema_editor):
    Session = apps.get_model('worksessions', 'Session')
    SessionTemplate = apps.get_model('worksessions', 'SessionTemplate')
    Session.objects.all().delete()
    SessionTemplate.objects.filter(name__icontains='Monthly Charges to Owners').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('worksessions', '0006_frequency_weekdays'),
        ('processes', '0008_processtemplatelink_and_more'),
    ]

    operations = [
        migrations.RunPython(clear_items, migrations.RunPython.noop),
    ]
