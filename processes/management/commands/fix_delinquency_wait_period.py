"""One-off, idempotent fix: the "Association Delinquency and Collection" template was seeded with a 45-day cure-period
wait step, but the association's late-assessment letter gives an owner 30 days. Updates that one step to 30 days —
only when it is still exactly the 45-day default the seed created, never touching a step someone has since edited by
hand in the builder. Safe to run more than once (the second run finds nothing left to change)."""
from django.core.management.base import BaseCommand

from processes.models import ProcessTemplateStep


class Command(BaseCommand):
    help = 'Changes the delinquency process\'s cure-period wait step from 45 days to 30, if it is still the seeded default.'

    def handle(self, *args, **options):
        step = ProcessTemplateStep.objects.filter(
            process_template__name='Association Delinquency and Collection',
            label='Wait through the cure period',
        ).first()
        if step is None:
            self.stdout.write('No such step found — nothing to do.')
            return
        if (step.config or {}).get('duration_days') != 45:
            self.stdout.write(f'Already {step.config.get("duration_days")} days — nothing to do.')
            return
        step.config = {**step.config, 'duration_days': 30}
        step.help_text = "Default 30 days — confirm against the association's bylaws."
        step.save(update_fields=['config', 'help_text'])
        self.stdout.write(self.style.SUCCESS('Cure period changed from 45 to 30 days.'))
