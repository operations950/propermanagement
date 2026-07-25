import logging

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from core.models import ContactImportCandidate, Property
from intake.contact_classifier import classify_contact
from intake.models import QuoMessage
from messaging.services import _to_e164

logger = logging.getLogger(__name__)

# A contact's overall role/property association is a slower-moving signal
# than "does this one conversation need a ticket" — a wider window than
# intake/adapters/quo.py's RECENT_WINDOW_DAYS (45) makes sense here since
# we're profiling the person, not judging one thread.
RECENT_WINDOW_DAYS = 90
MIN_MESSAGES = 10
MAX_MESSAGES = 150


class Command(BaseCommand):
    help = (
        'AI-classifies PENDING Quo-sourced ContactImportCandidate rows by reading each contact\'s actual '
        'Quo message history (QuoMessage) — sets suggested_contact_type/suggested_property/trade so the '
        'review queue (/contacts/review/) starts pre-filled instead of blank. Skips any candidate with no '
        'message history to learn from (a saved Quo contact who has never actually texted the line) — '
        'nothing to classify from, so the crude import-time guess is left as-is.'
    )

    def handle(self, *args, **options):
        candidates = ContactImportCandidate.objects.filter(
            status=ContactImportCandidate.Status.PENDING, source='quo',
        )
        property_names = list(Property.objects.filter(is_active=True).values_list('name', flat=True))

        classified = 0
        skipped_no_history = 0
        for candidate in candidates:
            phone = _to_e164(candidate.phone)
            if not phone:
                skipped_no_history += 1
                continue

            messages = list(
                QuoMessage.objects.filter(Q(from_number=phone) | Q(to_number__icontains=phone))
                .order_by('quo_created_at')
            )
            if not messages:
                skipped_no_history += 1
                continue

            recent = self._recent_messages(messages)
            transcript = self._format_transcript(recent, total_count=len(messages))
            contact_info = f'Name: {candidate.name or "unknown"}'
            if candidate.raw_context:
                contact_info += f' — {candidate.raw_context}'

            verdict = classify_contact(transcript, contact_info=contact_info, property_names=property_names)
            if verdict is None:
                continue

            candidate.suggested_contact_type = verdict.contact_type
            if verdict.trade:
                candidate.trade = verdict.trade
            if verdict.property_name:
                candidate.suggested_property = Property.objects.filter(name=verdict.property_name).first()
            candidate.raw_context = (
                (candidate.raw_context + '\n\n' if candidate.raw_context else '')
                + f'[AI classification, {len(messages)} message(s)]: {verdict.reasoning}'
            )
            candidate.save(update_fields=['suggested_contact_type', 'trade', 'suggested_property', 'raw_context'])
            classified += 1
            self.stdout.write(
                f'{candidate.name or candidate.phone}: {verdict.contact_type} / {verdict.property_name or "no property"}'
            )

        self.stdout.write(self.style.SUCCESS(
            f'Classified {classified} contact(s), skipped {skipped_no_history} with no message history.'
        ))

    def _recent_messages(self, messages):
        if len(messages) <= MIN_MESSAGES:
            return messages
        cutoff = timezone.now() - timezone.timedelta(days=RECENT_WINDOW_DAYS)
        recent = [m for m in messages if (m.quo_created_at or timezone.now()) >= cutoff]
        if len(recent) < MIN_MESSAGES:
            recent = messages[-MIN_MESSAGES:]
        return recent[-MAX_MESSAGES:]

    def _format_transcript(self, messages, total_count):
        lines = []
        if total_count > len(messages):
            lines.append(
                f'[Showing the most recent {len(messages)} of {total_count} total messages — '
                f'older history omitted.]'
            )
        lines.append('')
        for m in messages:
            speaker = 'Staff (Quo line)' if m.direction == QuoMessage.Direction.OUT else (m.from_number or 'Contact')
            timestamp = m.quo_created_at.isoformat() if m.quo_created_at else ''
            lines.append(f'[{timestamp}] {speaker}: {m.body}')
        return '\n'.join(lines)
