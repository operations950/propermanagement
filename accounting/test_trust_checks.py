"""Proper Realty's own books (Office 309) and the checks written from its trust accounts.

The office has a chart of its own (never the association master chart). A check from a trust account (Property Management, General Escrow) is Proper's check, drawn on Proper's check stock and
numbered from Proper's sequence, but it pays a bill that belongs to another property: that property's books take "Paid from Proper Realty trust account", and nothing at all is posted to the office.
"""
import io
from datetime import date
from decimal import Decimal as D

from django.db.models import Sum
from django.test import TestCase, override_settings

from core.models import Contact, Property

from . import check_runs as cr
from . import checkface, ledger, payables, payables_hub
from .models import Account, BankAccount, Bill, JournalEntry, JournalLine, Payment
from .test_check_runs import STORAGES
from .tests import make_assoc


def make_office():
    office = Property.objects.create(name='Proper Realty Co Office 309', property_type=Property.Type.GENERAL)
    prof = ledger.profile_for(office)
    prof.we_do_financials = True
    prof.legal_name = 'Proper Realty Co.'
    prof.next_check_number = 5000
    prof.save()
    for code, name, kind in (('1110', 'Operating Cash', 'asset'), ('1120', 'Property Management Trust Cash', 'asset'), ('1130', 'General Escrow Cash', 'asset'),
                             ('2200', 'Accounts Payable', 'liability'), ('6800', 'Office Expenses', 'expense')):
        Account.objects.create(association=office, code=code, name=name, kind=kind)
    return office


def bank(prop, name, last4, code, kind):
    b = BankAccount(association=prop, name=name, bank_name='Truist', last4=last4, kind=kind, account=ledger.account(prop, code), routing_number='000000000',
                    bank_address='1 Bank Plaza, Miami, FL 33131', payer_name='Proper Realty Co.' if prop.name.startswith('Proper') else f'{prop.name} Association, Inc.',
                    payer_address='309 NE 3rd Ave, Delray Beach, FL 33444')
    b.account_number = f'000000{last4}'
    b.save()
    return b


