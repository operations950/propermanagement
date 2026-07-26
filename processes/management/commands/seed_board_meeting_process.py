"""Idempotent: seeds the "Board Meeting Checklist" ProcessTemplate (the
concrete first process this feature was built for — see the plan this
shipped under) so it's available to attach to a ticket out of the box.
get_or_create by name — safe to run on every deploy."""
from django.core.management.base import BaseCommand

from processes.models import ProcessTemplate, ProcessTemplateItem

TEMPLATE_NAME = 'Board Meeting Checklist'

ITEMS = [
    ('Reserve a space', {}),
    ('Get agenda approved', {}),
    ('Put Google Meet on calendar', {'action_type': ProcessTemplateItem.ActionType.GOOGLE_MEET}),
    (
        'Prepare notice',
        {'action_type': ProcessTemplateItem.ActionType.DOCUMENT_TEMPLATE, 'document_key': 'board_meeting_notice'},
    ),
    ('Physically post notice', {'requires_upload': True}),
    ('Send emails', {'action_type': ProcessTemplateItem.ActionType.EMAIL_LINK}),
    (
        'Prepare affidavit',
        {'action_type': ProcessTemplateItem.ActionType.DOCUMENT_TEMPLATE, 'document_key': 'board_meeting_affidavit'},
    ),
    ('Affidavit signature', {'requires_upload': True}),
    ('Conduct meeting', {}),
    ('Meeting notes', {}),
]


class Command(BaseCommand):
    help = 'Seeds the Board Meeting Checklist process template.'

    def handle(self, *args, **options):
        template, created = ProcessTemplate.objects.get_or_create(
            name=TEMPLATE_NAME,
            defaults={
                'description': 'Florida condo/HOA board meeting workflow: notice, posting, affidavit, and the '
                                'meeting itself. See Ch. 718.112 / 720.306 for statutory notice requirements.',
            },
        )
        if not created:
            self.stdout.write(f'"{TEMPLATE_NAME}" already exists — leaving items untouched.')
            return

        for sequence_order, (text, extra) in enumerate(ITEMS, start=1):
            ProcessTemplateItem.objects.create(
                process_template=template, text=text, sequence_order=sequence_order, **extra,
            )
        self.stdout.write(self.style.SUCCESS(f'Created "{TEMPLATE_NAME}" with {len(ITEMS)} item(s).'))
