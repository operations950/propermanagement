import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Contact, ContactImportCandidate
from messaging.services import _to_e164

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        'One-time cleanup for a bug in an earlier sync_quo_contacts run: PENDING Quo-sourced '
        'ContactImportCandidate rows were compared against Contact.phone without normalizing format '
        "(Quo's API returns E.164, but Contact.phone is dash-format for anything approved through the "
        'review form), so hundreds of contacts that already exist got mass-staged as brand new. This '
        'finds any still-PENDING Quo candidate that actually matches an existing Contact once both '
        'sides are phone/email-normalized, backfills that Contact\'s quo_external_id, and marks the '
        'redundant candidate REJECTED (with resolved_contact set, so the link is preserved for audit) '
        'instead of leaving a duplicate sitting in the review queue. Safe to re-run — already-resolved '
        'candidates are skipped.'
    )

    def handle(self, *args, **options):
        contacts_by_phone = {}
        for c in Contact.objects.exclude(phone=''):
            key = _to_e164(c.phone)
            if key:
                contacts_by_phone[key] = c
        contacts_by_email = {c.email.lower(): c for c in Contact.objects.exclude(email='')}

        resolved = 0
        for candidate in ContactImportCandidate.objects.filter(
            status=ContactImportCandidate.Status.PENDING, source=Contact.Source.QUO,
        ):
            phone_key = _to_e164(candidate.phone) if candidate.phone else ''
            contact = contacts_by_phone.get(phone_key) or (
                contacts_by_email.get(candidate.email.lower()) if candidate.email else None
            )
            if contact is None:
                continue

            if candidate.external_id and not contact.quo_external_id:
                contact.quo_external_id = candidate.external_id
                contact.save(update_fields=['quo_external_id'])

            candidate.status = ContactImportCandidate.Status.REJECTED
            candidate.resolved_at = timezone.now()
            candidate.resolved_contact = contact
            candidate.raw_context = (
                candidate.raw_context
                + f'\n\n[Auto-resolved: matches existing contact "{contact.name}" once phone/email were '
                  f'normalized — see the sync_quo_contacts phone-format fix.]'
            )
            candidate.save(update_fields=['status', 'resolved_at', 'resolved_contact', 'raw_context'])
            resolved += 1

        self.stdout.write(self.style.SUCCESS(
            f'Resolved {resolved} pending candidate(s) that already match an existing contact.'
        ))
