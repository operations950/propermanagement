"""One click, one posting: a bank line that is dealt with once cannot be dealt with again, however the second request arrives."""
from datetime import date
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import Unit

from . import bankrec, charges, ledger
from .models import BankAccount, BankLine, BankStatement, Charge, JournalEntry, Receipt
from .test_bank_accounts import STORAGES
from .tests import make_assoc, owner_of


@override_settings(STORAGES=STORAGES)
class DoubleSubmitBase(TestCase):
    def setUp(self):
        self.prop = make_assoc('Guarded Condos')
        self.bank = BankAccount.objects.create(association=self.prop, name='Operating', bank_name='Truist', last4='5100', account=ledger.account(self.prop, '1110'))
        self.stmt = BankStatement.objects.create(bank_account=self.bank, period_end=date(2026, 10, 30), end_balance=D('0'))
        self.fee = BankLine.objects.create(statement=self.stmt, date=date(2026, 10, 27), description='CHECK 118', amount=D('-7571.58'), kind='check')
        self.deposit = BankLine.objects.create(statement=self.stmt, date=date(2026, 10, 5), description='DEPOSIT', amount=D('615.00'), kind='deposit')
        self.expense = ledger.account(self.prop, '6894')
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))

    def entries_for(self, line):
        return JournalEntry.objects.filter(association=self.prop, source_ref=f'bankline:{line.pk}')


class AddToBooksOnlyOnceTests(DoubleSubmitBase):
    def test_a_second_call_with_the_line_as_it_was_first_read_posts_nothing(self):
        stale = BankLine.objects.get(pk=self.fee.pk)                       # the second request read the line before the first one finished
        bankrec.add_to_books(self.fee, self.expense, None)
        with self.assertRaises(bankrec.StatementError) as ctx:
            bankrec.add_to_books(stale, self.expense, None)
        self.assertIn('already', str(ctx.exception))
        self.assertEqual(self.entries_for(self.fee).count(), 1)

    def test_a_live_entry_for_the_line_with_no_match_still_stops_a_second_one(self):
        ledger.post_entry(self.prop, date(2026, 10, 27), [{'account': ledger.account(self.prop, '1110'), 'credit': D('7571.58')}, {'account': self.expense, 'debit': D('7571.58')}],
                          memo='CHECK', source=JournalEntry.Source.BANK, source_ref=f'bankline:{self.fee.pk}')
        with self.assertRaises(bankrec.StatementError):
            bankrec.add_to_books(self.fee, self.expense, None)
        self.assertEqual(self.entries_for(self.fee).count(), 1)

    def test_an_entry_that_was_reversed_does_not_block_doing_it_again(self):
        first = bankrec.add_to_books(self.fee, self.expense, None)
        bankrec.unmatch(first)
        entry = self.entries_for(self.fee).get()
        ledger.reverse_entry(entry, date(2026, 10, 27))
        bankrec.add_to_books(BankLine.objects.get(pk=self.fee.pk), self.expense, None)
        live = self.entries_for(self.fee).filter(reverses__isnull=True).exclude(reversed_by__isnull=False)
        self.assertEqual(live.count(), 1)

    def test_the_screen_answers_a_repeated_post_with_a_message_and_one_entry(self):
        url = reverse('accounting:bank', args=[self.prop.pk, self.bank.pk, '2026-10'])
        data = {'action': 'add_to_books', 'bank_line': self.fee.pk, 'account': self.expense.pk}
        self.client.post(url, data)
        response = self.client.post(url, data, follow=True)
        self.assertIn('already', ' '.join(str(m) for m in response.context['messages']))
        self.assertEqual(self.entries_for(self.fee).count(), 1)


class OtherActionsOnlyOnceTests(DoubleSubmitBase):
    def test_an_owners_payment_from_a_line_is_recorded_once(self):
        unit = Unit.objects.filter(property=self.prop).first()
        owner = owner_of(self.prop, unit)
        charges.post_charge(self.prop, unit, owner, Charge.Kind.ASSESSMENT, 'Monthly assessment', date(2026, 10, 1), D('615'))
        stale = BankLine.objects.get(pk=self.deposit.pk)
        bankrec.owner_payment_from_line(self.deposit, unit, owner)
        with self.assertRaises(bankrec.StatementError):
            bankrec.owner_payment_from_line(stale, unit, owner)
        self.assertEqual(Receipt.objects.filter(unit=unit).count(), 1)

    def test_sorting_a_deposit_twice_posts_it_once(self):
        stale = BankLine.objects.get(pk=self.deposit.pk)
        other = {'account': ledger.account(self.prop, '4700'), 'amount': D('615.00'), 'memo': 'Refund'}
        bankrec.sort_deposit(self.deposit, [], [other], [])
        with self.assertRaises(bankrec.StatementError):
            bankrec.sort_deposit(stale, [], [other], [])
        self.assertEqual(self.entries_for(self.deposit).count(), 1)

    def test_clearing_the_same_ticked_line_twice_clears_it_once(self):
        stale_ids = [self.fee.pk]
        bankrec.clear_selected(self.bank, date(2026, 10, 1), stale_ids, [], account=self.expense)
        with self.assertRaises(bankrec.StatementError):
            bankrec.clear_selected(self.bank, date(2026, 10, 1), stale_ids, [], account=self.expense)
        self.assertEqual(JournalEntry.objects.filter(association=self.prop, source_ref='reconciling:2026-10').count(), 1)

    def test_matching_a_line_by_hand_twice_is_refused(self):
        bankrec.add_to_books(self.fee, self.expense, None)
        with self.assertRaises(bankrec.StatementError):
            bankrec.match_manual(self.bank, date(2026, 10, 1), [self.fee.pk], ['L1'])


class TheScreensCarryTheGuardTests(DoubleSubmitBase):
    def test_both_screens_load_the_script_that_stops_a_second_send(self):
        for name in ('bank', 'deposits'):
            html = self.client.get(reverse(f'accounting:{name}', args=[self.prop.pk, self.bank.pk, '2026-10'])).content.decode()
            self.assertIn('window.acSubmit', html)
            self.assertIn('Working', html)
