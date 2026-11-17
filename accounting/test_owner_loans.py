"""Owner loans: a long-term loan an owner repays on a schedule, kept apart from their dues (no late fee, no interest, not in the balance, a late letter, the aging or the list)."""
from datetime import date
from decimal import Decimal as D
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import Unit

from . import charges, delinquency, ledger, owner_loans
from .models import BankAccount, Charge, JournalEntry, MonthLock
from .test_collections_close import STORAGES
from .tests import make_assoc, owner_of

OPEN = date(2026, 9, 30)
TODAY = date(2026, 11, 17)


def code_balance(prop, code, basis=ledger.ACCRUAL):
    bal = ledger.balances_as_of(prop, TODAY, basis)
    return bal.get(ledger.account(prop, code).pk, D('0.00'))


class LoanBase(TestCase):
    def setUp(self):
        self.prop = make_assoc()
        self.unit = Unit.objects.filter(property=self.prop).first()
        self.owner = owner_of(self.prop, self.unit)
        self.bank = BankAccount.objects.create(association=self.prop, name='Operating', bank_name='Seacoast', last4='1234', kind='operating', account=ledger.account(self.prop, '1110'))
        profile = ledger.profile_for(self.prop)
        profile.late_fee_kind, profile.late_fee_flat, profile.late_fee_grace_days, profile.interest_rate = 'flat', D('25'), 10, D('12')
        profile.late_fee_confirmed = profile.interest_confirmed = True
        profile.save()

    def opening(self, amount='24000.00'):
        c = Charge.objects.create(association=self.prop, unit=self.unit, owner=self.owner, kind=Charge.Kind.OPENING, description='Balance forward from Yardi, 61 to 90 days', due_date=OPEN,
                                  amount=D(amount), income_account=ledger.account(self.prop, '4500'))
        ledger.post_entry(self.prop, OPEN, [{'account': '1300', 'debit': D(amount), 'unit': self.unit, 'owner': self.owner}, {'account': '3070', 'credit': D(amount)}],
                          memo='Opening balances', basis='accrual', source=JournalEntry.Source.OPENING)
        return c

    def account(self):
        return next(a for a in charges.accounts(self.prop) if a['unit'].pk == self.unit.pk)

    def with_loan(self, opening='24000.00', loan='22688.55'):
        self.opening(opening)
        return owner_loans.move_into_loan(self.prop, self.unit, self.owner, D(loan))[0]


