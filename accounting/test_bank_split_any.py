"""Any statement line can be split between accounts, and the split is remembered for next month."""
import json
from datetime import date
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from . import bankrec, ledger
from .models import Account, BankAccount, BankLine, BankSplitTemplate, BankStatement, JournalEntry, ReconMatch
from .test_bank_accounts import STORAGES
from .tests import make_assoc

OCT, NOV = date(2026, 10, 31), date(2026, 11, 30)
LOAN = '#Automatic Ln Paymt AUTOMATIC LOAN PAY'


@override_settings(STORAGES=STORAGES)
class SplitBase(TestCase):
    def setUp(self):
        self.prop = make_assoc('Castlewood Test')
        self.bank = BankAccount.objects.create(association=self.prop, name='Wood 8021', bank_name='Seacoast', last4='8021', kind='reserve', account=ledger.account(self.prop, '1130'))
        self.operating = BankAccount.objects.create(association=self.prop, name='Wood 1101', bank_name='Seacoast', last4='1101', account=ledger.account(self.prop, '1110'))
        self.stmt = BankStatement.objects.create(bank_account=self.bank, period_end=OCT, end_balance=D('0'))
        self.principal, self.interest = ledger.account(self.prop, '2311'), ledger.account(self.prop, '6611')
        self.user = get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678')
        self.client.force_login(self.user)
        self.url = reverse('accounting:bank', args=[self.prop.pk, self.bank.pk, '2026-10'])

    def line(self, amount, desc=LOAN, day=date(2026, 10, 13), stmt=None, kind='withdrawal'):
        return BankLine.objects.create(statement=stmt or self.stmt, date=day, description=desc, amount=D(amount), kind=kind)

    def loan_rows(self, interest='2322.03'):
        return [{'account': self.principal, 'amount': None, 'rest': True}, {'account': self.interest, 'amount': D(interest)}]


class SplitAPaymentTests(SplitBase):
    def test_a_loan_payment_is_split_into_principal_and_interest_and_matched(self):
        bl = self.line('-6554.76')
        bankrec.split_line(bl, self.loan_rows(), self.user)
        entry = JournalEntry.objects.get(source_ref=f'bankline:{bl.pk}')
        got = sorted((l.account.code, l.debit, l.credit) for l in entry.lines.all())
        self.assertEqual(got, [('1130', D('0.00'), D('6554.76')), ('2311', D('4232.73'), D('0.00')), ('6611', D('2322.03'), D('0.00'))])
        bl.refresh_from_db()
        self.assertEqual(bl.match.how, 'split between accounts')
        self.assertEqual(entry.date, date(2026, 10, 13))

    def test_the_cash_side_is_the_bank_accounts_own_cash_account(self):
        bl = self.line('-100.00')
        bankrec.split_line(bl, [{'account': self.interest, 'amount': D('60.00')}, {'account': ledger.account(self.prop, '6510'), 'amount': D('40.00')}], self.user)
        entry = JournalEntry.objects.get(source_ref=f'bankline:{bl.pk}')
        self.assertEqual(entry.lines.get(credit=D('100.00')).account.code, '1130')

    def test_amounts_that_do_not_add_up_are_refused_with_the_difference(self):
        bl = self.line('-100.00')
        with self.assertRaises(bankrec.StatementError) as e:
            bankrec.split_line(bl, [{'account': self.principal, 'amount': D('60.00')}, {'account': self.interest, 'amount': D('30.00')}], self.user)
        self.assertIn('$10.00 left to place', str(e.exception))
        with self.assertRaises(bankrec.StatementError) as e:
            bankrec.split_line(bl, [{'account': self.principal, 'amount': D('90.00')}, {'account': self.interest, 'amount': D('30.00')}], self.user)
        self.assertIn('too much', str(e.exception))
        self.assertFalse(JournalEntry.objects.filter(source_ref=f'bankline:{bl.pk}').exists())

    def test_a_split_needs_two_accounts_and_never_the_bank_account_itself(self):
        bl = self.line('-100.00')
        with self.assertRaises(bankrec.StatementError):
            bankrec.split_line(bl, [{'account': self.principal, 'amount': D('100.00')}], self.user)
        with self.assertRaises(bankrec.StatementError):
            bankrec.split_line(bl, [{'account': self.bank.account, 'amount': D('50.00')}, {'account': self.interest, 'amount': D('50.00')}], self.user)

    def test_the_rest_cannot_be_zero_or_below(self):
        bl = self.line('-100.00')
        with self.assertRaises(bankrec.StatementError):
            bankrec.split_line(bl, [{'account': self.principal, 'amount': None, 'rest': True}, {'account': self.interest, 'amount': D('100.00')}], self.user)

    def test_every_kind_of_account_may_be_a_part_of_a_split(self):
        bl = self.line('-300.00')
        income = ledger.account(self.prop, '4700')
        bankrec.split_line(bl, [{'account': self.principal, 'amount': D('200.00')}, {'account': self.interest, 'amount': D('150.00')}, {'account': income, 'amount': D('-50.00')}], self.user)
        entry = JournalEntry.objects.get(source_ref=f'bankline:{bl.pk}')
        self.assertEqual({l.account.code: (l.debit, l.credit) for l in entry.lines.all()}, {'1130': (D('0.00'), D('300.00')), '2311': (D('200.00'), D('0.00')), '6611': (D('150.00'), D('0.00')), '4700': (D('0.00'), D('50.00'))})

    def test_a_line_already_matched_cannot_be_split_again(self):
        bl = self.line('-6554.76')
        bankrec.split_line(bl, self.loan_rows(), self.user)
        with self.assertRaises(bankrec.StatementError):
            bankrec.split_line(bl, self.loan_rows(), self.user)
        self.assertEqual(JournalEntry.objects.filter(source_ref=f'bankline:{bl.pk}').count(), 1)


