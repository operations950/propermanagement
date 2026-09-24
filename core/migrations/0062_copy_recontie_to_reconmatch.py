from django.db import migrations


def copy_ties(apps, schema_editor):
    ReconTie = apps.get_model('core', 'ReconTie')
    ReconMatch = apps.get_model('core', 'ReconMatch')
    ReconAcceptance = apps.get_model('core', 'ReconAcceptance')
    for tie in ReconTie.objects.all():
        ReconMatch.objects.create(
            property_id=tie.property_id, unit_id=tie.unit_id, month=tie.month, kind='user',
            lines=[tie.line_id], events=tie.events, note=tie.note, created_by_id=tie.tied_by_id,
        )
    ReconAcceptance.objects.filter(prior_period=True, reason='').update(reason='prior_books')


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0061_reconmatch_and_reason'),
    ]

    operations = [
        migrations.RunPython(copy_ties, migrations.RunPython.noop),
    ]
