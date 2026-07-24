from django.db import migrations


def backfill_target_type(apps, schema_editor):
    """Every existing TicketTemplate predates target_type, which defaulted
    to 'every_property' at the DB level when the column was added — this
    reclassifies each row so its stored target_type matches how
    template_applies_to_property actually already treats it (property set
    -> PROPERTY, else property_types set -> PROPERTY_CATEGORY, else
    EVERY_PROPERTY), so no existing rule's real-world behavior changes."""
    TicketTemplate = apps.get_model('tickets', 'TicketTemplate')
    for template in TicketTemplate.objects.all().iterator():
        if template.property_id:
            target_type = 'property'
        elif template.property_types:
            target_type = 'property_category'
        else:
            target_type = 'every_property'
        if template.target_type != target_type:
            template.target_type = target_type
            template.save(update_fields=['target_type'])


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0013_tickettemplate_target_type_and_contact'),
    ]

    operations = [
        migrations.RunPython(backfill_target_type, migrations.RunPython.noop),
    ]
