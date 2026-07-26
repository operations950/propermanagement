"""Idempotent one-time cleanup for a bug in intake/classifier.py's
_reconcile_thread_ticket (shared by Gmail and Quo whole-thread
classification): the existing-ticket lookup used to filter by
(source, source_reference, kind), but `kind` is derived from Claude's
per-run role guess and can drift between reclassifications of the exact
same email/text thread ("generic" one run, "maintenance" the next). That
let a single conversation accumulate more than one live (non-cancelled)
Ticket row, since the DB's own UniqueConstraint on
('source', 'source_reference', 'kind') is deliberately scoped to include
kind (other handlers legitimately share an external id across genuinely
different fixed kinds, e.g. a booking's 'cleaning' ticket vs. a separate
'maintenance' ticket) and so does not stop two *different* kind values
from coexisting for the same reference.

This only ever affects kind in ('generic', 'maintenance') — the only two
values _reconcile_thread_ticket ever sets — so the scope here is
deliberately narrowed to those, leaving fixed-kind handlers (booking/
maintenance-from-event/generic-from-event) alone.

For each (source, source_reference) group with more than one live ticket
in that kind set: keep whichever one staff has actually engaged with
(assigned_staff, assigned_contact, or resolution_notes set) if any,
otherwise keep the most recently created one (the latest reclassification
is the most accurate read of the thread); cancel the rest with a note
pointing at the surviving ticket, rather than deleting them, so nothing
is silently lost.

Safe to run repeatedly — already-cancelled duplicates are excluded from
consideration on subsequent runs."""
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.utils import timezone

from tickets.models import Ticket

DEDUPE_KINDS = ('generic', 'maintenance')


class Command(BaseCommand):
    help = 'Cancels duplicate tickets that accumulated for the same email/text thread due to a kind-drift bug.'

    def handle(self, *args, **options):
        candidates = (
            Ticket.objects.filter(kind__in=DEDUPE_KINDS)
            .exclude(source_reference='')
            .exclude(status=Ticket.Status.CANCELLED)
            .order_by('created_at')
        )

        groups = defaultdict(list)
        for ticket in candidates:
            groups[(ticket.source, ticket.source_reference)].append(ticket)

        cancelled = 0
        for (source, source_reference), tickets in groups.items():
            if len(tickets) < 2:
                continue

            touched = [
                t for t in tickets
                if t.assigned_staff_id or t.assigned_contact_id or t.resolution_notes
            ]
            keeper = touched[-1] if touched else tickets[-1]

            for ticket in tickets:
                if ticket.pk == keeper.pk:
                    continue
                ticket.status = Ticket.Status.CANCELLED
                ticket.cancelled_at = timezone.now()
                ticket.cancelled_reason = (
                    f'Duplicate ticket for the same {source} thread ({source_reference}) — '
                    f'merged into ticket #{keeper.pk} during dedup backfill.'
                )
                ticket.save()
                cancelled += 1
                self.stdout.write(
                    f'Cancelled ticket #{ticket.pk} ("{ticket.title}") — duplicate of #{keeper.pk} '
                    f'for {source}/{source_reference}.'
                )

        self.stdout.write(self.style.SUCCESS(f'Cancelled {cancelled} duplicate ticket(s).'))
