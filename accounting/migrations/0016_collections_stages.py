from django.db import migrations, models

# The old steps of a case against the new ones. A final notice is still before the lawyer, so it is a letter sent; "with the attorney" is the first lawyer step; a payment plan is
# not a step any more (nobody is on one in the screens), so a case that was on one goes back to Letter sent, where its 30-day alert will bring it up for a decision.
FORWARD = {'final': 'letter', 'attorney': 'notice1', 'plan': 'letter'}
BACKWARD = {'notice1': 'attorney', 'notice2': 'attorney', 'lien': 'attorney'}


def forwards(apps, schema_editor):
    Case = apps.get_model('accounting', 'DelinquencyCase')
    Event = apps.get_model('accounting', 'CaseEvent')
    for case in Case.objects.all():
        last_move = Event.objects.filter(case=case, kind__in=('stage', 'attorney', 'plan')).order_by('-date', '-pk').first()
        case.stage = FORWARD.get(case.stage, case.stage)
        case.stage_since = last_move.date if last_move else case.opened_on
        case.save(update_fields=['stage', 'stage_since'])


def backwards(apps, schema_editor):
    Case = apps.get_model('accounting', 'DelinquencyCase')
    for case in Case.objects.all():
        if case.stage in BACKWARD:
            case.stage = BACKWARD[case.stage]
            case.save(update_fields=['stage'])


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0015_budget_project'),
    ]

    operations = [
        migrations.AddField(
            model_name='delinquencycase',
            name='stage_since',
            field=models.DateField(blank=True, help_text='The day the owner entered the step they are on: the 30 and 45 day alerts count from here.', null=True),
        ),
        migrations.AddField(
            model_name='journalrule',
            name='auto_post',
            field=models.BooleanField(default=True, help_text="Post this entry by itself on its day (the association's own automatic posting must be on too)."),
        ),
        migrations.AlterField(
            model_name='delinquencycase',
            name='stage',
            field=models.CharField(choices=[('letter', 'Letter sent'), ('notice1', 'Lawyer: notice 1 (intent to lien)'), ('notice2', 'Lawyer: notice 2 (intent to foreclose)'), ('lien', 'Lien or foreclosure'), ('resolved', 'Resolved')], default='letter', max_length=10),
        ),
        migrations.RunPython(forwards, backwards),
    ]
