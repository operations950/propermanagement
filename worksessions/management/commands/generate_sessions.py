from django.core.management.base import BaseCommand

from worksessions.services.generation import generate_due_sessions


class Command(BaseCommand):
    help = (
        'Opens Session instances from active SessionTemplates whose next_open_date (or any missed '
        'period since) has arrived. Idempotent (safe to run as often as you like — unique on '
        '(template, period_key)) and catch-up safe: a missed period always still opens its own '
        'Session rather than being fast-forwarded past.'
    )

    def handle(self, *args, **options):
        created = generate_due_sessions()
        self.stdout.write(self.style.SUCCESS(f'Opened {created} session(s).'))
