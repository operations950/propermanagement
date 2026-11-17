from datetime import date
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from . import bankrec, ledger
from .models import Account, BankLine, BankStatement, JournalEntry, ReconMatch
from .tests import bank_for, make_assoc

STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}


def line(statement, day, text, amount, kind):
    return BankLine.objects.create(statement=statement, date=day, description=text, amount=D(amount), kind=kind)


class SuggestedAccountTests(TestCase):
    """An unmatched bank line is offered the account its words were put to before: only an offer, nothing is posted until the button is pressed."""

    def setUp(self):
        self.prop = make_assoc()
        self.bank = bank_for(self.prop)
        self.accounts = list(self.prop.accounts.filter(is_active=True).order_by('code'))
        self.earlier = BankStatement.objects.create(bank_account=self.bank, period_end=date(2026, 2, 28), end_balance=D('0'))
        self.now = BankStatement.objects.create(bank_account=self.bank, period_end=date(2026, 3, 31), end_balance=D('0'))
        self.interest = ledger.account(self.prop, '4511')

    def test_the_same_words_go_where_they_went_last_time(self):
        first = line(self.earlier, date(2026, 2, 27), 'Interest Paid', '12.50', 'interest')
        bankrec.add_to_books(first, self.interest)
        again = line(self.now, date(2026, 3, 31), 'INTEREST PAID 0331', '3.10', 'interest')        # numbers and capitals do not matter
        self.assertEqual(bankrec.suggested_accounts([again], self.accounts), {again.pk: self.interest})

    def test_money_going_out_is_not_offered_the_account_money_came_in_to(self):
        bankrec.add_to_books(line(self.earlier, date(2026, 2, 27), 'Interest Paid', '12.50', 'interest'), self.interest)
        out = line(self.now, date(2026, 3, 31), 'Interest Paid', '-3.10', 'withdrawal')
        self.assertEqual(bankrec.suggested_accounts([out], self.accounts), {})

    def test_words_never_seen_before_get_no_suggestion(self):
        stranger = line(self.now, date(2026, 3, 9), 'ACH GARDEN CO', '-250', 'withdrawal')
        self.assertEqual(bankrec.suggested_accounts([stranger], self.accounts), {})

    def test_a_bank_fee_is_offered_bank_fees(self):
        fee = line(self.now, date(2026, 3, 31), 'MONTHLY SERVICE FEE', '-15', 'fee')
        self.assertEqual(bankrec.suggested_accounts([fee], self.accounts), {fee.pk: ledger.account(self.prop, '6510')})

    def test_a_reversed_posting_is_not_used(self):
        first = line(self.earlier, date(2026, 2, 27), 'Interest Paid', '12.50', 'interest')
        bankrec.add_to_books(first, self.interest)
        entry = JournalEntry.objects.get(source=JournalEntry.Source.BANK)
        ledger.reverse_entry(entry, date(2026, 3, 1))
        again = line(self.now, date(2026, 3, 31), 'Interest Paid', '3.10', 'interest')
        self.assertEqual(bankrec.suggested_accounts([again], self.accounts), {})


