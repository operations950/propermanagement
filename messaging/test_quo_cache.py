"""The live Quo conversation fetch is a real HTTP round-trip, asked for on every ticket page load and every 20 seconds while it is open: what was just fetched is kept for a few seconds,
and sending a message clears it."""
from types import SimpleNamespace
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings

from core.models import Contact
from intake.models import QuoThreadState

from . import services


@override_settings(QUO_CONVERSATION_CACHE_SECONDS=30, QUO_API_KEY='k', QUO_DEFAULT_FROM_NUMBER='+15615550100')
class TheConversationIsKeptBriefly(TestCase):
    def setUp(self):
        cache.clear()
        self.contact = Contact.objects.create(name='Pat Vendor', phone='561-555-0111')
        QuoThreadState.objects.create(conversation_id='c1', phone_number_id='pn1', participant='+15615550111')
        self.msgs = [{'direction': 'incoming', 'text': 'On my way', 'createdAt': '2026-11-18T10:00:00Z'}]

    def fetch(self):
        with mock.patch('intake.adapters.quo.QuoAdapter._list_messages', side_effect=lambda *a, **k: list(self.msgs)) as m:
            return services.fetch_quo_conversation(self.contact), m

    def test_a_second_look_does_not_call_quo_again(self):
        first, call1 = self.fetch()
        second, call2 = self.fetch()
        self.assertEqual(call1.call_count, 1)
        self.assertEqual(call2.call_count, 0)
        self.assertEqual(first, second)
        self.assertEqual(first[0]['body'], 'On my way')

    def test_sending_a_message_clears_it_so_the_next_look_is_fresh(self):
        self.fetch()
        self.msgs.append({'direction': 'outgoing', 'text': 'Thanks', 'createdAt': '2026-11-18T10:01:00Z'})
        with mock.patch('requests.post', return_value=SimpleNamespace(raise_for_status=lambda: None, json=lambda: {})), mock.patch.object(services, '_record_sent_message'), mock.patch.object(services, '_quo_from_number', return_value='+15615550100'):
            services.send_via_quo('561-555-0111', 'Thanks')
        again, call = self.fetch()
        self.assertEqual(call.call_count, 1)
        self.assertEqual([m['body'] for m in again], ['On my way', 'Thanks'])

    def test_with_the_setting_at_zero_nothing_is_kept(self):
        with override_settings(QUO_CONVERSATION_CACHE_SECONDS=0):
            self.fetch()
            _, call = self.fetch()
        self.assertEqual(call.call_count, 1)
