"""Idempotent, runs on every deploy (Procfile). Loads the approved template library (processes/library_data.py): the improved
Board Meeting and Annual Meeting templates, the split New Owner / Tenant applications, the rewritten New Association
Onboarding, and the new Special Assessment, Members Meeting, Insurance Renewal, Owner Sale, Short-Term Rental Onboarding and
Annual CAM Reconciliation templates.

A template's steps are written only when its library_version is behind LIBRARY_VERSION, so a deploy never overwrites steps
someone edited afterwards. Projects already running keep the steps they started with (a run holds its own copy).
The old "Association Application Process" is switched off (not deleted) once both replacements exist."""
from django.core.management.base import BaseCommand
from django.db import transaction

from processes.library_data import LIBRARY, LIBRARY_VERSION
from processes.models import ProcessTemplate, ProcessTemplateStep


def _find(spec):
    template = ProcessTemplate.objects.filter(name=spec['name']).order_by('id').first()
    if template is not None:
        return template, False
    for old in spec['old_names']:
        template = ProcessTemplate.objects.filter(name__istartswith=old).exclude(name__istartswith='Association Onboarding').order_by('id').first()
        if template is not None:
            return template, True
    return None, False


class Command(BaseCommand):
    help = 'Loads the approved process template library (safe to run repeatedly).'

    def handle(self, *args, **options):
        for spec in LIBRARY:
            with transaction.atomic():
                self._load(spec)
        replaced = ProcessTemplate.objects.filter(name__in=('New Owner Application', 'Tenant Application')).count() == 2
        if replaced:
            switched = ProcessTemplate.objects.filter(name='Association Application Process', is_active=True).update(is_active=False)
            if switched:
                self.stdout.write('Switched off "Association Application Process" (replaced by the New Owner and Tenant applications).')
        self.stdout.write(self.style.SUCCESS('Template library loaded.'))

    def _load(self, spec):
        template, renamed = _find(spec)
        if template is None:
            template = ProcessTemplate.objects.create(
                name=spec['name'], category=spec['category'], description=spec['description'], kind=ProcessTemplate.Kind.PROJECT,
            )
            self.stdout.write(f'Created "{spec["name"]}".')
        elif renamed:
            self.stdout.write(f'Renamed "{template.name}" to "{spec["name"]}".')
            template.name = spec['name']
            template.save(update_fields=['name'])
        if template.library_version >= LIBRARY_VERSION:
            self.stdout.write(f'"{spec["name"]}" is already up to date.')
            return
        template.steps.all().delete()
        for order, step in enumerate(spec['steps'], start=1):
            ProcessTemplateStep.objects.create(
                process_template=template, sequence_order=order, step_type=step['type'], label=step['label'], help_text=step['help'],
                config=step['config'], is_required=True, requires_upload=step['requires_upload'], deadline_days_after_start=step['day'],
            )
        template.kind = ProcessTemplate.Kind.PROJECT
        template.is_active = True
        template.library_version = LIBRARY_VERSION
        update = ['kind', 'is_active', 'library_version']
        if not template.category:
            template.category = spec['category']
            update.append('category')
        if not template.description:
            template.description = spec['description']
            update.append('description')
        template.save(update_fields=update)
        self.stdout.write(self.style.SUCCESS(f'"{spec["name"]}" loaded with {len(spec["steps"])} steps.'))
