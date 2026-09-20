"""Adds VisitRule.next_due — the explicit date of a rule's next visit (see
onsite/services/recurring.py) — and fills it in for rules that already exist.

A rule that has generated before keeps exactly the schedule it had:
next_due = last_generated_at + one interval (the same date the old
generator would have acted on). A rule that has NEVER generated has no start
date to carry over (the old behavior was "generate on the next run", which
gave nobody a say in the date), so it's left blank: it creates nothing until
someone picks a start date on the Recurring visits screen, where it's
flagged. The names of any such rules are printed to the deploy log."""
from datetime import timedelta

from dateutil.relativedelta import relativedelta
from django.db import migrations, models


def backfill_next_due(apps, schema_editor):
    VisitRule = apps.get_model('onsite', 'VisitRule')
    filled, waiting = 0, []
    for rule in VisitRule.objects.select_related('property', 'unit', 'visit_type'):
        if rule.last_generated_at:
            if rule.interval_days:
                rule.next_due = rule.last_generated_at + timedelta(days=rule.interval_days)
            else:
                rule.next_due = rule.last_generated_at + relativedelta(months=rule.interval_months)
            rule.save(update_fields=['next_due'])
            filled += 1
        else:
            target = rule.property.name + (f' — {rule.unit.label}' if rule.unit_id else '')
            waiting.append(f'{target} ({rule.visit_type.name})')
    print(f'Recurring visit rules: {filled} kept their existing schedule (next_due set).')
    if waiting:
        print(f'  {len(waiting)} rule(s) have no start date yet and will create nothing until one is set: ' + '; '.join(waiting))


class Migration(migrations.Migration):

    dependencies = [
        ('onsite', '0034_report_onsite_on_general_properties'),
    ]

    operations = [
        migrations.AddField(
            model_name='visitrule',
            name='next_due',
            field=models.DateField(blank=True, help_text='The date of the next visit this rule will create — chosen when the rule is added and editable any time. Blank means no start date has been set, and the rule creates nothing until one is. See onsite/services/recurring.py.', null=True),
        ),
        migrations.AlterField(
            model_name='visitrule',
            name='last_generated_at',
            field=models.DateField(blank=True, help_text='The date of the most recent visit this rule created (informational).', null=True),
        ),
        migrations.RunPython(backfill_next_due, migrations.RunPython.noop),
    ]
