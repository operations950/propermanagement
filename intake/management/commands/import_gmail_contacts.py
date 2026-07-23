import logging
from datetime import timedelta
from email.utils import parseaddr

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Contact, ContactImportCandidate
from intake.adapters.gmail import GmailAdapter
from intake.gmail_auth import is_configured
from intake.models import GmailInboxToken

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        'List recent threads in the connected Gmail inbox and stage unique senders not already known '
        '(by email) as ContactImportCandidate rows for human review — see the Contacts review queue. '
        'Bounded to GMAIL_CONTACT_IMPORT_DAYS of history (wider than the live poll window, since this '
        'is a one-off/periodic pass to build up the contact base, not per-ticket linking). Safe to '
        're-run: emails matching an existing Contact or an already-pending candidate are skipped. '
        'No-op until Gmail is connected (see /integrations/gmail/connect/, admin-only).'
    )

    def handle(self, *args, **options):
        if not is_configured() or not GmailInboxToken.objects.exists():
            self.stdout.write(self.style.WARNING('Gmail is not connected — nothing to import.'))
            return

        adapter = GmailAdapter()
        service = adapter._service()
        if service is None:
            self.stdout.write(self.style.WARNING('Gmail is not connected — nothing to import.'))
            return

        since = timezone.now() - timedelta(days=settings.GMAIL_CONTACT_IMPORT_DAYS)
        query = f'in:inbox after:{since.strftime("%Y/%m/%d")}'
        threads = adapter._list_threads(service, query)
        self.stdout.write(f'Scanning {len(threads)} thread(s) from the last {settings.GMAIL_CONTACT_IMPORT_DAYS} day(s)...')

        known_emails = set(Contact.objects.exclude(email='').values_list('email', flat=True))
        known_emails |= set(
            ContactImportCandidate.objects.filter(status=ContactImportCandidate.Status.PENDING)
            .exclude(email='').values_list('email', flat=True)
        )

        created = 0
        for th in threads:
            try:
                thread = service.users().threads().get(userId='me', id=th['id'], format='metadata').execute()
            except Exception:
                logger.exception('Gmail contact import: failed to fetch thread %s', th['id'])
                continue
            messages = thread.get('messages', [])
            if not messages:
                continue
            first_message = messages[0]
            from_header = adapter._header(first_message, 'From')
            name, email_addr = parseaddr(from_header)
            email_addr = email_addr.strip().lower()
            if not email_addr or email_addr in known_emails:
                continue
            subject = adapter._header(first_message, 'Subject') or '(no subject)'
            ContactImportCandidate.objects.create(
                source=Contact.Source.GMAIL,
                name=name or email_addr,
                email=email_addr,
                raw_context=f'Subject: {subject}',
            )
            known_emails.add(email_addr)
            created += 1

        self.stdout.write(self.style.SUCCESS(f'Staged {created} new contact candidate(s) from Gmail.'))
