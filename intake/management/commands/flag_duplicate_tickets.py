"""One-time (safe to re-run) sweep for the "AI created near-duplicate
tickets, none flagged" bug — the live intake pipeline now checks every
newly-created ticket against other open tickets at the same property (see
intake/duplicate_classifier.py, intake/classifier.py::_flag_if_duplicate),
but that check only runs going forward. This retroactively looks for the
same kind of match among tickets that already existed before that check
was added.

Only ever flags (sets Ticket.possible_duplicate_of/duplicate_reasoning) so
the match shows up in the Pending screen's "Possible duplicate" queue for
a human to confirm or dismiss — never auto-cancels or merges anything, and
never re-flags a ticket that's already flagged (safe to re-run as new
tickets accumulate)."""
import logging

from django.core.management.base import BaseCommand

from intake import duplicate_classifier
from intake.classifier import ACTIVE_TICKET_STATUSES
from tickets.models import Ticket

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Flags existing open tickets that look like duplicates of an earlier open ticket at the same property.'

    def handle(self, *args, **options):
        candidates_qs = (
            Ticket.objects.filter(
                status__in=ACTIVE_TICKET_STATUSES, property__isnull=False, possible_duplicate_of__isnull=True,
            )
            .select_related('property').order_by('property_id', 'created_at')
        )
        by_property = {}
        for t in candidates_qs:
            by_property.setdefault(t.property_id, []).append(t)

        flagged = 0
        checked = 0
        for tickets in by_property.values():
            if len(tickets) < 2:
                continue
            for i, ticket in enumerate(tickets):
                earlier = tickets[:i]
                if not earlier:
                    continue
                checked += 1
                verdict = duplicate_classifier.find_duplicate_ticket(
                    ticket.property, earlier[-duplicate_classifier.MAX_CANDIDATES:], ticket.title, ticket.description,
                )
                if verdict is None or not verdict.is_duplicate or not verdict.duplicate_ticket_id:
                    continue
                match = next((c for c in earlier if c.pk == verdict.duplicate_ticket_id), None)
                if match is None:
                    continue
                ticket.possible_duplicate_of = match
                ticket.duplicate_reasoning = verdict.reasoning
                ticket.save(update_fields=['possible_duplicate_of', 'duplicate_reasoning'])
                flagged += 1
                self.stdout.write(self.style.WARNING(
                    f'  ~ #{ticket.pk} "{ticket.title}" flagged as possible duplicate of '
                    f'#{match.pk} "{match.title}"'
                ))

        self.stdout.write(self.style.SUCCESS(
            f'Checked {checked} ticket(s) against earlier open tickets at the same property, flagged {flagged}. '
            f'Review flagged tickets on the Pending screen.'
        ))