class SplitADepositTests(SplitBase):
    def test_a_deposit_is_split_between_income_and_a_liability_with_cash_debited(self):
        bl = self.line('1000.00', desc='Deposit', kind='deposit')
        bankrec.split_line(bl, [{'account': ledger.account(self.prop, '4700'), 'amount': D('700.00')}, {'account': ledger.account(self.prop, '2250'), 'amount': D('300.00')}], self.user)
        entry = JournalEntry.objects.get(source_ref=f'bankline:{bl.pk}')
        self.assertEqual({l.account.code: (l.debit, l.credit) for l in entry.lines.all()}, {'1130': (D('1000.00'), D('0.00')), '4700': (D('0.00'), D('700.00')), '2250': (D('0.00'), D('300.00'))})


class TheScreenTests(SplitBase):
    def test_every_unmatched_line_offers_a_split_payments_and_deposits_alike(self):
        pay = self.line('-50.00', desc='Some fee')
        dep = self.line('80.00', desc='Some deposit', kind='deposit')
        html = self.client.get(self.url).content.decode()
        self.assertEqual(html.count('data-ub-pick="split">'), 2)
        self.assertIn('Split between accounts', html)
        self.assertIn('id="split-accounts"', html)
        self.assertIn(f'name="bank_line" value="{pay.pk}"', html)
        self.assertIn(f'name="bank_line" value="{dep.pk}"', html)

    def test_the_form_posts_one_entry_and_remembers_it(self):
        bl = self.line('-6554.76')
        r = self.client.post(self.url, {'action': 'split_line', 'bank_line': bl.pk, 'split_account_1': self.principal.pk, 'split_rest': '1', 'split_amount_1': '',
                                        'split_account_2': self.interest.pk, 'split_amount_2': '2,322.03', 'split_memo_2': 'Interest', 'remember': '1'}, follow=True)
        self.assertContains(r, 'Split between the accounts and matched')
        bl.refresh_from_db()
        self.assertIsNotNone(bl.match_id)
        tpl = BankSplitTemplate.objects.get(bank_account=self.bank)
        self.assertEqual(tpl.words, 'automatic ln paymt automatic loan pay')
        self.assertEqual([(l['account'], l['rest']) for l in tpl.lines], [(self.principal.pk, True), (self.interest.pk, False)])

    def test_an_error_is_a_plain_message_and_nothing_is_posted(self):
        bl = self.line('-100.00')
        r = self.client.post(self.url, {'action': 'split_line', 'bank_line': bl.pk, 'split_account_1': self.principal.pk, 'split_amount_1': '10', 'split_account_2': self.interest.pk, 'split_amount_2': '10'}, follow=True)
        self.assertContains(r, 'left to place')
        self.assertFalse(JournalEntry.objects.filter(source_ref=f'bankline:{bl.pk}').exists())

    def test_the_split_can_be_posted_without_being_remembered(self):
        bl = self.line('-100.00')
        self.client.post(self.url, {'action': 'split_line', 'bank_line': bl.pk, 'split_account_1': self.principal.pk, 'split_amount_1': '60', 'split_account_2': self.interest.pk, 'split_amount_2': '40'})
        self.assertFalse(BankSplitTemplate.objects.exists())

    def test_another_associations_account_is_refused(self):
        other = make_assoc('Other Condos')
        bl = self.line('-100.00')
        r = self.client.post(self.url, {'action': 'split_line', 'bank_line': bl.pk, 'split_account_1': Account.objects.filter(association=other, code='2311').first().pk, 'split_amount_1': '60',
                                        'split_account_2': self.interest.pk, 'split_amount_2': '40'})
        self.assertEqual(r.status_code, 404)
        self.assertFalse(JournalEntry.objects.filter(source_ref=f'bankline:{bl.pk}').exists())


