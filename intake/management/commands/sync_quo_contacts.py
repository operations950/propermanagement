import logging
from datetime import datetime

from django.conf import settings
from django.core.management.base import BaseCommand

from core.models import Contact, ContactImportCandidate, ContactUpdateCandidate
from intake.adapters.quo import QuoAdapter

logger = logging.getLogger(__name__)


def _parse_quo_timestamp(iso_str):
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        return None


class Command(BaseCommand):
    help = (
        "Daily sync from Quo's Contacts API (settings.QUO_CONTACT_SYNC_INTERVAL_MINUTES): stages "
        'brand-new Quo contacts as ContactImportCandidate rows for the usual review queue, and — the '
        'part a one-off import can\'t do — detects when an already-approved Contact\'s underlying Quo '
        "record has since changed (name/phone/email edited in Quo) using Quo's own per-contact "
        'updatedAt, staging that as a ContactUpdateCandidate for review rather than silently overwriting '
        'a Contact that may already be linked to tickets/properties/follow-ups. Matches by Quo\'s stable '
        'contact id once known; a Contact or pending candidate approved before this field existed is '
        'matched once by phone/email and has its id backfilled so every later run is id-based. Safe to '
        're-run — a day with nothing changed in Quo does nothing. No-op until QUO_API_KEY is configured.'
    )

    def handle(self, *args, **options):
        if not settings.QUO_API_KEY:
            self.stdout.write(self.style.WARNING('QUO_API_KEY not set — nothing to sync.'))
            return

        contacts = QuoAdapter()._list_contacts()

        contacts_by_ext_id = {c.quo_external_id: c for c in Contact.objects.exclude(quo_external_id='')}
        contacts_by_phone = {c.phone: c for c in Contact.objects.exclude(phone='')}
        contacts_by_email = {c.email.lower(): c for c in Contact.objects.exclude(email='')}

        pending = ContactImportCandidate.objects.filter(status=ContactImportCandidate.Status.PENDING)
        pending_ext_ids = set(pending.exclude(external_id='').values_list('external_id', flat=True))
        pending_phones = set(pending.exclude(phone='').values_list('phone', flat=True))
        pending_emails = {e.lower() for e in pending.exclude(email='').values_list('email', flat=True)}

        created = updates_flagged = backfilled = 0
        for c in contacts:
            external_id = c.get('id') or ''
            if not external_id:
                continue
            fields = c.get('defaultFields') or {}
            name = ' '.join(filter(None, [fields.get('firstName'), fields.get('lastName')])).strip()
            company = (fields.get('company') or '').strip()
            phone = next((p.get('value') for p in (fields.get('phoneNumbers') or []) if p.get('value')), '')
            email = next((e.get('value') for e in (fields.get('emails') or []) if e.get('value')), '')
            if not phone and not email:
                continue
            quo_updated_at = _parse_quo_timestamp(c.get('updatedAt'))

            contact = contacts_by_ext_id.get(external_id)
            if contact is None:
                contact = contacts_by_phone.get(phone) or (contacts_by_email.get(email.lower()) if email else None)
                if contact is not None:
                    # A real Contact that predates external_id tracking — link it up
                    # silently (recognizing an existing person, not a content change)
                    # so every future run for them is id-based instead of a phone guess.
                    contact.quo_external_id = external_id
                    contact.quo_updated_at = quo_updated_at
                    contact.save(update_fields=['quo_external_id', 'quo_updated_at'])
                    contacts_by_ext_id[external_id] = contact
                    backfilled += 1
                    continue

            if contact is not None:
                if contact.quo_updated_at and quo_updated_at and quo_updated_at <= contact.quo_updated_at:
                    continue  # nothing changed in Quo since the last sync
                changed = (
                    (name and name != contact.name)
                    or (phone and phone != contact.phone)
                    or (email and email != contact.email)
                )
                if changed and not ContactUpdateCandidate.objects.filter(
                    contact=contact, status=ContactUpdateCandidate.Status.PENDING,
                ).exists():
                    ContactUpdateCandidate.objects.create(
                        contact=contact, proposed_name=name or contact.name, proposed_phone=phone,
                        proposed_email=email,
                        raw_context=(
                            f'Quo contact changed as of {quo_updated_at.date() if quo_updated_at else "recently"} '
                            f'— was "{contact.name}" / {contact.phone or "no phone"} / {contact.email or "no email"}.'
                        ),
                    )
                    updates_flagged += 1
                if quo_updated_at:
                    contact.quo_updated_at = quo_updated_at
                    contact.save(update_fields=['quo_updated_at'])
                continue

            if external_id in pending_ext_ids or phone in pending_phones or (email and email.lower() in pending_emails):
                continue

            ContactImportCandidate.objects.create(
                source=Contact.Source.QUO, external_id=external_id, name=name or phone or email,
                phone=phone, email=email,
                suggested_contact_type=Contact.ContactType.VENDOR if company else Contact.ContactType.OTHER,
                raw_context=f'Company on file (Quo): {company}' if company else 'Saved Quo contact, no company on file.',
            )
            pending_ext_ids.add(external_id)
            if phone:
                pending_phones.add(phone)
            if email:
                pending_emails.add(email.lower())
            created += 1

        self.stdout.write(self.style.SUCCESS(
            f'Staged {created} new contact candidate(s), flagged {updates_flagged} contact update(s) for '
            f'review, linked {backfilled} pre-existing contact(s) to their Quo id.'
        ))
