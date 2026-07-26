import logging

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Contact
from intake.adapters.quo import QuoAdapter
from intake.models import QuoThreadState
from intake.quo_contact_activity import build_text_activity_map, list_our_phone_numbers
from messaging.services import _to_e164

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Ensures every Contact with a phone number has a QuoThreadState row for their most recent "
        "Quo conversation, matched purely by phone number against one global conversation crawl — "
        "independent of whether the regular Quo poller's cursor/window ever happened to cover that "
        "thread. This is what lets fetch_quo_conversation (contact/property 'view conversation', "
        "ticket detail's Contractor Communication box before a ticket is bound to a conversation) "
        "actually find a contact's history instead of reporting 'no Quo thread' just because nothing "
        "recent enough surfaced it through the ticket-classification pipeline. Only ever get_or_create "
        "— never touches a conversation_id already tracked, so this can't re-trigger reclassification "
        "of a thread the real poller already knows about. Cheap and safe to run every couple hours "
        "(settings.QUO_CONTACT_LINK_INTERVAL_MINUTES); no-op until QUO_API_KEY is configured."
    )

    def handle(self, *args, **options):
        if not settings.QUO_API_KEY:
            self.stdout.write(self.style.WARNING('QUO_API_KEY not set — nothing to link.'))
            return

        adapter = QuoAdapter()
        _, our_number_phones = list_our_phone_numbers(adapter)
        text_activity = build_text_activity_map(adapter, our_number_phones)

        known_conversation_ids = set(QuoThreadState.objects.values_list('conversation_id', flat=True))

        linked = skipped_already_tracked = skipped_no_activity = 0
        for contact in Contact.objects.exclude(phone=''):
            phone_key = _to_e164(contact.phone)
            if not phone_key:
                continue
            info = text_activity.get(phone_key)
            if not info:
                skipped_no_activity += 1
                continue
            if info['conversation_id'] in known_conversation_ids:
                skipped_already_tracked += 1
                continue
            # last_classified_at is set (rather than left null) purely to keep this row out of
            # the regular poller's retry bucket (QuoThreadState.last_classified_at__isnull=True)
            # — we're not actually running Claude over this thread here, just establishing the
            # phone_number_id linkage the history views need. A real new message afterward still
            # gets properly (re)classified through the normal pipeline, since last_message_id is
            # left blank and any later poll treats that as "always new."
            QuoThreadState.objects.get_or_create(
                conversation_id=info['conversation_id'],
                defaults={
                    'phone_number_id': info['phone_number_id'],
                    'participant': phone_key,
                    'last_classified_at': timezone.now(),
                },
            )
            known_conversation_ids.add(info['conversation_id'])
            linked += 1

        self.stdout.write(self.style.SUCCESS(
            f'Linked {linked} contact(s) to their Quo conversation, {skipped_already_tracked} already '
            f'tracked, {skipped_no_activity} with no Quo text history found.'
        ))
