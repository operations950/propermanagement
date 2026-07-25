import logging
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Contact, ContactImportCandidate, Property
from intake.adapters.quo import QuoAdapter
from intake.contact_classifier import classify_contact
from intake.quo_contact_activity import build_text_activity_map, has_recent_call, list_our_phone_numbers
from messaging.services import _to_dash_format, _to_e164

logger = logging.getLogger(__name__)

# One-time baseline pass — "spoken to in the last 12 months" is the actual ask, read via Quo's
# own conversation/call history (see quo_contact_activity.py), not our local QuoMessage mirror,
# which only has what backfill/the live webhook happened to capture and can't be trusted complete.
WINDOW_DAYS = 365
MAX_MESSAGES = 200


class Command(BaseCommand):
    help = (
        'One-time baseline pass ("Analyze Quo for Contacts", not the daily sync): imports Quo\'s '
        'saved-contact list, drops anyone who already matches an existing Contact by phone/email/id, '
        'then checks Quo\'s own conversation and call history (not our local mirror) for real activity '
        "in the last 12 months. Contacts we've texted back and forth with get their full conversation "
        'read and classified by Claude; contacts with only a phone call on file get staged with '
        'whatever info is available, no transcript to analyze. Anyone with neither in the last 12 '
        'months is left alone. Run with --count-only first — it still crawls Quo\'s conversation/call '
        'history (that\'s the whole point) but skips Claude entirely, so it is free, just not instant.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--count-only', action='store_true',
            help='Report how many contacts qualify (and how) without calling Claude.',
        )
        parser.add_argument(
            '--include-staged', action='store_true',
            help=(
                'One-time override: also reconsider contacts that already have a PENDING '
                'ContactImportCandidate (e.g. re-staged by the daily sync since a review-queue '
                'clear). Existing candidates get upgraded in place rather than duplicated. '
                'Approved/rejected Contacts are still always excluded.'
            ),
        )

    def handle(self, *args, **options):
        if not settings.QUO_API_KEY:
            self.stdout.write(self.style.WARNING('QUO_API_KEY not set — nothing to analyze.'))
            return

        count_only = options['count_only']
        include_staged = options['include_staged']
        cutoff = timezone.now() - timedelta(days=WINDOW_DAYS)
        adapter = QuoAdapter()

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

        self.stdout.write('Fetching our own Quo phone lines…')
        our_number_ids, our_number_phones = list_our_phone_numbers(adapter)
        self.stdout.write(f'Found {len(our_number_ids)} line(s) on this Quo account.')

        self.stdout.write("Crawling Quo's full conversation history for message activity (one pass, all lines)…")
        text_activity = build_text_activity_map(adapter, our_number_phones)
        self.stdout.write(f'Found conversation activity for {len(text_activity)} distinct phone number(s).')

        contacts = adapter._list_contacts()

        qualifying_text = []
        qualifying_call_only = []
        ignored = 0
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

            entry = {
                'external_id': external_id, 'name': name or company, 'company': company,
                'phone': phone, 'phone_key': phone_key, 'email': email,
            }

            text_info = text_activity.get(phone_key) if phone_key else None
            if text_info and text_info['last_activity'] >= cutoff:
                qualifying_text.append({**entry, **text_info})
                continue

            if not phone_key:
                ignored += 1
                continue

            has_call, latest_call = has_recent_call(adapter, our_number_ids, phone_key, cutoff)
            if has_call:
                qualifying_call_only.append({**entry, 'latest_call': latest_call})
            else:
                ignored += 1

        if count_only:
            self.stdout.write(self.style.SUCCESS(
                f'{len(qualifying_text)} contact(s) with a real text conversation in the last 12 months, '
                f'{len(qualifying_call_only)} more with only a phone call on file, '
                f'{ignored} ignored (no text or call activity in the window) — would be processed on a real run'
                + (' (including already-staged pending candidates, per --include-staged).' if include_staged else '.')
            ))
            return

        if not settings.ANTHROPIC_API_KEY:
            self.stdout.write(self.style.WARNING(
                f'{len(qualifying_text)} + {len(qualifying_call_only)} contact(s) qualify, but '
                'ANTHROPIC_API_KEY is not set — nothing to classify. Set it, then re-run without --count-only.'
            ))
            return

        property_names = list(Property.objects.filter(is_active=True).values_list('name', flat=True))
        created = upgraded = skipped_no_messages = call_only_staged = 0

        for entry in qualifying_text:
            messages = adapter._list_messages(entry['phone_number_id'], entry['phone_key'])
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
            candidate, was_created = self._stage(entry['external_id'], field_values, pending_candidates_by_ext_id)
            created += was_created
            upgraded += not was_created
            self.stdout.write(
                f'{candidate.name}: {candidate.suggested_contact_type} / '
                f'{candidate.suggested_property.name if candidate.suggested_property else "no property"} (text)'
            )

        for entry in qualifying_call_only:
            call = entry['latest_call']
            call_date = call['created_at'].date() if call and call.get('created_at') else 'unknown date'
            direction = call.get('direction', '') if call else ''
            duration = call.get('duration', 0) if call else 0
            field_values = dict(
                name=entry['name'] or entry['company'], phone=_to_dash_format(entry['phone']),
                email=entry['email'],
                suggested_contact_type=Contact.ContactType.VENDOR if entry['company'] else Contact.ContactType.OTHER,
                trade='',
                suggested_property=None,
                raw_context=(
                    (f'Company on file (Quo): {entry["company"]}. ' if entry['company'] else '')
                    + f'Phone call only — no text conversation on file in the last 12 months. '
                    + f'Most recent call: {call_date}, {direction}, {duration}s.'
                ),
            )
            candidate, was_created = self._stage(entry['external_id'], field_values, pending_candidates_by_ext_id)
            created += was_created
            upgraded += not was_created
            call_only_staged += 1
            self.stdout.write(f'{candidate.name}: {candidate.suggested_contact_type} (call only)')

        self.stdout.write(self.style.SUCCESS(
            f'Created {created} new contact candidate(s) ({call_only_staged} call-only), upgraded {upgraded} '
            f'already-staged candidate(s), skipped {skipped_no_messages} with no messages actually found.'
        ))

    def _stage(self, external_id, field_values, pending_candidates_by_ext_id):
        """Creates a fresh ContactImportCandidate, or upgrades one already
        pending (e.g. re-staged by the daily sync) in place instead of
        duplicating it. Returns (candidate, was_created: bool)."""
        existing_pending = pending_candidates_by_ext_id.get(external_id)
        if existing_pending is not None:
            for field, value in field_values.items():
                setattr(existing_pending, field, value)
            existing_pending.save(update_fields=list(field_values))
            return existing_pending, False
        candidate = ContactImportCandidate.objects.create(
            source=Contact.Source.QUO, external_id=external_id, **field_values,
        )
        return candidate, True

    def _format_transcript(self, messages, total_count):
        lines = []
        if total_count > len(messages):
            lines.append(
                f'[Showing the most recent {len(messages)} of {total_count} messages in the last 12 months.]'
            )
            lines.append('')
        for m in messages:
            direction = 'Staff (Quo line)' if m.get('direction') == 'outgoing' else 'Contact'
            timestamp = m.get('createdAt', '')
            lines.append(f'[{timestamp}] {direction}: {m.get("text", "")}')
        return '\n'.join(lines)
