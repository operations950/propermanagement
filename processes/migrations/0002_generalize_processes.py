import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('processes', '0001_initial'),
        ('core', '0026_contactdocument'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # --- Renames ---
        migrations.RenameModel('ProcessInstance', 'ProcessRun'),
        migrations.RenameModel('ProcessInstanceItem', 'ProcessRunStep'),
        migrations.RenameModel('ProcessTemplateItem', 'ProcessTemplateStep'),

        migrations.RenameField('ProcessTemplateStep', 'text', 'label'),
        migrations.RenameField('ProcessRunStep', 'text', 'label'),
        migrations.RenameField('ProcessRunStep', 'instance', 'run'),
        migrations.RenameField('ProcessRunStep', 'is_checked', 'is_complete'),
        migrations.RenameField('ProcessRunStep', 'checked_at', 'completed_at'),
        migrations.RenameField('ProcessRunStep', 'checked_by', 'completed_by'),
        migrations.RenameField('ProcessAttachment', 'instance_item', 'run_step'),
        migrations.RenameField('ProcessTemplateAttachment', 'template_item', 'template_step'),

        migrations.AlterModelOptions(name='processrun', options={'ordering': ['-created_at']}),
        migrations.AlterModelOptions(name='processtemplate', options={'ordering': ['category', 'name']}),

        # --- New columns (with safe defaults for existing rows) ---
        migrations.AddField('processtemplate', 'category', models.CharField(max_length=100, blank=True)),

        migrations.AddField('processtemplatestep', 'step_key', models.SlugField(max_length=60, blank=True)),
        migrations.AddField(
            'processtemplatestep', 'step_type',
            models.CharField(max_length=30, default='checkbox', choices=[
                ('checkbox', 'Checkbox'), ('checklist', 'Checklist'), ('short_text', 'Short text'),
                ('long_text', 'Long text / notes'), ('number_currency', 'Number / currency'),
                ('date_time', 'Date and time'), ('dropdown_multiselect', 'Dropdown / multi-select'),
                ('record_selector', 'Record selector'), ('document_upload', 'Document upload'),
                ('photo_video_upload', 'Photo / video upload'), ('digital_signature', 'Digital signature'),
                ('task_assignment', 'Task assignment'), ('approval_decision', 'Approval / decision'),
                ('email_text_action', 'Email / text action'), ('calendar_event', 'Calendar event'),
                ('calculation_formula', 'Calculation / formula'), ('wait_timer', 'Wait / timer'),
            ]),
        ),
        migrations.AddField('processtemplatestep', 'config', models.JSONField(default=dict, blank=True)),
        migrations.AddField(
            'processtemplatestep', 'assignee_role',
            models.CharField(max_length=20, blank=True, choices=[
                ('admin', 'Admin'), ('property_manager', 'Property Manager'), ('maintenance', 'Maintenance'),
                ('cleaner', 'Cleaner'), ('contractor', 'Contractor'), ('accounting', 'Accounting'),
                ('external', 'External party'),
            ]),
        ),
        migrations.AddField(
            'processtemplatestep', 'assignee_staff',
            models.ForeignKey(
                null=True, blank=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+',
                to='core.staffprofile',
            ),
        ),
        migrations.AddField('processtemplatestep', 'deadline_days_after_start', models.PositiveSmallIntegerField(null=True, blank=True)),

        migrations.AddField('processrunstep', 'step_key', models.SlugField(max_length=60, blank=True)),
        migrations.AddField(
            'processrunstep', 'step_type',
            models.CharField(max_length=30, default='checkbox', choices=[
                ('checkbox', 'Checkbox'), ('checklist', 'Checklist'), ('short_text', 'Short text'),
                ('long_text', 'Long text / notes'), ('number_currency', 'Number / currency'),
                ('date_time', 'Date and time'), ('dropdown_multiselect', 'Dropdown / multi-select'),
                ('record_selector', 'Record selector'), ('document_upload', 'Document upload'),
                ('photo_video_upload', 'Photo / video upload'), ('digital_signature', 'Digital signature'),
                ('task_assignment', 'Task assignment'), ('approval_decision', 'Approval / decision'),
                ('email_text_action', 'Email / text action'), ('calendar_event', 'Calendar event'),
                ('calculation_formula', 'Calculation / formula'), ('wait_timer', 'Wait / timer'),
            ]),
        ),
        migrations.AddField('processrunstep', 'config', models.JSONField(default=dict, blank=True)),
        migrations.AddField('processrunstep', 'response', models.JSONField(default=dict, blank=True)),
        migrations.AddField(
            'processrunstep', 'assignee_role',
            models.CharField(max_length=20, blank=True, choices=[
                ('admin', 'Admin'), ('property_manager', 'Property Manager'), ('maintenance', 'Maintenance'),
                ('cleaner', 'Cleaner'), ('contractor', 'Contractor'), ('accounting', 'Accounting'),
                ('external', 'External party'),
            ]),
        ),
        migrations.AddField(
            'processrunstep', 'assignee_staff',
            models.ForeignKey(
                null=True, blank=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+',
                to='core.staffprofile',
            ),
        ),
        migrations.AddField('processrunstep', 'deadline_days_after_start', models.PositiveSmallIntegerField(null=True, blank=True)),

        migrations.AlterField(
            'processrun', 'ticket',
            models.ForeignKey(
                null=True, blank=True, on_delete=django.db.models.deletion.CASCADE, related_name='process_runs',
                to='tickets.ticket',
            ),
        ),
        migrations.AddField(
            'processrun', 'property',
            models.ForeignKey(
                null=True, blank=True, on_delete=django.db.models.deletion.CASCADE, related_name='process_runs',
                to='core.property',
            ),
        ),
        migrations.AddField(
            'processrun', 'contact',
            models.ForeignKey(
                null=True, blank=True, on_delete=django.db.models.deletion.CASCADE, related_name='process_runs',
                to='core.contact',
            ),
        ),
        migrations.AddField('processrun', 'status', models.CharField(
            max_length=20, default='active',
            choices=[('active', 'Active'), ('completed', 'Completed'), ('cancelled', 'Cancelled')],
        )),
        migrations.AddField(
            'processrun', 'created_by',
            models.ForeignKey(
                null=True, blank=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
