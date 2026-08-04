"""Idempotent: gives every ticket lacking any assignee (neither
assigned_staff nor assigned_contact) a real person — a DepartmentDefaultAssignee
match for its assigned_role if one exists, else any company-admin StaffProfile
as a last-resort catch-all. Marks assignment_source='auto' either way (see
Ticket.save() for the same logic applied automatically going forward on new
tickets).

Required before Ticket's assignment CheckConstraint can ever be tightened
from "at most one" to "exactly one" — that migration is NOT part of this
change and should only be added once this command reports zero still-
unassigned rows in production (see ONSITE_DESIGN.md-adjacent notes in
CLAUDE.md on backfill-before-constraint sequencing: `migrate` always runs
before any Procfile command, so a not-yet-satisfied CheckConstraint would
fail the very deploy meant to fix it).

Safe to re-run: every ticket this touches now has an assignee, so the next
run's queryset is empty."""
from django.core.management.base import BaseCommand

from core.models import StaffProfile
from tickets.models import DepartmentDefaultAssignee, Ticket


class Command(BaseCommand):
    help = 'Assigns a real person to every ticket with neither assigned_staff nor assigned_contact set.'

    def handle(self, *args, **options):
        unassigned = Ticket.objects.filter(assigned_staff__isnull=True, assigned_contact__isnull=True)
        defaults_by_role = {d.role: d.staff for d in DepartmentDefaultAssignee.objects.select_related('staff')}
        fallback = StaffProfile.objects.filter(is_company_admin=True).first()

        assigned = 0
        still_unassigned = 0
        for ticket in unassigned:
            staff = defaults_by_role.get(ticket.assigned_role) or fallback
            if not staff:
                still_unassigned += 1
                continue
            ticket.assigned_staff = staff
            ticket.assignment_source = Ticket.AssignmentSource.AUTO
            ticket.save(update_fields=['assigned_staff', 'assignment_source'])
            assigned += 1

        self.stdout.write(self.style.SUCCESS(f'Auto-assigned {assigned} ticket(s).'))
        if still_unassigned:
            self.stdout.write(self.style.WARNING(
                f'{still_unassigned} ticket(s) still have no assignee — no DepartmentDefaultAssignee for '
                "their role and no company-admin StaffProfile exists as a fallback. Configure at least "
                'one of those (Admin → Department Default Assignees, or grant a StaffProfile '
                'is_company_admin), then re-run this command.',
            ))
