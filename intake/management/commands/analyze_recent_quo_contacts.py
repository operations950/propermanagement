import logging
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from core.models import Contact, ContactImportCandidate, Property
from intake.adapters.quo import QuoAdapter
from intake.contact_classifier import classify_contact
from intake.models import QuoMessage
from messaging.services import _to_dash_format, _to_e164

logger = logging.getLogger(__name__)

# A one-time baseline pass, not the daily sync — deliberately wider than
# classify_pending_contacts.py's RECENT_WINDOW_DAYS (90): "spoken to in the
# last 12 months" is the actual ask, and this reads real message activity
# to decide who even qualifies, rather than staging every saved Quo contact
# regardless of whether we've heard from them recently (sync_quo_contacts's
# behavior).
WINDOW_DAYS = 365
MAX_MESSAGES = 200


def _has_recent_activity(phone_e164, cutoff):
    if not phone_e164:
        return False
    return QuoMessage.objects.filter(
        Q(from_number=phone_e164) | Q(to_number__icontains=phone_e164),
        quo_created_at__gte=cutoff,
    ).exists()


class Command(BaseCommand):
    help = (
        'One-time baseline pass (not the daily sync): for every contact Quo has saved that we\'ve '
        'actually exchanged messages with in the last 12 months, and who isn\'t already an approved '
        'Contact or a pending candidate, pulls their full 12-month message history and AI-classifies '
        'them (contact type / property / trade) into a fresh ContactImportCandidate for review at '
        '/contacts/review/. Run with --count-only first to see how many contacts qualify, with no Quo '
        'message fetch or Claude API calls at all — fast and free. No-op until QUO_API_KEY is set; '
        'ANTHROPIC_API_KEY is also required for a real (non-count-only) run.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--count-only', action='store_true',
            help='Report how many contacts qualify without fetching their message history or calling Claude.',
        )
        parser.add_argument(
            '--include-staged', action='store_true',
            help=(
                'One-time override: also reconsider contacts that already have a PENDING '
                'ContactImportCandidate (e.g. re-staged by the daily sync since a review-queue '
                'clear). Existing candidates get upgraded in place with the AI classification '
                'rather than duplicated. Approved/rejected Contacts are still always excluded.'
            ),
        )

    def handle(self, *args, **options):
        if not settings.QUO_API_KEY:
            self.stdout.write(self.style.WARNING('QUO_API_KEY not set — nothing to analyze.'))
            return

        count_only = options['count_only']
        include_staged = options['include_staged']
        cutoff = timezone.now() - timedelta(days=WINDOW_DAYS)

        existing_ext_ids = set(
            Contact.objects.exclude(quo_external_id='').values_list('quo_external_id', flat=True)
        )
        existing_phones = {
            _to_e164(p) for p in Contact.objects.exclude(phone='').values_list('phone', flat=True)
        }
        existing_phones.discard('')
        existing_emails = {e.lower() for e in Contact.objects.exclude(email='').values_list('email', flat=True)}
        pending_candidates_by_ext_id = {
            cand.external_id: cand
            for cand in ContactImportCandidate.objects.filter(status=ContactImportCandidate.Status.PENDING)
            .exclude(external_id='')
        }
        pending_ext_ids = set(pending_candidates_by_ext_id)

        contacts = QuoAdapter()._list_contacts()

        qualifying = []
        for c in contacts:
            external_id = c.get('id') or ''
            if not external_id or external_id in existing_ext_ids:
                continue
            if external_id in pending_ext_ids and not include_staged:
                continue
            fields = c.get('defaultFields') or {}
            name = ' '.join(filter(None, [fields.get('firstName'), fields.get('lastName')])).strip()
            company = (fields.get('company') or '').strip()
            phone = next((p.get('value') for p in (fields.get('phoneNumbers') or []) if p.get('value')), '')
            email = next((e.get('value') for e in (fields.get('emails') or []) if e.get('value')), '')
            if not phone and not email:
                continue
            phone_key = _to_e164(phone) if phone else ''
            if phone_key and phone_key in existing_phones:
                continue
            if email and email.lower() in existing_emails:
                continue
            if not _has_recent_activity(phone_key, cutoff):
                continue  # no message activity with this number in the last 12 months

            qualifying.append({
                'external_id': external_id, 'name': name or company, 'company': company,
                'phone': phone, 'phone_key': phone_key, 'email': email,
            })

        if count_only:
            suffix = (
                'no existing Contact record — would be analyzed on a real run (including '
                'already-staged pending candidates, per --include-staged).'
                if include_staged else
                'no existing Contact or pending-candidate record — would be analyzed on a real run.'
            )
            self.stdout.write(self.style.SUCCESS(
                f'{len(qualifying)} Quo-saved contact(s) have message activity in the last 12 months and {suffix}'
            ))
            return

        if not settings.ANTHROPIC_API_KEY:
            self.stdout.write(self.style.WARNING(
                f'{len(qualifying)} contact(s) qualify, but ANTHROPIC_API_KEY is not set — nothing to '
                'classify. Set it, then re-run without --count-only.'
            ))
            return

        property_names = list(Property.objects.filter(is_active=True).values_list('name', flat=True))
        created = upgraded = skipped_no_messages = 0
        for entry in qualifying:
            messages = list(
                QuoMessage.objects.filter(
                    Q(from_number=entry['phone_key']) | Q(to_number__icontains=entry['phone_key']),
                    quo_created_at__gte=cutoff,
                ).order_by('quo_created_at')
            )
            if not messages:
                skipped_no_messages += 1
                continue

            recent = messages[-MAX_MESSAGES:]
            transcript = self._format_transcript(recent, total_count=len(messages))
            contact_info = f'Name: {entry["name"] or "unknown"}'
            if entry['company']:
                contact_info += f' — company on file: {entry["company"]}'

            verdict = classify_contact(transcript, contact_info=contact_info, property_names=property_names)

            field_values = dict(
                name=entry['name'] or entry['company'], phone=_to_dash_format(entry['phone']),
                email=entry['email'],
                suggested_contact_type=verdict.contact_type if verdict else (
                    Contact.ContactType.VENDOR if entry['company'] else Contact.ContactType.OTHER
                ),
                trade=(verdict.trade if verdict else ''),
                suggested_property=(
                    Property.objects.filter(name=verdict.property_name).first()
                    if verdict and verdict.property_name else None
                ),
                raw_context=(
                    (f'Company on file (Quo): {entry["company"]}. ' if entry['company'] else '')
                    + (
                        f'[AI classification, {len(messages)} message(s) in the last 12 months]: {verdict.reasoning}'
                        if verdict else '[AI classification unavailable — see logs.]'
                    )
                ),
            )

            existing_pending = pending_candidates_by_ext_id.get(entry['external_id'])
            if existing_pending is not None:
                for field, value in field_values.items():
                    setattr(existing_pending, field, value)
                existing_pending.save(update_fields=list(field_values))
                candidate = existing_pending
                upgraded += 1
            else:
                candidate = ContactImportCandidate.objects.create(
                    source=Contact.Source.QUO, external_id=entry['external_id'], **field_values,
                )
                created += 1
            self.stdout.write(
                f'{candidate.name}: {candidate.suggested_contact_type} / '
                f'{candidate.suggested_property.name if candidate.suggested_property else "no property"}'
            )

        self.stdout.write(self.style.SUCCESS(
            f'Created {created} new contact candidate(s), upgraded {upgraded} already-staged candidate(s) '
            f'with AI classification, skipped {skipped_no_messages} with no messages found in the 12-month '
            'window.'
        ))

    def _format_transcript(self, messages, total_count):
        lines = []
        if total_count > len(messages):
            lines.append(
                f'[Showing the most recent {len(messages)} of {total_count} messages in the last 12 months.]'
            )
            lines.append('')
        for m in messages:
            speaker = 'Staff (Quo line)' if m.direction == QuoMessage.Direction.OUT else (m.from_number or 'Contact')
            timestamp = m.quo_created_at.isoformat() if m.quo_created_at else ''
            lines.append(f'[{timestamp}] {speaker}: {m.body}')
        return '\n'.join(lines)
