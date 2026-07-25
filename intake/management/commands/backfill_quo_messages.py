import logging
import time

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from intake.adapters.quo import QuoAdapter
from intake.models import QuoMessage, QuoThreadState

logger = logging.getLogger(__name__)


def _parse_iso(ts):
    if not ts:
        return None
    dt = parse_datetime(ts)
    if dt and timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt


class Command(BaseCommand):
    help = (
        'One-time (but safe to re-run — idempotent on message_id) historical backfill: pulls every '
        'Quo conversation and its full message history into QuoMessage/QuoThreadState, so '
        'classify_quo_conversations has a real baseline to work from instead of only what arrives '
        'after the message webhook went live.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--updated-after', default=None,
            help='ISO datetime — only backfill conversations updated after this. Default: all history.',
        )

    def handle(self, *args, **options):
        adapter = QuoAdapter()
        conversations = adapter._list_conversations(updated_after=options.get('updated_after'))
        self.stdout.write(f'Found {len(conversations)} conversation(s) to backfill.')

        total_messages = 0
        for i, convo in enumerate(conversations, start=1):
            conversation_id = convo['id']
            phone_number_id = convo.get('phoneNumberId', '')
            participants = convo.get('participants') or []
            if not participants:
                continue
            participant = participants[0]

            try:
                messages = adapter._list_messages(phone_number_id, participant)
            except Exception:
                logger.exception('Backfill: failed to list messages for conversation %s', conversation_id)
                continue

            for m in messages:
                direction = QuoMessage.Direction.IN if m.get('direction') == 'incoming' else QuoMessage.Direction.OUT
                QuoMessage.objects.get_or_create(message_id=m.get('id', ''), defaults={
                    'conversation_id': conversation_id,
                    'phone_number_id': phone_number_id,
                    'direction': direction,
                    'from_number': m.get('from', '') or '',
                    'to_number': ','.join(m.get('to') or []),
                    'body': m.get('text', ''),
                    'quo_created_at': _parse_iso(m.get('createdAt', '')),
                })
            total_messages += len(messages)

            if messages:
                QuoThreadState.objects.update_or_create(
                    conversation_id=conversation_id,
                    defaults={
                        'phone_number_id': phone_number_id, 'participant': participant,
                        'last_message_id': messages[-1].get('id', ''),
                    },
                )

            self.stdout.write(f'[{i}/{len(conversations)}] {conversation_id}: {len(messages)} message(s)')
            time.sleep(0.15)  # stay comfortably under Quo's 10 req/sec rate limit

        self.stdout.write(self.style.SUCCESS(
            f'Backfilled {total_messages} message(s) across {len(conversations)} conversation(s).'
        ))