class MovingIntoALoanTests(LoanBase):
    def test_the_amount_leaves_the_balance_and_becomes_a_loan_with_its_own_account(self):
        self.opening()
        loan, _ = owner_loans.move_into_loan(self.prop, self.unit, self.owner, D('22688.55'))
        a = self.account()
        self.assertEqual((a['balance'], a['loan']), (D('1311.45'), D('22688.55')))
        self.assertEqual((loan.kind, loan.due_date, loan.amount), ('loan', OPEN, D('22688.55')))
        self.assertEqual((code_balance(self.prop, '1300'), code_balance(self.prop, '1305')), (D('1311.45'), D('22688.55')))      # the books agree with the owner's account
        self.assertEqual(ledger.account(self.prop, '1305').name, 'Owner Loans Receivable (long-term)')
        self.assertEqual(ledger.account(self.prop, '1305').kind, 'asset')

    def test_the_receivables_report_still_ties_to_the_books(self):
        self.opening()
        owner_loans.move_into_loan(self.prop, self.unit, self.owner, D('22688.55'))
        totals = charges.totals(charges.accounts(self.prop))
        self.assertEqual(code_balance(self.prop, '1300'), totals['net'])

    def test_payments_made_since_are_put_back_loan_installment_on_the_loan_and_the_rest_on_the_dues(self):
        self.opening('27020.94')
        charges.post_charge(self.prop, self.unit, self.owner, Charge.Kind.ASSESSMENT, 'Regular assessment Oct 2026', date(2026, 10, 1), D('3332.33'))
        receipt = charges.receive_payment(self.prop, self.unit, self.owner, date(2026, 10, 26), D('4332.33'), reference='Yardi R-4265')       # applied to the balance forward, oldest first
        self.assertEqual(self.account()['balance'], D('26020.94'))
        loan, notes = owner_loans.move_into_loan(self.prop, self.unit, self.owner, D('27020.94'), loan_payments={receipt.pk: D('1000.00')})
        a = self.account()
        self.assertEqual((a['loan'], a['balance']), (D('26020.94'), D('0.00')))                 # 1,000 paid the loan; 3,332.33 paid October's assessment
        self.assertEqual(sorted((where, part) for r, where, part in notes), [('Regular assessment Oct 2026', D('3332.33')), ('loan', D('1000.00'))])
        self.assertEqual((code_balance(self.prop, '1300'), code_balance(self.prop, '1305')), (D('0.00'), D('26020.94')))
        self.assertEqual(Charge.objects.get(pk=loan.pk).income_account.code, '4515')
        self.assertTrue(Charge.objects.get(kind='opening').is_void)                              # all of the balance forward was the loan
        cash_4500, cash_4515 = code_balance(self.prop, '4500', ledger.CASH), code_balance(self.prop, '4515', ledger.CASH)
        self.assertEqual(cash_4515, D('-1000.00'))                                               # the installment is special assessment income on the cash book

    def test_the_whole_balance_can_be_the_loan(self):
        self.opening('5000.00')
        owner_loans.move_into_loan(self.prop, self.unit, self.owner, D('5000.00'))
        a = self.account()
        self.assertEqual((a['balance'], a['loan']), (D('0.00'), D('5000.00')))
        self.assertEqual(code_balance(self.prop, '1300'), D('0.00'))

    def test_it_is_refused_when_no_charge_owes_that_much_or_it_already_exists(self):
        self.opening('1000.00')
        with self.assertRaises(charges.ChargeError):
            owner_loans.move_into_loan(self.prop, self.unit, self.owner, D('1500.00'))
        owner_loans.move_into_loan(self.prop, self.unit, self.owner, D('400.00'))
        with self.assertRaises(charges.ChargeError) as e:
            owner_loans.move_into_loan(self.prop, self.unit, self.owner, D('100.00'))
        self.assertIn('already has a loan', str(e.exception))

    def test_a_closed_month_refuses_it_unless_it_is_told_to(self):
        self.opening()
        MonthLock.objects.create(association=self.prop, month=date(2026, 9, 1))
        with self.assertRaises(ledger.LedgerError):
            owner_loans.move_into_loan(self.prop, self.unit, self.owner, D('100.00'))
        owner_loans.move_into_loan(self.prop, self.unit, self.owner, D('100.00'), allow_locked=True)
        self.assertEqual(self.account()['loan'], D('100.00'))

    def test_a_loan_is_not_made_or_voided_like_a_charge(self):
        loan = self.with_loan()
        with self.assertRaises(charges.ChargeError):
            charges.post_charge(self.prop, self.unit, self.owner, Charge.Kind.LOAN, 'Loan', OPEN, D('10'))
        with self.assertRaises(charges.ChargeError):
            charges.void_charge(loan)


class KeptOutOfDuesTests(LoanBase):
    def test_no_late_fee_and_no_interest_on_a_loan(self):
        self.with_loan(opening='22688.55', loan='22688.55')
        self.assertEqual(charges._late_fee_rows(self.prop, TODAY), [])
        self.assertEqual(charges._interest_rows(self.prop, TODAY), [])
        self.assertEqual(charges.assess_late_fees(self.prop, TODAY), [])
        self.assertEqual(charges.assess_interest(self.prop, TODAY), [])

    def test_dues_beside_a_loan_still_get_their_late_fee(self):
        self.with_loan()
        charges.post_charge(self.prop, self.unit, self.owner, Charge.Kind.ASSESSMENT, 'Regular assessment Sep 2026', date(2026, 9, 1), D('3332.33'))
        rows = charges._late_fee_rows(self.prop, TODAY)
        self.assertEqual(sorted((c.description, fee) for c, fee, due in rows), [('Balance forward from Yardi, 61 to 90 days', D('25.00')), ('Regular assessment Sep 2026', D('25.00'))])      # the dues, never the loan

    def test_the_loan_is_not_in_what_is_owed_a_letter_or_the_list(self):
        self.with_loan(opening='23000.00', loan='22688.55')
        b = delinquency.breakdown(self.unit, self.owner)
        self.assertEqual((b['assessments'], b['total']), (D('311.45'), D('311.45')))
        rows = delinquency.delinquent_rows(self.prop, TODAY, D('500'))
        self.assertEqual([r for r in rows if r['unit'].pk == self.unit.pk], [])                 # only $311.45 of dues: under the line, whatever the loan is
        self.assertEqual(delinquency.delinquent_as_of(self.prop, TODAY, D('500')), [])

    def test_the_loan_is_not_aged(self):
        self.with_loan(opening='22688.55', loan='22688.55')
        self.assertEqual(charges.aging(self.prop, TODAY)['rows'], [])

    def test_the_running_ledger_shows_dues_only_and_the_loan_has_its_own(self):
        self.with_loan(opening='23000.00', loan='22688.55')
        rows = charges.ledger_rows(self.unit)
        self.assertEqual([(r['what'], r['balance']) for r in rows], [('Balance forward from Yardi, 61 to 90 days', D('311.45'))])
        loan = charges.loan_rows(self.unit)
        self.assertEqual([(r['what'], r['balance']) for r in loan], [('Loan balance', D('22688.55'))])


