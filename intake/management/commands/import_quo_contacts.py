import logging

from django.conf import settings
from django.core.management.base import BaseCommand

from core.models import Contact, ContactImportCandidate
from intake.adapters.quo import QuoAdapter

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Pull every saved contact from Quo's Contacts API and stage the ones not already known (by "
        'phone) as ContactImportCandidate rows for human review — see the Contacts review queue. '
        'Safe to re-run: phones matching an existing Contact or an already-pending candidate are '
        'skipped. No-op until QUO_API_KEY is configured.'
    )

    def handle(self, *args, **options):
        if not settings.QUO_API_KEY:
            self.stdout.write(self.style.WARNING('QUO_API_KEY not set — nothing to import.'))
            return

        lookup = QuoAdapter()._build_contact_lookup()
        known_phones = set(Contact.objects.exclude(phone='').values_list('phone', flat=True))
        known_phones |= set(
            ContactImportCandidate.objects.filter(status=ContactImportCandidate.Status.PENDING)
            .exclude(phone='').values_list('phone', flat=True)
        )

        created = 0
        for phone, info in lookup.items():
            if phone in known_phones:
                continue
            company = (info.get('company') or '').strip()
            ContactImportCandidate.objects.create(
                source=Contact.Source.QUO,
                name=info.get('name') or phone,
                phone=phone,
                suggested_contact_type=Contact.ContactType.VENDOR if company else Contact.ContactType.OTHER,
                raw_context=f'Company on file (Quo): {company}' if company else 'Saved Quo contact, no company on file.',
            )
            known_phones.add(phone)
            created += 1

        self.stdout.write(self.style.SUCCESS(f'Staged {created} new contact candidate(s) from Quo.'))
