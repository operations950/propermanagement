import io
import shutil
import tempfile
from datetime import date
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from pypdf import PdfReader

from core.models import Contact

from . import check_runs as cr
from . import ledger, payables
from .models import BankAccount, Bill, JournalEntry, OwnerProfile, Payment
from .tests import make_assoc

STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}
DAY = date(2026, 11, 14)


def pages(data):
    return PdfReader(io.BytesIO(data)).pages


def text_of(data):
    return [p.extract_text() for p in pages(data)]


@override_settings(STORAGES=STORAGES)
class CheckRunTests(TestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, True)
        self.override = override_settings(STORAGES=STORAGES, MEDIA_ROOT=self.media)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.alpha = make_assoc('Alpha Condos')
        self.beta = make_assoc('Beta Condos')
        self.bank_a = self.bank(self.alpha, 'Alpha Operating', '1111')
        self.bank_b = self.bank(self.beta, 'Beta Operating', '2222')
        self.acme = Contact.objects.create(name='Acme Pest Control', contact_type=Contact.ContactType.VENDOR)
        self.zed = Contact.objects.create(name='Zed Roofing', contact_type=Contact.ContactType.VENDOR)
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.url = reverse('accounting:payables_print_checks')

    def bank(self, prop, name, last4, kind='operating', code='1110'):
        bank = BankAccount(association=prop, name=name, bank_name='Truist', last4=last4, kind=kind, account=ledger.account(prop, code), routing_number='000000000',
                           bank_address='1 Bank Plaza, Miami, FL 33131', payer_name=f'{prop.name} Association, Inc.', payer_address='9 Main Street, Miami, FL 33131')
        bank.account_number = f'000000{last4}'
        bank.save()
        return bank

    def bill(self, prop, vendor, number, amount, due, **kw):
        return payables.create_bill(prop, date(2026, 10, 1), [('6906', D(amount), '')], vendor=vendor, number=number, due=due, kind=Bill.Kind.ROUTINE, **kw)

    def seed(self):
        """Alpha: Acme owed for two months and Zed once; Beta: Acme once."""
        self.a1 = self.bill(self.alpha, self.acme, 'A-100', '250.00', date(2026, 10, 20))
        self.a2 = self.bill(self.alpha, self.acme, 'A-101', '300.00', date(2026, 11, 20))
        self.z1 = self.bill(self.alpha, self.zed, 'Z-9', '1000.00', date(2026, 10, 10))
        self.b1 = self.bill(self.beta, self.acme, 'B-7', '75.50', date(2026, 10, 15))

    def print_all(self, **extra):
        return self.client.post(self.url, {'action': 'print', 'date': '2026-11-14', 'bill': [b.pk for b in Bill.objects.filter(status='approved')], **extra})

    # ---- the plan
    def test_it_groups_by_bank_account_and_pays_one_vendor_with_one_check(self):
        self.seed()
        result = cr.plan()
        self.assertEqual([g['bank'].pk for g in result['groups']], [self.bank_a.pk, self.bank_b.pk])
        alpha = result['groups'][0]
        self.assertEqual(len(alpha['checks']), 2)                                         # Acme's two invoices are ONE check, Zed's is the other
        acme = next(c for c in alpha['checks'] if c['payee'] == 'Acme Pest Control')
        self.assertEqual(sorted(b.number for b in acme['bills']), ['A-100', 'A-101'])
        self.assertEqual(acme['total'], D('550.00'))
        self.assertEqual((result['n_checks'], result['total']), (3, D('1625.50')))

    def test_checks_are_ordered_by_association_then_due_date_and_numbered_from_each_associations_next_number(self):
        self.seed()
        for prop, n in ((self.alpha, 1050), (self.beta, 2001)):
            profile = ledger.profile_for(prop)
            profile.next_check_number = n
            profile.save()
        alpha, beta = cr.plan()['groups']
        self.assertEqual([(c['payee'], c['number']) for c in alpha['checks']], [('Zed Roofing', 1050), ('Acme Pest Control', 1051)])      # Zed is due Oct 10, Acme's first invoice Oct 20
        self.assertEqual([(c['payee'], c['number']) for c in beta['checks']], [('Acme Pest Control', 2001)])

    def test_the_same_vendor_in_two_bank_accounts_gets_a_check_from_each(self):
        second = self.bank(self.alpha, 'Alpha Reserve', '3333', kind='reserve', code='1120')
        one = self.bill(self.alpha, self.acme, 'A-1', '100.00', date(2026, 10, 20))
        two = self.bill(self.alpha, self.acme, 'A-2', '200.00', date(2026, 10, 25))
        for b in (one, two):
            b.bank_account = self.bank_a if b is one else second
            b.save()
        groups = cr.plan()['groups']
        self.assertEqual(sorted(len(g['checks']) for g in groups), [1, 1])
        self.assertEqual({g['bank'].pk for g in groups}, {self.bank_a.pk, second.pk})

    # ---- printing is paying
    def test_printing_makes_the_payments_pays_the_bills_posts_the_books_and_returns_one_pdf(self):
        self.seed()
        entries_before = JournalEntry.objects.count()
        response = self.print_all()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        pays = list(Payment.objects.order_by('pk'))
        self.assertEqual([(p.number, p.payee, p.amount) for p in pays], [('1001', 'Zed Roofing', D('1000.00')), ('1002', 'Acme Pest Control', D('550.00')), ('1001', 'Acme Pest Control', D('75.50'))])
        self.assertTrue(all(p.printed_at and p.entry_id and p.cash_entry_id and p.status == 'outstanding' for p in pays))
        self.assertEqual(JournalEntry.objects.count(), entries_before + 6)                    # accrual and cash for each of the three checks
        self.assertEqual(set(Bill.objects.values_list('status', flat=True)), {'paid'})
        self.assertEqual(len(pages(response.content)), 3)                                       # one page per check
        acme = next(p for p in pays if p.amount == D('550.00'))
        self.assertEqual(acme.memo, 'Inv A-100, A-101')
        self.assertEqual(acme.bill_links.count(), 2)
        self.assertEqual(ledger.profile_for(self.alpha).next_check_number, 1003)

    def test_a_payable_that_is_unticked_is_held_out_of_the_run(self):
        self.seed()
        response = self.client.post(self.url, {'action': 'print', 'date': '2026-11-14', 'bill': [self.a1.pk, self.z1.pk]})
        self.assertEqual(len(pages(response.content)), 2)
        self.a2.refresh_from_db(), self.b1.refresh_from_db()
        self.assertEqual((self.a2.status, self.b1.status), ('approved', 'approved'))
        paid_acme = Payment.objects.get(payee_text='', vendor=self.acme)
        self.assertEqual(paid_acme.amount, D('250.00'))                                           # only the ticked invoice

    def test_printing_with_nothing_ticked_pays_nothing(self):
        self.seed()
        response = self.client.post(self.url, {'action': 'print', 'date': '2026-11-14'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Payment.objects.count(), 0)

    def test_the_check_page_lists_the_payee_amount_words_and_the_invoices_on_the_stub(self):
        self.seed()
        data = self.print_all().content
        texts = text_of(data)
        acme = next(t for t in texts if 'Acme Pest Control' in t and '550.00' in t)
        for needle in ('Five hundred fifty and 00/100', 'A-100', 'A-101', '250.00', '300.00', '11/14/2026', 'RECORDS COPY', 'VENDOR COPY', 'Check No.'):
            self.assertIn(needle, acme)

    # ---- the safety net
    def test_a_test_print_pays_and_numbers_nothing(self):
        self.seed()
        response = self.client.get(reverse('accounting:payables_checks_pdf') + f'?test={self.alpha.pk}')
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertEqual(len(pages(response.content)), 1)
        self.assertIn('TEST PAGE', text_of(response.content)[0])
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(ledger.profile_for(self.alpha).next_check_number, 1001)
        self.assertEqual(Bill.objects.filter(status='approved').count(), 4)

    def test_a_reprint_draws_the_same_checks_and_posts_nothing(self):
        self.seed()
        first = self.print_all().content
        before = (Payment.objects.count(), JournalEntry.objects.count(), ledger.profile_for(self.alpha).next_check_number)
        run = cr.recent_runs()[0]
        again = self.client.get(reverse('accounting:payables_checks_pdf') + f'?run={run["key"]}')
        self.assertEqual(text_of(again.content), text_of(first))
        one = self.client.get(reverse('accounting:payables_checks_pdf') + f'?payment={run["payments"][0].pk}')
        self.assertEqual(len(pages(one.content)), 1)
        self.assertEqual((Payment.objects.count(), JournalEntry.objects.count(), ledger.profile_for(self.alpha).next_check_number), before)

    def test_a_void_puts_the_payables_back_in_the_list_to_print(self):
        self.seed()
        self.print_all()
        self.assertEqual(cr.plan()['n_checks'], 0)
        payment = Payment.objects.get(vendor=self.acme, amount=D('550.00'))
        self.client.post(self.url, {'action': 'void', 'payment': payment.pk})
        payment.refresh_from_db()
        self.assertEqual(payment.status, 'void')
        self.assertEqual(set(Bill.objects.filter(vendor=self.acme, association=self.alpha).values_list('status', flat=True)), {'approved'})
        result = cr.plan()
        self.assertEqual(result['n_checks'], 1)
        self.assertEqual(result['groups'][0]['checks'][0]['total'], D('550.00'))
        run = cr.recent_runs()[0]
        self.assertEqual(run['n'], 2)                                                                  # the void check is still listed, but not counted
        self.assertEqual(len(run['payments']), 3)

    # ---- what cannot print
    def test_a_payable_for_an_association_without_a_bank_account_is_listed_as_cannot_print_with_a_way_to_fix_it(self):
        gamma = make_assoc('Gamma Condos')
        self.bill(gamma, self.acme, 'G-1', '40.00', date(2026, 10, 20))
        result = cr.plan()
        self.assertEqual((result['groups'], len(result['blocked'])), ([], 1))
        self.assertIn('no bank account', result['blocked'][0]['why'])
        html = self.client.get(self.url).content.decode()
        self.assertIn("Can't print yet", html)
        self.assertIn(reverse('accounting:settings', args=[gamma.pk]) + '#banks', html)

    def test_several_operating_accounts_and_none_chosen_waits_until_one_is_chosen(self):
        self.bank(self.alpha, 'Alpha Second', '4444')
        bill = self.bill(self.alpha, self.acme, 'A-1', '10.00', date(2026, 10, 20))
        result = cr.plan()
        self.assertEqual([x['bill'].pk for x in result['blocked']], [bill.pk])
        self.assertIn('several bank accounts', result['blocked'][0]['why'])
        bill.bank_account = self.bank_a
        bill.save()
        self.assertEqual(cr.plan()['blocked'], [])

    def test_the_screen_warns_about_a_missing_check_layout_and_a_gap_in_the_numbers(self):
        self.seed()
        Payment.objects.create(association=self.alpha, bank_account=self.bank_a, number='1040', date=DAY, payee_text='Old', amount=D('10'), method='check')
        profile = ledger.profile_for(self.alpha)
        profile.next_check_number = 1050
        profile.save()
        warnings = [w['text'] for w in cr.plan()['groups'][0]['warnings']]
        self.assertFalse(any('No check layout' in w for w in warnings))                         # the built-in check face is the default now
        self.assertTrue(any('next check number is 1050' in w and 'last check written' in w and 'gap' in w for w in warnings))
        html = self.client.get(self.url).content.decode()
        self.assertNotIn('Set up the check stock', html)

    def test_only_an_admin_can_open_it(self):
        self.client.logout()
        self.client.force_login(get_user_model().objects.create_user('staffer', password='pw12345678'))
        self.assertNotEqual(self.client.get(self.url).status_code, 200)
        self.assertNotEqual(self.client.get(reverse('accounting:payables_print_envelopes')).status_code, 200)

    def test_the_screen_shows_every_check_with_its_number_and_invoices_and_the_recent_run_afterwards(self):
        self.seed()
        html = self.client.get(self.url).content.decode()
        for needle in ('Alpha Condos', 'Paid from Alpha Operating', 'Acme Pest Control', 'Invoice A-100', 'Invoice A-101', 'Print checks', 'Test print on plain paper'):
            self.assertIn(needle, html)
        self.print_all()
        html = self.client.get(self.url).content.decode()
        self.assertIn('Printed recently', html)
        self.assertIn('Reprint all', html)
        self.assertIn('Nothing is ready to print', html)


@override_settings(STORAGES=STORAGES)
class EnvelopeTests(TestCase):
    def setUp(self):
        self.alpha = make_assoc('Alpha Condos')
        self.bank = BankAccount(association=self.alpha, name='Operating', bank_name='Truist', last4='1111', account=ledger.account(self.alpha, '1110'), routing_number='000000000',
                                payer_name='Alpha Condos Association, Inc.', payer_address='9 Main Street, Miami, FL 33131')
        self.bank.account_number = '0000001111'
        self.bank.save()
        self.acme = Contact.objects.create(name='Acme Pest Control', contact_type=Contact.ContactType.VENDOR)
        self.zed = Contact.objects.create(name='Zed Roofing', contact_type=Contact.ContactType.VENDOR)
        self.nowhere = Contact.objects.create(name='No Address Plumbing', contact_type=Contact.ContactType.VENDOR)
        OwnerProfile.objects.create(contact=self.acme, mailing_address='12 Palm Way\nBoca Raton, FL 33432')
        OwnerProfile.objects.create(contact=self.zed, mailing_address='900 Ocean Blvd, Delray Beach, FL 33444')
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        for vendor, number, amount, due in ((self.zed, 'Z-1', '100.00', date(2026, 10, 10)), (self.acme, 'A-1', '50.00', date(2026, 10, 20)), (self.nowhere, 'N-1', '25.00', date(2026, 10, 30))):
            payables.create_bill(self.alpha, date(2026, 10, 1), [('6906', D(amount), '')], vendor=vendor, number=number, due=due, kind=Bill.Kind.ROUTINE)
        self.client.post(reverse('accounting:payables_print_checks'), {'action': 'print', 'date': '2026-11-14', 'bill': [b.pk for b in Bill.objects.all()]})
        self.pays = list(Payment.objects.order_by('number'))

    def test_one_plain_number_10_envelope_per_check_in_check_order_with_the_vendor_address_and_proper_realty(self):
        ids = [p.pk for p in self.pays if p.vendor_id != self.nowhere.pk]
        url = reverse('accounting:payables_envelopes_pdf') + '?' + '&'.join(f'id={i}' for i in ids)
        response = self.client.get(url)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        ps = pages(response.content)
        self.assertEqual(len(ps), 2)
        self.assertAlmostEqual(float(ps[0].mediabox.width) / 72, 9.5, 2)                       # size 10: 9.5 x 4.125 inches
        self.assertAlmostEqual(float(ps[0].mediabox.height) / 72, 4.125, 2)
        first, second = text_of(response.content)
        self.assertIn('Zed Roofing', first)                                                      # check 1001 is Zed's (due first)
        self.assertIn('900 Ocean Blvd', first)
        self.assertIn('Delray Beach, FL 33444', first)
        self.assertIn('Acme Pest Control', second)
        self.assertIn('12 Palm Way', second)
        for t in (first, second):
            self.assertIn('Proper Realty', t)
            self.assertNotIn('Alpha Condos', t)                                                  # the return address is the company, not the association

    def test_a_vendor_without_an_address_is_warned_about_and_skipped(self):
        html = self.client.get(reverse('accounting:payables_print_envelopes')).content.decode()
        self.assertIn('No mailing address on file for this vendor', html)
        self.assertIn(reverse('contact_edit', args=[self.nowhere.pk]), html)
        self.assertIn('1 without an address', html)
        ids = [p.pk for p in self.pays]
        response = self.client.get(reverse('accounting:payables_envelopes_pdf') + '?' + '&'.join(f'id={i}' for i in ids))
        self.assertEqual(len(pages(response.content)), 2)                                        # the vendor with no address gets none
        only = [p.pk for p in self.pays if p.vendor_id == self.nowhere.pk]
        back = self.client.get(reverse('accounting:payables_envelopes_pdf') + f'?id={only[0]}')
        self.assertEqual(back.status_code, 302)

    def test_the_latest_run_is_ticked_and_a_test_print_makes_one_envelope(self):
        html = self.client.get(reverse('accounting:payables_print_envelopes')).content.decode()
        self.assertEqual(html.count(' checked'), 2)
        ids = '&'.join(f'id={p.pk}' for p in self.pays)
        test = self.client.get(reverse('accounting:payables_envelopes_pdf') + '?test=1&' + ids)
        self.assertEqual(len(pages(test.content)), 1)
        self.assertIn('TEST PAGE', text_of(test.content)[0])

    def test_a_voided_check_gets_no_envelope(self):
        payment = self.pays[0]
        payables.void_payment(payment)
        response = self.client.get(reverse('accounting:payables_envelopes_pdf') + f'?id={payment.pk}&id={self.pays[1].pk}')
        self.assertEqual(len(pages(response.content)), 1)