@override_settings(STORAGES=STORAGES)
class TrustCheckTests(TestCase):
    def setUp(self):
        self.office = make_office()
        self.alpha = make_assoc('Alpha Condos')
        self.office_op = bank(self.office, 'Operating', '6415', '1110', 'operating')
        self.trust = bank(self.office, 'Property Management', '7001', '1120', 'trust')
        self.alpha_op = bank(self.alpha, 'Alpha Operating', '1111', '1110', 'operating')
        self.acme = Contact.objects.create(name='Acme Pest Control', contact_type=Contact.ContactType.VENDOR)

    def bill(self, prop, number='A-1', amount='250.00', code='6906', **kw):
        return payables.create_bill(prop, date(2026, 10, 1), [(code, D(amount), '')], vendor=self.acme, number=number, due=date(2026, 10, 20), kind=Bill.Kind.ROUTINE, **kw)

    def approved(self, prop, **kw):
        b = self.bill(prop, **kw)
        payables.approve_bill(b, None)
        b.refresh_from_db()
        return b

    def test_the_office_keeps_its_own_chart(self):
        before = Account.objects.filter(association=self.office).count()
        self.assertTrue(ledger.is_office(self.office))
        self.assertFalse(ledger.is_office(self.alpha))
        self.assertEqual(ledger.ensure_chart(self.office), 0)
        ledger.sync_all_charts()
        self.assertEqual(Account.objects.filter(association=self.office).count(), before)

    def test_the_office_can_have_payables_and_is_a_property_a_payable_may_be_set_up_for(self):
        self.assertEqual(payables_hub.eligibility(self.office)[0], 'ok')
        self.assertIn(self.office, [p for p, _ in payables_hub.candidate_properties()])
        self.assertEqual(payables_hub.banks_for(self.office), [self.office_op])               # the trust account is never an option for the office's own bills

    def test_trust_accounts_are_offered_on_other_properties_only(self):
        self.assertEqual(payables_hub.trust_banks_for(self.alpha), [self.trust])
        self.assertEqual(payables_hub.trust_banks_for(self.office), [])
        self.assertEqual(payables_hub.banks_for(self.alpha), [self.alpha_op])

    def test_a_trust_check_posts_to_the_property_it_pays_for_and_not_to_the_office(self):
        bill = self.approved(self.alpha)
        office_entries = JournalEntry.objects.filter(association=self.office).count()
        pay = payables.pay_bills(self.alpha, self.trust, [(bill, D('250.00'))], date(2026, 11, 20), method=Payment.Method.CHECK)
        self.assertEqual(pay.number, '5000')                                                   # Proper's sequence, not Alpha's
        self.assertEqual(ledger.profile_for(self.office).next_check_number, 5001)
        self.assertEqual(ledger.profile_for(self.alpha).next_check_number, 1001)
        self.assertEqual(JournalEntry.objects.filter(association=self.office).count(), office_entries)
        for basis in ('accrual', 'cash'):
            lines = JournalLine.objects.filter(entry__association=self.alpha).filter(entry__basis__in=(basis, 'both'))
            clearing = lines.filter(account__code='2290').aggregate(c=Sum('credit'), d=Sum('debit'))
            self.assertEqual(clearing['c'], D('250.00'))
            self.assertFalse(lines.filter(account=self.alpha_op.account).exclude(entry__source='opening').exists())        # Alpha's own cash is untouched
        bill.refresh_from_db()
        self.assertEqual(bill.status, Bill.Status.PAID)

    def test_the_office_never_pays_its_own_bills_from_trust(self):
        bill = self.approved(self.office, code='6800')
        with self.assertRaises(payables.PayableError):
            payables.pay_bills(self.office, self.trust, [(bill, D('250.00'))], date(2026, 11, 20))
        with self.assertRaises(payables.PayableError):
            payables_hub.set_bank(bill, self.trust)

    def test_a_property_can_choose_a_trust_account_on_a_payable_and_not_another_propertys_operating_account(self):
        bill = self.approved(self.alpha)
        payables_hub.set_bank(bill, self.trust)
        self.assertEqual(bill.bank_account_id, self.trust.pk)
        with self.assertRaises(payables.PayableError):
            payables_hub.set_bank(bill, self.office_op)

    def test_the_check_run_prints_a_trust_check_on_the_checkbook_owners_face_and_numbering(self):
        b1 = self.approved(self.alpha, number='A-1')
        payables_hub.set_bank(b1, self.trust)
        own = self.approved(self.office, number='O-1', amount='40.00', code='6800')
        result = cr.plan()
        banks = {g['bank'].pk: g for g in result['groups']}
        self.assertEqual(set(banks), {self.trust.pk, self.office_op.pk})
        g = banks[self.trust.pk]
        self.assertEqual(g['association'], self.alpha)
        self.assertEqual(g['association_id'], self.office.pk)                                   # one number sequence for all of Proper's checks
        numbers = sorted(c['number'] for gr in result['groups'] for c in gr['checks'])
        self.assertEqual(numbers, [5000, 5001])
        made = cr.print_run(None, date(2026, 11, 20))
        self.assertEqual(len(made), 2)
        trust_pay = next(p for p in made if p.bank_account_id == self.trust.pk)
        self.assertEqual(trust_pay.association_id, self.alpha.pk)
        data = checkface.check_data(trust_pay)
        self.assertEqual(data['payer_name'], 'Proper Realty Co.')
        self.assertEqual(data['bank_label'], self.trust.label)
        pdf = cr.checks_pdf(made)
        self.assertTrue(pdf.startswith(b'%PDF'))
        own.refresh_from_db()
        self.assertEqual(own.status, Bill.Status.PAID)

    def test_two_properties_paid_from_one_trust_account_are_two_groups(self):
        beta = make_assoc('Beta Condos')
        b1, b2 = self.approved(self.alpha, number='A-1'), self.approved(beta, number='B-1')
        payables_hub.set_bank(b1, self.trust)
        payables_hub.set_bank(b2, self.trust)
        groups = [g for g in cr.plan()['groups'] if g['bank'].pk == self.trust.pk]
        self.assertEqual({g['association'] for g in groups}, {self.alpha, beta})
