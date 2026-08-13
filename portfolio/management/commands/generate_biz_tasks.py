from django.core.management.base import BaseCommand

from portfolio.services.generation import generate_due_tasks


class Command(BaseCommand):
    help = 'Generates any BizTask rows due from active BizRecurringRules.'

    def handle(self, *args, **options):
        total = generate_due_tasks()
        self.stdout.write(self.style.SUCCESS(f'Generated {total} task(s).'))
