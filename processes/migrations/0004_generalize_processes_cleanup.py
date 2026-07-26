from django.db import migrations, models


class Migration(migrations.Migration):
    """Split out of the old 0002 so these ALTER TABLE / ADD CONSTRAINT
    operations run in their own transaction, after 0003's data migration has
    fully committed — see 0003_migrate_process_data.py's docstring."""

    dependencies = [
        ('processes', '0003_migrate_process_data'),
    ]

    operations = [
        # --- Drop the now-migrated old columns/model ---
        migrations.RemoveField('processtemplatestep', 'action_type'),
        migrations.RemoveField('processtemplatestep', 'document_key'),
        migrations.RemoveField('processrunstep', 'action_type'),
        migrations.RemoveField('processrunstep', 'document_key'),
        migrations.RemoveField('processrunstep', 'meeting_datetime'),
        migrations.RemoveField('processrunstep', 'meeting_link'),
        migrations.RemoveField('processrunstep', 'meeting_dial_in'),
        migrations.RemoveField('processrunstep', 'calendar_event_id'),
        migrations.RemoveField('processrunstep', 'calendar_id'),
        migrations.DeleteModel('ProcessInstanceDocument'),

        # --- Constraints (added last, after step_key/ticket-or-property-or-contact data is in place) ---
        migrations.AddConstraint(
            model_name='processtemplatestep',
            constraint=models.UniqueConstraint(fields=['process_template', 'step_key'], name='uniq_template_step_key'),
        ),
        migrations.AddConstraint(
            model_name='processrun',
            constraint=models.CheckConstraint(
                condition=(
                    (models.Q(ticket__isnull=False) & models.Q(property__isnull=True) & models.Q(contact__isnull=True))
                    | (models.Q(ticket__isnull=True) & models.Q(property__isnull=False) & models.Q(contact__isnull=True))
                    | (models.Q(ticket__isnull=True) & models.Q(property__isnull=True) & models.Q(contact__isnull=False))
                ),
                name='processrun_exactly_one_of_ticket_property_contact',
            ),
        ),
    ]
