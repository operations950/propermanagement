"""The Close associations list builds a full close sheet for every association: each card is kept for a short time, and anything saved to the books drops them all."""
from datetime import date
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings

from . import closing, ledger
from .models import CloseStepDone
from .tests import make_assoc

MONTH = date(2026, 10, 1)


@override_settings(CLOSE_CARD_CACHE_SECONDS=45)
class TheCardsAreKeptBriefly(TestCase):
    def setUp(self):
        cache.clear()
        self.prop = make_assoc('Card Condos')

    def build(self):
        with mock.patch.object(closing, 'association_card_uncached', wraps=closing.association_card_uncached) as inner:
            card = closing.association_card(self.prop, MONTH)
        return card, inner.call_count

    def test_a_second_look_does_not_build_the_close_sheet_again(self):
        first, n1 = self.build()
        second, n2 = self.build()
        self.assertEqual((n1, n2), (1, 0))
        self.assertEqual(first, second)

    def test_each_caller_gets_its_own_copy(self):
        first, _ = self.build()
        first['segments'].append('x')
        second, _ = self.build()
        self.assertNotIn('x', second['segments'])

    def test_saving_something_to_the_books_drops_the_cards(self):
        self.build()
        CloseStepDone.objects.create(association=self.prop, month=MONTH, key='charges')
        _, n = self.build()
        self.assertEqual(n, 1)

    def test_a_posted_entry_drops_the_cards(self):
        self.build()
        ledger.post_entry(self.prop, date(2026, 10, 5), [{'account': ledger.account(self.prop, '1110'), 'debit': 5, 'credit': 0}, {'account': ledger.account(self.prop, '4500'), 'debit': 0, 'credit': 5}], memo='x')
        _, n = self.build()
        self.assertEqual(n, 1)

    @override_settings(CLOSE_CARD_CACHE_SECONDS=0)
    def test_with_the_setting_at_zero_every_look_builds_it(self):
        self.build()
        _, n = self.build()
        self.assertEqual(n, 1)