@override_settings(STORAGES=STORAGES)
class UnmatchedLinesScreenTests(TestCase):
    def setUp(self):
        self.prop = make_assoc()
        self.bank = bank_for(self.prop)
        self.statement = BankStatement.objects.create(bank_account=self.bank, period_end=date(2026, 3, 31), end_balance=D('0'))
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.url = reverse('accounting:bank', args=[self.prop.pk, self.bank.pk, '2026-03'])

    def page(self):
        return self.client.get(self.url).content.decode()

    def test_a_deposit_with_no_guess_has_the_account_box_and_three_buttons_and_no_paid_to(self):
        line(self.statement, date(2026, 3, 12), 'DEPOSIT 8841', '615', 'deposit')
        html = self.page()
        self.assertIn('class="ub-top"', html)                                              # description, the three choices and the amount on one line
        self.assertIn('data-ub-pick="add">Add to books</button>', html)
        self.assertIn('data-ub-panel="add" hidden', html)                                  # the account box only appears when Add to books is clicked
        self.assertIn("data-ub-pick=\"owner\">Owner's payment</button>", html)
        self.assertIn('data-ub-pick="split">Split</button>', html)                          # one split button, not two
        self.assertNotIn('Split it', html)
        self.assertNotIn('Split between accounts', html)
        self.assertNotIn('Add to one account', html)                                       # redundant: the account box is on the line itself
        self.assertNotIn('Paid to', html)                                                  # nobody was paid: the money came in

    def test_a_payment_has_paid_to_a_split_and_no_owner_choice(self):
        line(self.statement, date(2026, 3, 12), 'ACH GARDEN CO', '-250', 'withdrawal')
        html = self.page()
        self.assertIn('Paid to', html)
        self.assertIn('data-ub-pick="split">Split</button>', html)              # a payment can always be split between accounts
        self.assertNotIn('Add to one account', html)
        self.assertNotIn('data-ub-pick="owner"', html)
        self.assertNotIn("Owner's payment", html)
        self.assertNotIn('Split it', html)

    def test_a_guess_is_already_chosen_in_the_account_box(self):
        earlier = BankStatement.objects.create(bank_account=self.bank, period_end=date(2026, 2, 28), end_balance=D('0'))
        interest = ledger.account(self.prop, '4511')
        bankrec.add_to_books(line(earlier, date(2026, 2, 27), 'Interest Paid', '12.50', 'interest'), interest)
        line(self.statement, date(2026, 3, 31), 'Interest Paid', '3.10', 'interest')
        html = self.page()
        self.assertIn(f'<option value="{interest.pk}" selected>', html)                  # the account the same words went to last month is chosen for you
        self.assertIn('Same words went to', html)
        self.assertNotIn('Paid to', html)

    def test_the_guess_button_posts_exactly_what_the_old_form_did(self):
        earlier = BankStatement.objects.create(bank_account=self.bank, period_end=date(2026, 2, 28), end_balance=D('0'))
        interest = ledger.account(self.prop, '4511')
        bankrec.add_to_books(line(earlier, date(2026, 2, 27), 'Interest Paid', '12.50', 'interest'), interest)
        new = line(self.statement, date(2026, 3, 31), 'Interest Paid', '3.10', 'interest')
        matches = ReconMatch.objects.count()
        self.client.post(self.url, {'action': 'add_to_books', 'bank_line': new.pk, 'account': interest.pk})
        new.refresh_from_db()
        self.assertIsNotNone(new.match_id)
        self.assertEqual(ReconMatch.objects.count(), matches + 1)
        self.assertEqual(JournalEntry.objects.filter(source=JournalEntry.Source.BANK, source_ref=f'bankline:{new.pk}').count(), 1)

    def test_owner_payment_and_split_still_reach_their_actions(self):
        deposit = line(self.statement, date(2026, 3, 12), 'DEPOSIT 8841', '615', 'deposit')
        html = self.page()
        self.assertIn('name="action" value="owner_payment"', html)
        self.assertIn(f'?open={deposit.pk}', html)                                          # several owners in one deposit: the deposit sorting screen, opened on this line

    def test_every_statement_screen_takes_a_dropped_file(self):
        html = self.page()
        self.assertIn('data-statement-drop', html)
        self.assertIn('js/statement-drop.js', html)                                        # the one shared script, from the base page
        self.assertIn('drag the file', html)

    def test_the_month_end_bank_tie_out_takes_a_dropped_file_too(self):
        html = self.client.get(reverse('close_bank_tieout') + '?month=2026-03').content.decode()
        self.assertIn('data-statement-drop', html)
        self.assertIn('data-statement-name', html)
        self.assertIn('js/statement-drop.js', html)
        self.assertIn('drag the file', html)