class RememberTheSplitTests(SplitBase):
    def next_month(self, amount='-6554.76', interest=None):
        stmt = BankStatement.objects.get_or_create(bank_account=self.bank, period_end=NOV, defaults={'end_balance': D('0')})[0]
        return self.line(amount, desc='#Automatic Ln Paymt AUTOMATIC LOAN PAY 1126', day=date(2026, 11, 13), stmt=stmt)

    def test_next_month_the_same_words_come_up_with_the_same_split_and_the_rest_recalculated(self):
        bankrec.split_line(self.line('-6554.76'), self.loan_rows(), self.user)
        nov = self.next_month('-6554.76')
        got = bankrec.suggested_splits([nov])[nov.pk]['rows']
        self.assertEqual([(r['account'].code, r['amount'], r['rest']) for r in got], [('2311', D('4232.73'), True), ('6611', D('2322.03'), False)])
        # a payment of a different amount: interest stays at last month's figure and the principal is whatever is left
        nov2 = self.next_month('-6540.00')
        got = bankrec.suggested_splits([nov2])[nov2.pk]['rows']
        self.assertEqual([r['amount'] for r in got], [D('4217.97'), D('2322.03')])

    def test_only_what_changed_has_to_be_typed_and_one_press_posts_it(self):
        bankrec.split_line(self.line('-6554.76'), self.loan_rows(), self.user)
        nov = self.next_month('-6554.76')
        nov_url = reverse('accounting:bank', args=[self.prop.pk, self.bank.pk, '2026-11'])
        html = self.client.get(nov_url).content.decode()
        self.assertIn('Same split as last month', html)
        rows = json.loads(html.split('data-rows="')[1].split('"')[0].replace('&quot;', '"'))
        self.assertEqual([r['rest'] for r in rows], [True, False])
        # interest is $2,300.00 this month: type it, the principal follows
        self.client.post(nov_url, {'action': 'split_line', 'bank_line': nov.pk, 'split_account_1': self.principal.pk, 'split_rest': '1', 'split_account_2': self.interest.pk, 'split_amount_2': '2300.00', 'remember': '1'})
        entry = JournalEntry.objects.get(source_ref=f'bankline:{nov.pk}')
        self.assertEqual({l.account.code: l.debit for l in entry.lines.all() if l.debit}, {'2311': D('4254.76'), '6611': D('2300.00')})
        tpl = BankSplitTemplate.objects.get(bank_account=self.bank)
        self.assertEqual(tpl.used_count, 2)
        self.assertEqual(tpl.lines[1]['amount'], '2300.00')                      # next month starts from this month's interest

    def test_nothing_is_posted_by_a_suggestion(self):
        bankrec.split_line(self.line('-6554.76'), self.loan_rows(), self.user)
        nov = self.next_month()
        self.client.get(reverse('accounting:bank', args=[self.prop.pk, self.bank.pk, '2026-11']))
        self.assertFalse(JournalEntry.objects.filter(source_ref=f'bankline:{nov.pk}').exists())

    def test_a_deposit_with_the_same_words_is_not_given_a_payments_split(self):
        bankrec.split_line(self.line('-6554.76'), self.loan_rows(), self.user)
        stmt = BankStatement.objects.get_or_create(bank_account=self.bank, period_end=NOV, defaults={'end_balance': D('0')})[0]
        dep = self.line('6554.76', desc=LOAN, day=date(2026, 11, 13), stmt=stmt, kind='deposit')
        self.assertEqual(bankrec.suggested_splits([dep]), {})

    def test_another_bank_accounts_lines_do_not_use_it(self):
        bankrec.split_line(self.line('-6554.76'), self.loan_rows(), self.user)
        stmt = BankStatement.objects.create(bank_account=self.operating, period_end=NOV, end_balance=D('0'))
        other = BankLine.objects.create(statement=stmt, date=date(2026, 11, 13), description=LOAN, amount=D('-6554.76'), kind='withdrawal')
        self.assertEqual(bankrec.suggested_splits([other]), {})

    def test_a_split_does_not_teach_the_one_account_guess(self):
        bankrec.split_line(self.line('-6554.76'), self.loan_rows(), self.user)
        nov = self.next_month()
        accounts = list(self.prop.accounts.filter(is_active=True))
        self.assertEqual(bankrec.suggested_accounts([nov], accounts), {})

    def test_a_remembered_account_that_was_switched_off_is_not_suggested(self):
        bankrec.split_line(self.line('-6554.76'), self.loan_rows(), self.user)
        Account.objects.filter(pk=self.interest.pk).update(is_active=False)
        nov = self.next_month()
        self.assertEqual(bankrec.suggested_splits([nov]), {})
