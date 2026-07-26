"""Admin-triggered, one-off look-back over a single connected Gmail mailbox's
recent threads — for "did we miss anything in the last N days" without
waiting for (or being limited by) the normal incremental poll's PollCursor,
which may already be well past that window. Unlike the scheduled poll
(GmailAdapter.pull), this ALWAYS re-fetches and re-classifies every thread
found in the window, even ones already seen — the whole point is a manual
re-check, most usefully right after connecting a mailbox or after intake
logic changes (e.g. the Airbnb-booking detection this was built for).
GmailThreadState still gets updated the normal way, so the next regular
poll doesn't immediately redo this same work.

Triggered from Admin Tools (see intake/views.py::gmail_lookback_trigger) —
no Railway shell access, so this is the only way to run it on production."""
import logging
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from intake import classifier
from intake.adapters.gmail import GmailAdapter
from intake.models import GmailInboxToken

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "One-off look-back over a connected Gmail mailbox's recent threads, ignoring the normal poll cursor."

    def add_arguments(self, parser):
        parser.add_argument('--mailbox', required=True, help='The connected mailbox email to scan.')
        parser.add_argument('--days', type=int, default=2, help='How many days back to look (default 2).')

    def handle(self, *args, **options):
        mailbox_email = options['mailbox']
        days = options['days']
        token = GmailInboxToken.objects.filter(mailbox_email=mailbox_email).first()
        if not token:
            raise CommandError(f'No connected mailbox matches {mailbox_email!r}.')

        adapter = GmailAdapter()
        service = adapter._service(token)
        after = (timezone.now() - timedelta(days=days)).strftime('%Y/%m/%d')
        query = f'in:inbox after:{after}'

        threads = adapter._list_threads(service, query)
        self.stdout.write(f'{mailbox_email}: found {len(threads)} thread(s) since {after}.')

        effects = []
        for th in threads:
            thread_id = th['id']
            try:
                # known_last_message_id=None forces this thread to always
                # be rebuilt and reclassified, even if GmailThreadState
                # already has a row for it — a normal poll would skip it.
                event = adapter._build_event(service, thread_id, None, mailbox_email)
            except Exception:
                logger.exception('Gmail look-back: failed to fetch thread %s', thread_id)
                continue
            if not event:
                continue
            try:
                result = classifier.process_event(event)
            except Exception:
                logger.exception('Gmail look-back: failed to classify thread %s', thread_id)
                continue
            if result is not None:
                effects.append((event, result))

        self.stdout.write(self.style.SUCCESS(
            f'{mailbox_email}: {len(effects)} ticket(s)/effect(s) produced from {len(threads)} thread(s).'
        ))
        for event, result in effects:
            kind = getattr(result, 'kind', '')
            source = getattr(result, 'source', '')
            pk = getattr(result, 'pk', '?')
            title = getattr(result, 'title', str(result))
            flag = ' [AIRBNB BOOKING -> CLEANING]' if event.event_type == 'airbnb_email_booking' else ''
            self.stdout.write(f'  - #{pk} [{source}/{kind}]{flag}: {title}')
