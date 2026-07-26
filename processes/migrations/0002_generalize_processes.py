import uuid

import django.db.models.deletion
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import migrations, models
from django.utils.text import slugify

OLD_ACTION_TYPE_MAP = {
    '': ('checkbox', {}),
    'google_meet': ('calendar_event', {'add_meet': True}),
    'document_template': ('document_upload', {}),
    'email_link': ('email_text_action', {}),
}


def _unique_key(base, used):
    base = (slugify(base)[:50] or 'step')
    key = base
    n = 2
    while key in used:
        key = f'{base}-{n}'
        n += 1
    used.add(key)
    return key


def migrate_data_forward(apps, schema_editor):
    ProcessTemplateStep = apps.get_model('processes', 'ProcessTemplateStep')
    ProcessRunStep = apps.get_model('processes', 'ProcessRunStep')
    ProcessInstanceDocument = apps.get_model('processes', 'ProcessInstanceDocument')
    ProcessAttachment = apps.get_model('processes', 'ProcessAttachment')

    # Template steps: assign a stable step_key (unique per template) and
    # translate the old 4-value action_type into the new step_type/config.
    for template_id in ProcessTemplateStep.objects.values_list('process_template_id', flat=True).distinct():
        used = set()
        for step in ProcessTemplateStep.objects.filter(process_template_id=template_id).order_by('sequence_order', 'pk'):
            step.step_key = _unique_key(step.label, used)
            step_type, config = OLD_ACTION_TYPE_MAP.get(step.action_type, ('checkbox', {}))
            step.step_type = step_type
            step.config = config
            step.save(update_fields=['step_key', 'step_type', 'config'])

    # Run steps: same step_key/step_type/config translation, plus fold the
    # old dedicated meeting_* columns into the new response JSON field.
    for run_id in ProcessRunStep.objects.values_list('run_id', flat=True).distinct():
        used = set()
        for step in ProcessRunStep.objects.filter(run_id=run_id).order_by('sequence_order', 'pk'):
            step.step_key = _unique_key(step.label, used)
            step_type, config = OLD_ACTION_TYPE_MAP.get(step.action_type, ('checkbox', {}))
            step.step_type = step_type
            step.config = config
            response = {}
            if step.meeting_datetime:
                response['meeting_datetime'] = step.meeting_datetime.isoformat()
            if step.meeting_link:
                response['meeting_link'] = step.meeting_link
            if step.meeting_dial_in:
                response['meeting_dial_in'] = step.meeting_dial_in
            if step.calendar_event_id:
                response['calendar_event_id'] = step.calendar_event_id
            if step.calendar_id:
                response['calendar_id'] = step.calendar_id
            step.response = response
            step.save(update_fields=['step_key', 'step_type', 'config', 'response'])

    # Preserve any already-saved prefilled document content (the old
    # document_template step type) as an uploaded .html attachment on the
    # migrated run step, rather than silently dropping it — this data has
    # nowhere else to live once ProcessInstanceDocument is removed below.
    for doc in ProcessInstanceDocument.objects.select_related('instance_item').all():
        if not doc.content:
            continue
        attachment = ProcessAttachment(
            run_step_id=doc.instance_item_id,
            caption='Migrated prefilled document content',
            uploaded_by_id=doc.generated_by_id,
        )
        attachment.file.save(
            f'migrated_document_{doc.instance_item_id}.html', ContentFile(doc.content.encode('utf-8')), save=False,
        )
        attachment.save()


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

        # --- Data migration (must run after the AddFields above, before the
        # RemoveFields/DeleteModel below that would delete the source data) ---
        migrations.RunPython(migrate_data_forward, migrations.RunPython.noop),

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
