"""Generates Visit rows from active VisitRules whose interval has elapsed —
the recurring path for deep cleans/inspections, mirroring
tickets.generate_recurring_tickets' shape per CLAUDE.md's guidance to model
new automation on the recurring pattern rather than the reversed reactive
one."""
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
        for rule in VisitRule.objects.filter(is_active=True).select_related('property', 'visit_type'):
            next_due = (
                rule.last_generated_at + relativedelta(months=rule.interval_months)
                if rule.last_generated_at else today
            )
            if next_due > today:
                continue

            create_visit(
                rule.property, rule.visit_type,
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
