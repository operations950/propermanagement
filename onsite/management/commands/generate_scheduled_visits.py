"""Generates Visit rows from active VisitRules whose interval has elapsed —
the recurring path for deep cleans/inspections, mirroring
tickets.generate_recurring_tickets' shape per CLAUDE.md's guidance to model
new automation on the recurring pattern rather than the reversed reactive
one."""
from datetime import timedelta

from dateutil.relativedelta import relativedelta
from django.core.management.base import BaseCommand
from django.utils import timezone

from onsite.models import Visit, VisitRule
from onsite.services.checklist import create_visit


class Command(BaseCommand):
    help = 'Generates Visit rows for VisitRules whose interval has elapsed.'

    def handle(self, *args, **options):
        today = timezone.localdate()
        created = 0
        # An addon bundle (e.g. deep-clean extras) is layered onto another
        # visit via Visit.is_deep_clean, not scheduled as a Visit of its own
        # — see onsite/services/checklist.py's set_deep_clean. A rule
        # pointed at one is a misconfiguration, not something to silently
        # generate ad-hoc Visits for.
        for rule in (
            VisitRule.objects.filter(is_active=True, visit_type__is_addon=False)
            .select_related('property', 'unit', 'visit_type')
        ):
            if not rule.last_generated_at:
                next_due = today
            elif rule.interval_days:
                next_due = rule.last_generated_at + timedelta(days=rule.interval_days)
            else:
                next_due = rule.last_generated_at + relativedelta(months=rule.interval_months)
            if next_due > today:
                continue

            create_visit(
                rule.property, rule.visit_type, unit=rule.unit,
                scheduled_date=today,
                assigned_staff=rule.default_assignee,
                status=Visit.Status.SCHEDULED if rule.default_assignee else Visit.Status.UNASSIGNED,
                created_from_rule=rule,
            )
            rule.last_generated_at = today
            rule.save(update_fields=['last_generated_at'])
            created += 1

        if created:
            self.stdout.write(self.style.SUCCESS(f'Generated {created} visit(s).'))
        else:
            self.stdout.write('No visits due.')
