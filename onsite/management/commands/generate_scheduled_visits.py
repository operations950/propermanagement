"""Generates Visit rows from active VisitRules whose next visit is coming up
— the recurring path for deep cleans/inspections, mirroring
tickets.generate_recurring_tickets' shape per CLAUDE.md's guidance to model
new automation on the recurring pattern rather than the reversed reactive
one. The dating rules live in onsite/services/recurring.py: each visit is
scheduled on the rule's next_due date and created a few days ahead."""
from django.core.management.base import BaseCommand
from django.utils import timezone

from onsite.models import VisitRule
from onsite.services.recurring import generate_for_rule


class Command(BaseCommand):
    help = 'Generates Visit rows for VisitRules whose next visit date is coming up.'

    def handle(self, *args, **options):
        today = timezone.localdate()
        created = 0
        # An addon bundle (e.g. deep-clean extras) is layered onto another
        # visit via Visit.is_deep_clean, not scheduled as a Visit of its own
        # — see onsite/services/checklist.py's set_deep_clean. A rule
        # pointed at one is a misconfiguration, not something to silently
        # generate ad-hoc Visits for. A general placeholder property never
        # gets visits (see create_visit).
        for rule in (
            VisitRule.objects.filter(is_active=True, visit_type__is_addon=False, property__is_general=False)
            .select_related('property', 'unit', 'visit_type', 'default_assignee')
        ):
            if generate_for_rule(rule, today):
                created += 1

        if created:
            self.stdout.write(self.style.SUCCESS(f'Generated {created} visit(s).'))
        else:
            self.stdout.write('No visits due.')
