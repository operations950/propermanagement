"""One-off backfill for the "staff-side status override skips issue->ticket
conversion" bug (see onsite/views.py::visit_detail's set_status action —
now fixed to call checklist_service.create_issue_tickets, but that only
covers status changes from here on). Any visit that was pushed to
Submitted/Verified by hand BEFORE the fix has reported issues sitting with
no created_ticket that will otherwise never get one, since nothing
re-checks an already-Submitted/Verified visit.

Dry-run by default — prints exactly which issues would be converted.
--apply actually creates the tickets. Safe to re-run: create_issue_tickets
only ever touches issues with created_ticket__isnull=True, so a second run
after --apply finds nothing left to do."""
from django.core.management.base import BaseCommand

from onsite.models import Visit, VisitIssue
from onsite.services.checklist import create_issue_tickets


class Command(BaseCommand):
    help = 'Converts any orphaned VisitIssue (visit already Submitted/Verified, no ticket yet) into a real Ticket.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Actually create the tickets (default: dry run).')

    def handle(self, *args, **options):
        orphaned = (
            VisitIssue.objects
            .filter(created_ticket__isnull=True, visit__status__in=[Visit.Status.SUBMITTED, Visit.Status.VERIFIED])
            .select_related('visit__property')
        )
        orphaned = list(orphaned)

        if not orphaned:
            self.stdout.write(self.style.SUCCESS('No orphaned issues found — nothing to do.'))
            return

        self.stdout.write(f'{len(orphaned)} orphaned issue(s) found:')
        for issue in orphaned:
            self.stdout.write(
                f'  Visit #{issue.visit_id} ({issue.visit.property.name}, {issue.visit.get_status_display()}) '
                f'— "{issue.description[:70]}"'
            )

        if not options['apply']:
            self.stdout.write(self.style.WARNING('\nDry run — pass --apply to actually create these tickets.'))
            return

        visits_touched = {issue.visit_id for issue in orphaned}
        for visit_id in visits_touched:
            create_issue_tickets(Visit.objects.get(pk=visit_id))

        self.stdout.write(self.style.SUCCESS(
            f'\nCreated tickets for {len(orphaned)} issue(s) across {len(visits_touched)} visit(s).'
        ))
