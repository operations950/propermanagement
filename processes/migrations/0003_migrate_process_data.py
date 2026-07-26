from django.core.files.base import ContentFile
from django.db import migrations
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
    # nowhere else to live once ProcessInstanceDocument is removed in the
    # follow-up migration.
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
    """Isolated in its own migration/transaction so its row-level UPDATEs
    against processtemplatestep/processrunstep fully commit before the
    following migration's ALTER TABLE (RemoveField/AddConstraint) touches
    those same tables — running both in one transaction is what caused
    Postgres's "cannot ALTER TABLE ... because it has pending trigger
    events" crash on deploy (SQLite has no such restriction, which is why
    local dev testing never caught it)."""

    dependencies = [
        ('processes', '0002_generalize_processes'),
    ]

    operations = [
        migrations.RunPython(migrate_data_forward, migrations.RunPython.noop),
    ]
