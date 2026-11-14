from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0014_budget_rounding_meeting_amount'),
        ('legalforms', '0002_email_and_project_step'),
    ]

    operations = [
        migrations.CreateModel(
            name='BudgetProject',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('final_at', models.DateTimeField(blank=True, help_text='The Board draft was marked final for mailing.', null=True)),
                ('meeting_date', models.DateField(blank=True, null=True)),
                ('meeting_time', models.CharField(blank=True, max_length=40)),
                ('meeting_place', models.CharField(blank=True, max_length=200)),
                ('return_address', models.TextField(blank=True, help_text='Printed in the corner of every envelope. Blank: the association, care of the management company.')),
                ('envelope_size', models.CharField(choices=[('no10', 'Size 10 envelope'), ('full', 'Full page')], default='no10', max_length=6)),
                ('packet_total', models.DecimalField(blank=True, decimal_places=2, help_text='The assessments the budget needed when the notice was made, to tell if it changed since.', max_digits=14, null=True)),
                ('mailed_on', models.DateField(blank=True, null=True)),
                ('mailed_by', models.CharField(blank=True, max_length=120)),
                ('mail_method', models.CharField(blank=True, max_length=60)),
                ('mailed_count', models.PositiveIntegerField(blank=True, null=True)),
                ('mailing_snapshot', models.JSONField(blank=True, default=list, help_text='The mailing list exactly as it went out.')),
                ('held_on', models.DateField(blank=True, help_text='The day the meeting was held.', null=True)),
                ('loaded_at', models.DateTimeField(blank=True, null=True)),
                ('loaded_note', models.CharField(blank=True, max_length=300)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('affidavit_doc', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='legalforms.generateddocument')),
                ('budget', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='project', to='accounting.budget')),
                ('notice_doc', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='legalforms.generateddocument')),
            ],
        ),
    ]
