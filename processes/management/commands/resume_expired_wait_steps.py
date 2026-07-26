"""Auto-completes WAIT_TIMER process steps configured with wait_mode
'duration' once that many days have passed since their run started —
registered as a periodic job in proptasks/scheduler.py, same shape as
every other background poll in this app. Steps configured as
'manual_resume' or 'until_date' are left alone (no target date is
captured for 'until_date' yet — staff use the "Resume now" button)."""
from django.core.management.base import BaseCommand
from django.utils import timezone

from processes.models import ProcessRunStep, StepType


class Command(BaseCommand):
    help = 'Auto-resumes WAIT_TIMER steps whose configured duration has elapsed.'

    def handle(self, *args, **options):
        steps = ProcessRunStep.objects.filter(
            step_type=StepType.WAIT_TIMER, is_complete=False, config__wait_mode='duration',
        ).select_related('run')

        resumed = 0
        for step in steps:
            duration_days = step.config.get('duration_days')
            if not duration_days:
                continue
            elapsed = timezone.now() - step.run.created_at
            if elapsed.days >= duration_days:
                step.response = {**step.response, 'resumed_at': timezone.now().isoformat(), 'resumed_by': 'scheduled'}
                step.mark_complete()
                resumed += 1
                self.stdout.write(f'  ~ Resumed step #{step.pk} "{step.label}" on run #{step.run_id}')

        self.stdout.write(self.style.SUCCESS(f'Checked {steps.count()} wait step(s), resumed {resumed}.'))
