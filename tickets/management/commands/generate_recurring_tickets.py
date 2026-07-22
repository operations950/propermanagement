from datetime import date, datetime, timedelta

from dateutil.relativedelta import relativedelta
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from core.models import Property

from ...models import Frequency, Ticket, TicketTemplate

STEP = {
    Frequency.DAILY: relativedelta(days=1),
    Frequency.WEEKLY: relativedelta(weeks=1),
    Frequency.BIWEEKLY: relativedelta(weeks=2),
    Frequency.MONTHLY: relativedelta(months=1),
    Frequency.QUARTERLY: relativedelta(months=3),
    Frequency.YEARLY: relativedelta(years=1),
}


def nth_business_day(year, month, n):
    """The nth Mon-Fri day of the given (year, month), 1-indexed, or None if
    the month doesn't have that many. Weekends are skipped; holidays are not
    currently accounted for (no holiday calendar configured yet)."""
    d = date(year, month, 1)
    count = 0
    while d.month == month:
        if d.weekday() < 5:
            count += 1
            if count == n:
                return d
        d += timedelta(days=1)
    return None


def next_workday_occurrence(after_date, workday_of_month):
    """The next MONTHLY_WORKDAY date after the month of `after_date` — real
    ops schedules like "Working Day 3" land on a different calendar date
    every month depending on where weekends fall, so this recomputes it
    fresh each month rather than adding a fixed interval."""
    month_cursor = date(after_date.year, after_date.month, 1) + relativedelta(months=1)
    for _ in range(24):  # safety cap; a realistic workday_of_month always resolves within a month or two
        due = nth_business_day(month_cursor.year, month_cursor.month, workday_of_month)
        if due:
            return due
        month_cursor += relativedelta(months=1)
    return after_date + relativedelta(months=1)  # pathological fallback, shouldn't happen


class Command(BaseCommand):
    help = (
        'Generate Ticket instances from active TicketTemplates whose next_run_date has arrived. '
        'Idempotent (safe to run as often as you like) and catch-up safe (backfills or skips '
        'missed occurrences per template.skip_missed if the scheduler was down).'
    )

    def handle(self, *args, **options):
        today = timezone.localdate()
        created_count = 0

        for template in TicketTemplate.objects.filter(is_active=True):
            is_workday = template.frequency == Frequency.MONTHLY_WORKDAY

            def advance(d, template=template, is_workday=is_workday):
                if is_workday:
                    return next_workday_occurrence(d, template.workday_of_month)
                return d + STEP[template.frequency]

            with transaction.atomic():
                if template.skip_missed:
                    while template.next_run_date < today:
                        template.next_run_date = advance(template.next_run_date)

                while template.next_run_date <= today:
                    properties = (
                        [template.property] if template.property_id
                        else list(Property.objects.filter(is_active=True))
                    )
                    for prop in properties:
                        due = timezone.make_aware(datetime.combine(template.next_run_date, datetime.min.time()))
                        _, created = Ticket.objects.get_or_create(
                            created_from_template=template, scheduled_for=template.next_run_date, property=prop,
                            defaults={
                                'title': template.title,
                                'description': template.description,
                                'kind': template.kind,
                                'source': Ticket.Source.RECURRING,
                                'assigned_role': template.default_assigned_role,
                                'assigned_staff': template.default_assigned_staff,
                                'priority': template.default_priority,
                                'status': (
                                    Ticket.Status.ASSIGNED if template.default_assigned_staff_id
                                    else Ticket.Status.OPEN
                                ),
                                'due_date': due,
                            },
                        )
                        if created:
                            created_count += 1
                    template.next_run_date = advance(template.next_run_date)

                template.save(update_fields=['next_run_date'])

        self.stdout.write(self.style.SUCCESS(f'Generated {created_count} recurring ticket(s).'))