class PayingTests(LoanBase):
    def test_an_ordinary_payment_never_touches_the_loan(self):
        self.with_loan(opening='23000.00', loan='22688.55')
        charges.post_charge(self.prop, self.unit, self.owner, Charge.Kind.ASSESSMENT, 'Regular assessment Oct 2026', date(2026, 10, 1), D('3332.33'))
        charges.receive_payment(self.prop, self.unit, self.owner, date(2026, 11, 1), D('5000.00'))
        a = self.account()
        self.assertEqual(a['loan'], D('22688.55'))                                          # the payment cleared the dues (3,643.78) and the rest is held as a credit, not on the loan
        self.assertEqual(a['balance'], D('-1356.22'))

    def test_a_payment_for_the_loan_pays_only_the_loan(self):
        self.with_loan(opening='23000.00', loan='22688.55')
        charges.post_charge(self.prop, self.unit, self.owner, Charge.Kind.ASSESSMENT, 'Regular assessment Oct 2026', date(2026, 10, 1), D('3332.33'))
        charges.receive_payment(self.prop, self.unit, self.owner, date(2026, 11, 1), D('1000.00'), to_loan=True)
        a = self.account()
        self.assertEqual((a['loan'], a['balance']), (D('21688.55'), D('3643.78')))           # the dues did not move
        self.assertEqual((code_balance(self.prop, '1300'), code_balance(self.prop, '1305')), (D('3643.78'), D('21688.55')))
        self.assertEqual(code_balance(self.prop, '4515', ledger.CASH), D('-1000.00'))      # on the cash book it is special assessment income, not assessment income
        self.assertEqual(code_balance(self.prop, '4500', ledger.CASH), D('0.00'))
        self.assertEqual([r['what'] for r in charges.ledger_rows(self.unit)], ['Balance forward from Yardi, 61 to 90 days', 'Regular assessment Oct 2026'])   # not in the dues ledger
        self.assertEqual([r['balance'] for r in charges.loan_rows(self.unit)], [D('22688.55'), D('21688.55')])

    def test_a_loan_payment_cannot_be_more_than_the_loan_or_for_an_owner_with_none(self):
        self.with_loan(opening='23000.00', loan='1000.00')
        with self.assertRaises(charges.ChargeError) as e:
            charges.receive_payment(self.prop, self.unit, self.owner, date(2026, 11, 1), D('1500.00'), to_loan=True)
        self.assertIn('more than the loan balance of $1,000.00', str(e.exception))
        charges.receive_payment(self.prop, self.unit, self.owner, date(2026, 11, 1), D('1000.00'), to_loan=True)
        with self.assertRaises(charges.ChargeError) as e:
            charges.receive_payment(self.prop, self.unit, self.owner, date(2026, 11, 2), D('10.00'), to_loan=True)
        self.assertIn('no loan balance', str(e.exception))

    def test_paid_ahead_money_is_not_used_on_the_loan(self):
        self.with_loan(opening='23000.00', loan='22688.55')
        charges.receive_payment(self.prop, self.unit, self.owner, date(2026, 11, 1), D('2000.00'))          # 311.45 of dues, the rest held
        charges.apply_credits(self.prop, self.unit, self.owner)
        self.assertEqual(self.account()['loan'], D('22688.55'))


