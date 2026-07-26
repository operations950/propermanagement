"""Idempotent: seeds the "Board Meeting Checklist" ProcessTemplate (the
concrete first process this feature was built for) so it's available to
attach to a ticket out of the box. get_or_create by name — safe to run on
every deploy. Updated for the generalized Processes v2 schema (StepType
instead of the old 4-value action_type)."""
from django.core.management.base import BaseCommand

from processes.models import ProcessTemplate, ProcessTemplateStep, StepType

TEMPLATE_NAME = 'Board Meeting Checklist'

STEPS = [
    ('Reserve a space', StepType.CHECKBOX, {}),
    ('Get agenda approved', StepType.CHECKBOX, {}),
    ('Put Google Meet on calendar', StepType.CALENDAR_EVENT, {'config': {'add_meet': True}}),
    ('Prepare notice', StepType.DOCUMENT_UPLOAD, {}),
    ('Physically post notice', StepType.CHECKBOX, {'requires_upload': True}),
    ('Send emails', StepType.EMAIL_TEXT_ACTION, {}),
    ('Prepare affidavit', StepType.DOCUMENT_UPLOAD, {}),
    ('Affidavit signature', StepType.CHECKBOX, {'requires_upload': True}),
    ('Conduct meeting', StepType.CHECKBOX, {}),
    ('Meeting notes', StepType.LONG_TEXT, {}),
]


class Command(BaseCommand):
    help = 'Seeds the Board Meeting Checklist process template.'

    def handle(self, *args, **options):
        template, created = ProcessTemplate.objects.get_or_create(
            name=TEMPLATE_NAME,
            defaults={
                'category': 'Association',
                'description': 'Florida condo/HOA board meeting workflow: notice, posting, affidavit, and the '
                                'meeting itself. See Ch. 718.112 / 720.306 for statutory notice requirements.',
            },
        )
        if not created:
            self.stdout.write(f'"{TEMPLATE_NAME}" already exists — leaving steps untouched.')
            return

        for sequence_order, (label, step_type, extra) in enumerate(STEPS, start=1):
            ProcessTemplateStep.objects.create(
                process_template=template, label=label, step_type=step_type, sequence_order=sequence_order, **extra,
            )
        self.stdout.write(self.style.SUCCESS(f'Created "{TEMPLATE_NAME}" with {len(STEPS)} step(s).'))
