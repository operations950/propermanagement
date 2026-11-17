"""Sorting deposits: the owner list that opens on its own leaves out $0 balances (a search and "show all" bring them back), and the page always has room under its last card."""
from datetime import date
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import BankLine, BankStatement
from .test_budget_sections import STORAGES
from .tests import bank_for, make_assoc


@override_settings(STORAGES=STORAGES)
class DepositsPickerTests(TestCase):
    def setUp(self):
        self.prop = make_assoc('Picker Condos')
        bank = bank_for(self.prop)
        st = BankStatement.objects.create(bank_account=bank, period_end=date(2026, 10, 30), end_balance=D('1000'))
        BankLine.objects.create(statement=st, date=date(2026, 10, 5), description='DEPOSIT', amount=D('615'), kind='deposit')
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.html = self.client.get(reverse('accounting:deposits', args=[self.prop.pk, bank.pk, '2026-10'])).content.decode()

    def test_the_list_that_opens_leaves_out_zero_balances_and_a_search_or_show_all_brings_them_back(self):
        self.assertIn('return s.showAll || u.owesC !== 0 || on[u.id];', self.html)           # no search, not show all: only owners with a balance, and anyone already ticked
        self.assertIn('if (q) return (u.label + \' \' + u.owner).toLowerCase().indexOf(q) >= 0;', self.html)   # a search looks at everyone
        self.assertIn('data-act="showall"', self.html)
        self.assertIn('with a $0 balance', self.html)                                           # says how many are not listed

    def test_there_is_always_room_under_the_last_card_for_a_list_or_dropdown(self):
        self.assertIn('height: 45vh;', self.html)
