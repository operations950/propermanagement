import logging

from django.core.management.base import BaseCommand
from django.db.models import Max
from django.utils import timezone

from intake.adapters.base import RawEvent
from intake.adapters.quo import MAX_MESSAGES, MIN_MESSAGES, RECENT_WINDOW_DAYS, QuoAdapter
from intake.classifier import _handle_quo_thread
from intake.models import QuoMessage, QuoThreadState

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        'Periodic AI classification pass (see proptasks/scheduler.py — runs every '
        'QUO_CLASSIFY_INTERVAL_MINUTES, default 2 hours). Finds every Quo conversation with local '
        'message activity newer than its last classification and asks Claude whether it now needs a '
        'ticket, reading the transcript from QuoMessage (kept live by the message webhook) instead of '
        'a live Quo API call — decoupling "capture" (real-time, via webhook) from "decide" (periodic, '
        'so a conversation gets a chance to develop before being judged actionable).'
    )

    def handle(self, *args, **options):
        latest_by_conversation = dict(
            QuoMessage.objects.exclude(conversation_id='')
            .values_list('conversation_id')
            .annotate(latest=Max('quo_created_at'))
            .values_list('conversation_id', 'latest')
        )
        known_state = {s.conversation_id: s for s in QuoThreadState.objects.all()}

        to_classify = [
            conversation_id for conversation_id, latest in latest_by_conversation.items()
            if not (state := known_state.get(conversation_id)) or not state.last_classified_at
            or (latest and latest > state.last_classified_at)
        ]
        self.stdout.write(f'{len(to_classify)} conversation(s) need (re)classification.')

        contact_lookup = QuoAdapter()._build_contact_lookup()

        processed = 0
        for conversation_id in to_classify:
            try:
                if self._classify_one(conversation_id, contact_lookup):
                    processed += 1
            except Exception:
                logger.exception('Quo classify: failed for conversation %s', conversation_id)

        self.stdout.write(self.style.SUCCESS(f'Processed {processed}/{len(to_classify)} conversation(s).'))

    def _classify_one(self, conversation_id, contact_lookup):
        messages = list(QuoMessage.objects.filter(conversation_id=conversation_id).order_by('quo_created_at'))
        if not messages:
            return False

        recent = self._recent_messages(messages)
        participant = self._participant(messages)
        contact = contact_lookup.get(participant)
        transcript = self._format_transcript(recent, contact, participant, total_count=len(messages))

        latest = messages[-1]
        event = RawEvent(
            event_type='quo_thread', source='quo', external_id=conversation_id,
            body=transcript, reporter_phone=participant,
            reporter_name=(contact.get('name') or '') if contact else '',
            extra={
                'phone_number_id': latest.phone_number_id, 'latest_message_id': latest.message_id,
                'is_known_contact': bool(contact), 'contact_company': (contact.get('company') or '') if contact else '',
            },
        )
        _handle_quo_thread(event)
        return True

    def _participant(self, messages):
        for m in reversed(messages):
            if m.direction == QuoMessage.Direction.IN and m.from_number:
                return m.from_number
            if m.direction == QuoMessage.Direction.OUT and m.to_number:
                return m.to_number.split(',')[0]
        return ''

    def _recent_messages(self, messages):
        if len(messages) <= MIN_MESSAGES:
            return messages
        cutoff = timezone.now() - timezone.timedelta(days=RECENT_WINDOW_DAYS)
        recent = [m for m in messages if (m.quo_created_at or timezone.now()) >= cutoff]
        if len(recent) < MIN_MESSAGES:
            recent = messages[-MIN_MESSAGES:]
        return recent[-MAX_MESSAGES:]

    def _format_transcript(self, messages, contact, participant, total_count):
        lines = []
        if total_count > len(messages):
            lines.append(
                f'[Showing the most recent {len(messages)} of {total_count} total messages in this '
                f'conversation — older history omitted.]'
            )
        if contact and (contact.get('name') or contact.get('company')):
            label = contact.get('name') or ''
            if contact.get('company'):
                label = f"{label} ({contact['company']})" if label else contact['company']
            lines.append(f'[Caller: {label} — saved Quo contact, {participant}]')
        else:
            lines.append(f'[Caller: {participant} — NOT a saved contact in Quo. Treat with extra scrutiny.]')
        lines.append('')
        for m in messages:
            speaker = 'Staff (Quo line)' if m.direction == QuoMessage.Direction.OUT else (m.from_number or 'Caller')
            timestamp = m.quo_created_at.isoformat() if m.quo_created_at else ''
            lines.append(f'[{timestamp}] {speaker}: {m.body}')
        return '\n'.join(lines)
