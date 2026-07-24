from datetime import date, datetime, timedelta

from dateutil.relativedelta import relativedelta
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from ...models import (
    Frequency,
    PackageRun,
    PropertyTemplateOverride,
    TaskPackageTemplate,
    TemplateOccurrence,
    Ticket,
    TicketChecklistItem,
    TicketTemplate,
)
from ...services import applicability

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


def _advance(cursor, frequency, workday_of_month):
    if frequency == Frequency.MONTHLY_WORKDAY:
        return next_workday_occurrence(cursor, workday_of_month)
    return cursor + STEP[frequency]


def _package_step_and_run(template, property, scheduled_for):
    """The TaskPackageTemplate step this template plays in an active
    package assigned to `property`, and the PackageRun to attach generated
    tickets to — or (None, None) if this template isn't an active package
    step for this property."""
    step = (
        TaskPackageTemplate.objects.filter(
            template=template, package__is_active=True, package__property_assignments__property=property,
        )
        .select_related('package', 'depends_on__template')
        .first()
    )
    if not step:
        return None, None
    run, _ = PackageRun.objects.get_or_create(package=step.package, property=property, scheduled_for=scheduled_for)
    return step, run


def _initial_status(scheduled_for, today, lead_time_days, assigned_staff_id, step, run):
    if step and step.depends_on_id and run:
        prereq_ticket = Ticket.objects.filter(
            package_run=run, created_from_template_id=step.depends_on.template_id,
        ).first()
        if not prereq_ticket or prereq_ticket.status not in Ticket.DEPENDENCY_SATISFYING_STATUSES:
            return Ticket.Status.BLOCKED
    if lead_time_days and today < scheduled_for:
        return Ticket.Status.UPCOMING
    return Ticket.Status.ASSIGNED if assigned_staff_id else Ticket.Status.OPEN


def _generate_occurrence(template, properties, scheduled_for, today, checklist_specs, created_count):
    occurrence, _ = TemplateOccurrence.objects.get_or_create(template=template, scheduled_for=scheduled_for)
    due = timezone.make_aware(datetime.combine(scheduled_for, datetime.min.time()))

    for prop in properties:
        effective = applicability.effective_settings(template, prop)
        step, run = _package_step_and_run(template, prop, scheduled_for)
        status = _initial_status(
            scheduled_for, today, template.lead_time_days,
            effective['assigned_staff'] and effective['assigned_staff'].id, step, run,
        )
        ticket, created = Ticket.objects.get_or_create(
            created_from_template=template, scheduled_for=scheduled_for, property=prop,
            defaults={
                'title': template.title,
                'description': template.description,
                'kind': template.kind,
                'source': Ticket.Source.RECURRING,
                'assigned_role': effective['assigned_role'],
                'assigned_staff': effective['assigned_staff'],
                'priority': effective['priority'],
                'status': status,
                'due_date': due,
                'template_occurrence': occurrence,
                'package_run': run,
            },
        )
        if created:
            created_count[0] += 1
            if checklist_specs:
                TicketChecklistItem.objects.bulk_create([
                    TicketChecklistItem(
                        ticket=ticket, text=spec['text'], sequence_order=spec['sequence_order'],
                        is_required=spec['is_required'],
                    )
                    for spec in checklist_specs
                ])


def _run_cursor_group(template, properties, frequency, workday_of_month, cursor_holder, today, created_count):
    """Advances one shared cursor (the template's own next_run_date for
    properties with no frequency override, or a single override's own
    next_run_date for a property with one) forward through every occurrence
    up to today, generating tickets for `properties` at each stop."""
    checklist_specs = [
        {'text': item.text, 'sequence_order': item.sequence_order, 'is_required': item.is_required}
        for item in template.checklist_items.all()
    ]
    cursor = cursor_holder.next_run_date

    if template.skip_missed:
        while cursor < today:
            cursor = _advance(cursor, frequency, workday_of_month)

    lead = timedelta(days=template.lead_time_days)
    while cursor - lead <= today:
        _generate_occurrence(template, properties, cursor, today, checklist_specs, created_count)
        cursor = _advance(cursor, frequency, workday_of_month)

    cursor_holder.next_run_date = cursor
    cursor_holder.save(update_fields=['next_run_date'])


class Command(BaseCommand):
    help = (
        'Generate Ticket instances from active TicketTemplates whose applicable properties (see '
        'tickets.services.applicability) have a due occurrence. Idempotent (safe to run as often as '
        'you like) and catch-up safe (backfills or skips missed occurrences per template.skip_missed '
        'if the scheduler was down).'
    )

    def handle(self, *args, **options):
        today = timezone.localdate()
        created_count = [0]

        for template in TicketTemplate.objects.filter(is_active=True).prefetch_related('checklist_items'):
            if template.target_type == TicketTemplate.TargetType.COMPANY:
                # A single company-wide occurrence per period, not fanned out
                # to any property — [None] flows through the exact same
                # cursor/occurrence/ticket machinery every other template
                # uses (effective_settings and _package_step_and_run both
                # already handle property=None).
                with transaction.atomic():
                    _run_cursor_group(
                        template, [None], template.frequency, template.workday_of_month,
                        template, today, created_count,
                    )
                continue

            effective_properties = applicability.effective_properties_for_template(template)
            if not effective_properties:
                continue

            property_ids = [p.id for p in effective_properties]
            frequency_overrides = {
                o.property_id: o for o in PropertyTemplateOverride.objects.filter(
                    template=template, property_id__in=property_ids,
                    action=PropertyTemplateOverride.Action.INCLUDE,
                ).exclude(frequency='')
            }
            normal_properties = [p for p in effective_properties if p.id not in frequency_overrides]
            overridden_properties = [p for p in effective_properties if p.id in frequency_overrides]
            # Captured before the normal-properties cursor advances template.next_run_date in place —
            # a freshly-created override's cursor must start from where the template started this run,
            # not from wherever the shared cursor ends up after advancing past today's occurrences.
            starting_next_run_date = template.next_run_date

            with transaction.atomic():
                if normal_properties:
                    _run_cursor_group(
                        template, normal_properties, template.frequency, template.workday_of_month,
                        template, today, created_count,
                    )

                for prop in overridden_properties:
                    override = frequency_overrides[prop.id]
                    if override.next_run_date is None:
                        override.next_run_date = starting_next_run_date
                        override.save(update_fields=['next_run_date'])
                    _run_cursor_group(
                        template, [prop], override.frequency,
                        override.workday_of_month if override.workday_of_month is not None else template.workday_of_month,
                        override, today, created_count,
                    )

        self.stdout.write(self.style.SUCCESS(f'Generated {created_count[0]} recurring ticket(s).'))
