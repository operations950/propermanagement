"""An owner's payment found on the statement is first looked for among the payments the books already hold (a Yardi receipt, say), so it is never recorded twice; and the unit is found by name."""
from datetime import date
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import Unit

from . import bankrec, charges, ledger
from .models import BankAccount, BankLine, BankStatement, Charge, Receipt
from .test_bank_accounts import STORAGES
from .tests import make_assoc, owner_of


@override_settings(STORAGES=STORAGES)
class OwnerPaymentBase(TestCase):
    def setUp(self):
        self.prop = make_assoc('Castle Condos')
        self.bank = BankAccount.objects.create(association=self.prop, name='Operating', bank_name='Seacoast', last4='1101', account=ledger.account(self.prop, '1110'))
        self.stmt = BankStatement.objects.create(bank_account=self.bank, period_end=date(2026, 10, 31), end_balance=D('0'))
        self.units = list(Unit.objects.filter(property=self.prop).order_by('label'))
        self.unit, self.other_unit = self.units[0], self.units[1]
        self.owner = owner_of(self.prop, self.unit, 'Kristin Bodner')
        owner_of(self.prop, self.other_unit, 'Alberto Vazquez')
        charges.post_charge(self.prop, self.unit, self.owner, Charge.Kind.ASSESSMENT, 'Monthly assessment', date(2026, 10, 1), D('3332.33'))
        # the books already hold the payment (as the Yardi import left it): Oct 5, not on the statement yet
        self.yardi = charges.receive_payment(self.prop, self.unit, self.owner, date(2026, 10, 5), D('3332.33'), Receipt.Method.ACH, reference='Yardi R-4052 :ACH-WEB', bank_account=self.bank)
        self.deposit = BankLine.objects.create(statement=self.stmt, date=date(2026, 10, 6), description='Preauthorized Credit PROPERREALTY-WOO Settlement', amount=D('3332.33'), kind='deposit')
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.url = reverse('accounting:bank', args=[self.prop.pk, self.bank.pk, '2026-10'])


class CandidatesTests(OwnerPaymentBase):
    def test_the_payment_already_in_the_books_is_offered_for_the_deposit(self):
        found = bankrec.receipt_candidates(self.deposit)
        self.assertEqual([c.amount for c in found], [D('3332.33')])
        self.assertEqual(found[0].obj.unit_id, self.unit.pk)

    def test_another_amount_or_a_date_a_month_away_is_not_offered(self):
        far = BankLine.objects.create(statement=self.stmt, date=date(2026, 10, 30), description='Deposit', amount=D('3332.33'), kind='deposit')
        odd = BankLine.objects.create(statement=self.stmt, date=date(2026, 10, 6), description='Deposit', amount=D('100.00'), kind='deposit')
        self.assertEqual(bankrec.receipt_candidates(far), [])
        self.assertEqual(bankrec.receipt_candidates(odd), [])

    def test_the_screen_lists_it_with_a_this_one_button_and_makes_recording_a_new_one_the_second_choice(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn('Already in the books, not on the statement', html)
        self.assertIn('Payment from Kristin Bodner', html)
        self.assertIn('This one', html)
        self.assertIn('Record a new one', html)
        self.assertIn('name="force" value="1"', html)

    def test_with_nothing_in_the_books_the_panel_is_the_plain_form(self):
        lone = BankLine.objects.create(statement=self.stmt, date=date(2026, 10, 20), description='Deposit 99', amount=D('777.00'), kind='deposit')
        html = self.client.get(self.url).content.decode()
        self.assertEqual(bankrec.receipt_candidates(lone), [])
        self.assertIn('>Record</button>', html)


class ThisOneTests(OwnerPaymentBase):
    def test_this_one_matches_the_existing_payment_and_creates_no_receipt(self):
        item = bankrec.receipt_candidates(self.deposit)[0]
        before = Receipt.objects.count()
        self.client.post(self.url, {'action': 'match_manual', 'bank_line': self.deposit.pk, 'item': item.key})
        self.deposit.refresh_from_db()
        self.assertIsNotNone(self.deposit.match_id)
        self.assertEqual(Receipt.objects.count(), before)
        self.assertEqual(Receipt.objects.filter(unit=self.unit, is_void=False).count(), 1)
        self.assertEqual(bankrec.receipt_candidates(BankLine.objects.create(statement=self.stmt, date=date(2026, 10, 6), description='x', amount=D('3332.33'), kind='deposit')), [])


class RefuseDuplicateTests(OwnerPaymentBase):
    def test_recording_the_same_units_payment_again_is_refused(self):
        with self.assertRaises(bankrec.StatementError) as ctx:
            bankrec.owner_payment_from_line(self.deposit, self.unit, self.owner)
        self.assertIn('already have', str(ctx.exception))
        self.assertEqual(Receipt.objects.filter(unit=self.unit).count(), 1)
        self.deposit.refresh_from_db()
        self.assertIsNone(self.deposit.match_id)

    def test_force_records_it_anyway(self):
        bankrec.owner_payment_from_line(self.deposit, self.unit, self.owner, force=True)
        self.assertEqual(Receipt.objects.filter(unit=self.unit).count(), 2)

    def test_a_different_unit_is_not_refused(self):
        other = owner_of(self.prop, self.units[2], 'Grant Cobb')
        bankrec.owner_payment_from_line(self.deposit, self.units[2], other)
        self.assertEqual(Receipt.objects.filter(unit=self.units[2]).count(), 1)

    def test_the_screen_says_why_and_force_goes_through(self):
        r = self.client.post(self.url, {'action': 'owner_payment', 'bank_line': self.deposit.pk, 'unit': self.unit.pk}, follow=True)
        self.assertIn('already have', ' '.join(str(m) for m in r.context['messages']))
        self.assertEqual(Receipt.objects.filter(unit=self.unit).count(), 1)
        self.client.post(self.url, {'action': 'owner_payment', 'bank_line': self.deposit.pk, 'unit': self.unit.pk, 'force': '1'})
        self.assertEqual(Receipt.objects.filter(unit=self.unit).count(), 2)


class UnitPickerTests(OwnerPaymentBase):
    def test_every_unit_option_carries_the_owner_name_and_the_picker_is_searchable(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn(f'<option value="{self.unit.pk}">{self.unit.label} - Kristin Bodner</option>', html)
        self.assertIn(f'<option value="{self.other_unit.pk}">{self.other_unit.label} - Alberto Vazquez</option>', html)
        self.assertIn('class="form-select form-select-sm ac-pick" data-noun="unit" name="unit"', html)
        self.assertIn(f'<option value="{self.units[3].pk}">{self.units[3].label}</option>', html)          # no owner on file: just the unit
