# Removes the old per-property recurring-ticket engine (TicketTemplate, task packages / Functions, task groups,
# occurrences, package runs). The worksessions app ("Compliance") replaced it. Tickets that came from it keep
# source='recurring' (now labelled "Earlier recurring task") and Ticket.scheduled_for as plain history.
#
# Hand-ordered: the auto-generated version removed each model's fields one by one, which breaks on SQLite
# (it rebuilds the table with a stale unique_together). Dropping the Ticket link first and then deleting each
# model whole, children before parents, is valid on SQLite and Postgres alike.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0044_remove_ticket_checklists'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='ticket',
            name='uniq_template_scheduled_for_property',
        ),
        migrations.RemoveField(
            model_name='ticket',
            name='created_from_template',
        ),
        migrations.RemoveField(
            model_name='ticket',
            name='template_occurrence',
        ),
        migrations.RemoveField(
            model_name='ticket',
            name='package_run',
        ),
        migrations.AlterField(
            model_name='ticket',
            name='source',
            field=models.CharField(choices=[('manual', 'Manual'), ('email', 'Email'), ('quo', 'Phone (Quo)'), ('calendar', 'Calendar'), ('airbnb', 'Airbnb'), ('vrbo', 'VRBO'), ('fake', 'Simulated (dev)'), ('recurring', 'Earlier recurring task'), ('onsite', 'On-site visit'), ('session', 'Compliance item')], default='manual', max_length=20),
        ),
        # children first
        migrations.DeleteModel(name='PropertyTemplateOverride'),
        migrations.DeleteModel(name='PropertyPackage'),
        migrations.DeleteModel(name='TaskPackageTemplate'),
        migrations.DeleteModel(name='TaskGroup'),
        migrations.DeleteModel(name='PackageRun'),
        migrations.DeleteModel(name='TemplateOccurrence'),
        migrations.DeleteModel(name='TicketTemplateDocument'),
        # parents last
        migrations.DeleteModel(name='TaskPackage'),
        migrations.DeleteModel(name='TicketTemplate'),
    ]
