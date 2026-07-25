import logging
from datetime import datetime, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Contact, ContactImportCandidate, ContactUpdateCandidate
from intake.adapters.quo import QuoAdapter
from intake.quo_contact_activity import build_text_activity_map, has_recent_call, list_our_phone_numbers
from messaging.services import _to_dash_format, _to_e164

logger = logging.getLogger(__name__)

# Same "have we actually talked to them" window as analyze_recent_quo_contacts.py's one-time
# pass, for one consistent definition of "worth reviewing" whether a contact showed up in the
# initial baseline import or was saved in Quo afterward.
ACTIVITY_WINDOW_DAYS = 365


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
        'brand-new Quo contacts as ContactImportCandidate rows for the usual review queue — but only '
        "ones we've actually had a new call or conversation with in the last 12 months (checked against "
        "Quo's own conversation/call history, same definition analyze_recent_quo_contacts.py's one-time "
        'pass uses), not merely "this name exists in Quo\'s address book." Also detects when an '
        "already-approved Contact's underlying Quo record has since changed (name/phone/email edited in "
        "Quo) using Quo's own per-contact updatedAt, staging that as a ContactUpdateCandidate for review "
        'rather than silently overwriting a Contact that may already be linked to tickets/properties/'
        'follow-ups. Matches by Quo\'s stable contact id once known; a Contact or pending candidate '
        'approved before this field existed is matched once by phone/email and has its id backfilled so '
        'every later run is id-based. Safe to re-run — a day with nothing changed in Quo does nothing. '
        'No-op until QUO_API_KEY is configured.'
    )

    def handle(self, *args, **options):
        if not settings.QUO_API_KEY:
            self.stdout.write(self.style.WARNING('QUO_API_KEY not set — nothing to sync.'))
            return

        adapter = QuoAdapter()
        contacts = adapter._list_contacts()
        cutoff = timezone.now() - timedelta(days=ACTIVITY_WINDOW_DAYS)
        our_number_ids, our_number_phones = list_our_phone_numbers(adapter)
        text_activity = build_text_activity_map(adapter, our_number_phones)

        # Contact.phone is stored in two different shapes depending on how the row was created —
        # dash-format XXX-XXX-XXXX when approved through the review form (core.models.phone_validator),
        # raw E.164 when auto-created straight from a live Quo event (intake/classifier.py, which
        # doesn't go through a ModelForm so the validator never runs). Quo's API always returns E.164.
        # Comparing either set of keys directly against Quo's raw value would silently fail to match
        # roughly half of them — normalize everything to E.164 before building any lookup or comparison.
        contacts_by_ext_id = {c.quo_external_id: c for c in Contact.objects.exclude(quo_external_id='')}
        contacts_by_phone = {}
        for c in Contact.objects.exclude(phone=''):
            key = _to_e164(c.phone)
            if key:
                contacts_by_phone[key] = c
        contacts_by_email = {c.email.lower(): c for c in Contact.objects.exclude(email='')}

        pending = ContactImportCandidate.objects.filter(status=ContactImportCandidate.Status.PENDING)
        pending_ext_ids = set(pending.exclude(external_id='').values_list('external_id', flat=True))
        pending_phones = {
            _to_e164(p) for p in pending.exclude(phone='').values_list('phone', flat=True) if _to_e164(p)
        }
        pending_emails = {e.lower() for e in pending.exclude(email='').values_list('email', flat=True)}

        created = updates_flagged = backfilled = skipped_nameless = skipped_no_activity = 0
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

            phone_key = _to_e164(phone) if phone else ''
            contact = contacts_by_ext_id.get(external_id)
            if contact is None:
                contact = contacts_by_phone.get(phone_key) or (contacts_by_email.get(email.lower()) if email else None)
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
                    or (phone and phone_key and phone_key != _to_e164(contact.phone))
                    or (email and email.lower() != contact.email.lower())
                )
                if changed and not ContactUpdateCandidate.objects.filter(
                    contact=contact, status=ContactUpdateCandidate.Status.PENDING,
                ).exists():
                    ContactUpdateCandidate.objects.create(
                        contact=contact, proposed_name=name or contact.name,
                        proposed_phone=_to_dash_format(phone), proposed_email=email,
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

            if external_id in pending_ext_ids or (phone_key and phone_key in pending_phones) or (
                email and email.lower() in pending_emails
            ):
                continue

            if not name and not company:
                # No first/last name AND no company on file — just a bare phone number Quo
                # auto-saved (e.g. someone who texted the line once), not what staff mean by
                # "a saved contact." Never worth a review-queue slot; if this person matters,
                # a real inbound message still creates a real Contact via the live ticket
                # pipeline (intake/classifier.py) regardless of whether they're staged here.
                skipped_nameless += 1
                continue

            has_activity = False
            latest_call = None
            if phone_key:
                text_info = text_activity.get(phone_key)
                if text_info and text_info['last_activity'] >= cutoff:
                    has_activity = True
                else:
                    has_activity, latest_call = has_recent_call(adapter, our_number_ids, phone_key, cutoff)
            if not has_activity:
                # Has a name and a phone, but no call or conversation with them in the last 12
                # months — a saved contact isn't itself "we should review this person," per the
                # one-time pass's same rule. Stays off the queue until they actually reach out.
                skipped_no_activity += 1
                continue

            raw_context = f'Company on file (Quo): {company}. ' if company else ''
            raw_context += (
                'Has a text conversation on file.' if phone_key and phone_key in text_activity
                and text_activity[phone_key]['last_activity'] >= cutoff
                else f'Phone call only — most recent {latest_call["created_at"].date()}.' if latest_call
                else 'Saved Quo contact.'
            )
            ContactImportCandidate.objects.create(
                source=Contact.Source.QUO, external_id=external_id, name=name or company,
                phone=_to_dash_format(phone), email=email,
                suggested_contact_type=Contact.ContactType.VENDOR if company else Contact.ContactType.OTHER,
                raw_context=raw_context,
            )
            pending_ext_ids.add(external_id)
            if phone_key:
                pending_phones.add(phone_key)
            if email:
                pending_emails.add(email.lower())
            created += 1

        self.stdout.write(self.style.SUCCESS(
            f'Staged {created} new contact candidate(s), flagged {updates_flagged} contact update(s) for '
            f'review, linked {backfilled} pre-existing contact(s) to their Quo id, skipped {skipped_nameless} '
            f'nameless bare-phone contact(s), skipped {skipped_no_activity} named contact(s) with no call or '
            f'conversation in the last 12 months.'
        ))