@override_settings(STORAGES=STORAGES)
class CommandAndScreenTests(LoanBase):
    def run_command(self, *args):
        out = StringIO()
        call_command('move_owner_loans', self.prop.name, *args, stdout=out)
        return out.getvalue()

    def test_a_dry_run_changes_nothing_and_apply_moves_it(self):
        self.opening()
        said = self.run_command('--loan', f'{self.unit.label}=22688.55')
        self.assertIn('MOVE', said)
        self.assertIn('DRY RUN', said)
        self.assertEqual(self.account()['loan'], D('0.00'))
        self.assertIn('APPLIED', self.run_command('--loan', f'{self.unit.label}=22688.55', '--apply'))
        a = self.account()
        self.assertEqual((a['balance'], a['loan']), (D('1311.45'), D('22688.55')))
        self.assertIn('already has a loan', self.run_command('--loan', f'{self.unit.label}=22688.55', '--apply'))               # safe to run again

    def test_a_closed_month_skips_the_owner_and_says_so(self):
        self.opening()
        MonthLock.objects.create(association=self.prop, month=date(2026, 9, 1))
        said = self.run_command('--loan', f'{self.unit.label}=100', '--apply')
        self.assertIn('is closed', said)
        self.assertEqual(self.account()['loan'], D('0.00'))

    def test_the_owners_page_shows_loan_balance_on_its_own_line(self):
        user = get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678')
        self.client.force_login(user)
        self.with_loan(opening='23000.00', loan='22688.55')
        html = self.client.get(reverse('accounting:unit', args=[self.prop.pk, self.unit.pk])).content.decode()
        self.assertIn('Loan balance', html)
        self.assertIn('22,688.55', html)
        self.assertNotIn('not due now', html)

    def test_a_payment_can_be_marked_as_for_the_loan_from_the_payment_screen(self):
        user = get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678')
        self.client.force_login(user)
        self.with_loan(opening='23000.00', loan='22688.55')
        page = self.client.get(reverse('accounting:receipt_new', args=[self.prop.pk]) + f'?unit={self.unit.pk}').content.decode()
        self.assertIn('This payment is for the loan', page)
        self.client.post(reverse('accounting:receipt_new', args=[self.prop.pk]), {'unit': self.unit.pk, 'amount': '500.00', 'date': '2026-11-01', 'method': 'check', 'to_loan': '1'})
        self.assertEqual(self.account()['loan'], D('22188.55'))


@override_settings(STORAGES=STORAGES)
class DepositScreenLoanTests(LoanBase):
    def test_an_owner_row_on_the_deposits_screen_can_be_for_the_loan(self):
        from . import views_money
        self.with_loan(opening='23000.00', loan='22688.55')
        people = {p['id']: p for p in views_money._deposit_people(self.prop)}
        self.assertEqual(people[self.unit.pk]['loan'], '22688.55')
        self.assertEqual(people[self.unit.pk]['owes'], '311.45')                              # the loan is not in what they owe
        rows = views_money._owner_rows(self.prop, [{'unit': self.unit.pk, 'amount': '1000.00', 'ref': 'ACH', 'to_loan': True}])
        self.assertTrue(rows[0]['to_loan'])
        receipt = charges.receive_payment(self.prop, self.unit, self.owner, date(2026, 11, 1), D('1000.00'), to_loan=rows[0]['to_loan'])
        self.assertEqual(self.account()['loan'], D('21688.55'))
        self.assertEqual(receipt.applications.count(), 1)

    def test_the_deposits_screen_offers_the_loan_checkbox(self):
        user = get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678')
        self.client.force_login(user)
        self.with_loan(opening='23000.00', loan='22688.55')
        source = open('accounting/templates/accounting/deposits.html', encoding='utf8').read()
        self.assertIn('For the loan (', source)
        self.assertIn('to_loan: !!r.loan', source)


class BudgetIncomeAccountTests(TestCase):
    def test_special_assessment_loan_interest_income_can_be_a_budget_income_line(self):
        from . import budgeting
        prop = make_assoc()
        income = ledger.account(prop, '4516')
        self.assertTrue(budgeting.is_budgetable(income))
        self.assertEqual(budgeting.section_of(income), 'Income')
        for code in ('4500', '4513', '4515'):                                     # the assessments themselves and the special assessments stay off the list
            self.assertFalse(budgeting.is_budgetable(ledger.account(prop, code)), code)
