import json
import tempfile
from datetime import date
from decimal import Decimal as D
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import Contact, Property

from . import ledger, payables
from . import payables_hub as hub
from .models import Account, BankAccount, Bill, Budget, BudgetLine, JournalEntry, PayableUpload, VendorProfile, VendorTerms
from .tests import make_assoc

MEDIA = tempfile.mkdtemp()
STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage', 'OPTIONS': {'location': MEDIA}},
            'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage', 'OPTIONS': {'location': MEDIA}},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}
READ = {'is_invoice': True, 'vendor': 'Acme Plumbing LLC', 'vendor_street': '12 Main St', 'vendor_city': 'Boca Raton', 'vendor_state': 'FL', 'vendor_zip': '33432', 'vendor_phone': '(561) 555-0100',
        'vendor_email': 'billing@acmeplumbing.com', 'vendor_tax_id': '12-3456789', 'invoice_number': 'INV-1001', 'invoice_date': '2026-11-03', 'due_date': '2026-12-03', 'total': 450.0,
        'lines': [{'description': 'Repair leaking pipe', 'amount': 450.0}], 'bill_to': 'Test Condos', 'property_address': '', 'unit': '', 'account_number': '', 'description': 'Plumbing repair of the main pipe'}


def pdf_file(name='invoice.pdf'):
    return SimpleUploadedFile(name, b'%PDF-1.4 fake invoice', content_type='application/pdf')


def bank(prop, name='Operating', last4='4465', kind='operating', code='1110'):
    return BankAccount.objects.create(association=prop, name=name, bank_name='Truist', last4=last4, kind=kind, account=ledger.account(prop, code))


@override_settings(STORAGES=STORAGES)
class PayablesBase(TestCase):
    def setUp(self):
        self.prop = make_assoc('Test Condos')
        self.bank = bank(self.prop)
        self.user = get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678')
        self.client.force_login(self.user)
        self.upload_url = reverse('accounting:payables_upload')
        self.home = reverse('accounting:payables_home')

    def drop(self, read=None, name='invoice.pdf'):
        with mock.patch.object(hub, 'read_invoice', return_value=dict(read or READ)):
            return hub.ingest(pdf_file(name), self.user)

    def card_url(self, up):
        return reverse('accounting:payables_card', args=[up.pk])

    def acct(self, code='6800'):
        return ledger.account(self.prop, code)

    def vendor(self, name='Acme Plumbing LLC'):
        return Contact.objects.create(name=name, contact_type=Contact.ContactType.VENDOR)


class EligibilityTests(PayablesBase):
    def test_an_association_with_its_books_and_a_bank_account_is_eligible(self):
        self.assertEqual(hub.eligibility(self.prop)[0], 'ok')

    def test_no_bank_account_blocks_and_points_at_the_bank_account_setup(self):
        other = make_assoc('No Bank Condos')
        code, message, url = hub.eligibility(other)
        self.assertEqual(code, 'bank')
        self.assertIn('no bank account', message)
        self.assertTrue(url.endswith('#banks'))
        self.assertIn(f'/{other.pk}/settings/', url)

    def test_an_inactive_bank_account_does_not_count(self):
        other = make_assoc('Closed Bank Condos')
        b = bank(other)
        b.is_active = False
        b.save()
        self.assertEqual(hub.eligibility(other)[0], 'bank')

    def test_books_not_kept_blocks(self):
        other = Property.objects.create(name='Unbooked HOA', property_type=Property.Type.ASSOCIATION)
        self.assertEqual(hub.eligibility(other)[0], 'books')

    def test_associations_and_commercial_buildings_are_eligible_and_other_kinds_are_not(self):
        self.assertEqual(hub.ELIGIBLE_TYPES, (Property.Type.ASSOCIATION, Property.Type.COMMERCIAL))
        commercial = Property.objects.create(name='Office Park', property_type=Property.Type.COMMERCIAL)     # its books start switched on, with no bank account yet
        self.assertEqual(hub.eligibility(commercial)[0], 'bank')
        rental = Property.objects.create(name='Beach House', property_type=Property.Type.SHORT_TERM_RENTAL)
        self.assertEqual(hub.eligibility(rental)[0], 'type')

    def test_the_picker_lists_the_ones_we_can_pay_for_first_and_explains_the_rest(self):
        make_assoc('No Bank Condos')
        rows = hub.picker_rows()
        self.assertEqual([r['p'].name for r in rows], ['Test Condos', 'No Bank Condos'])
        self.assertEqual([r['ok'] for r in rows], [True, False])
        self.assertIn('no bank account', rows[1]['msg'])


class UploadAndMatchTests(PayablesBase):
    def test_a_dropped_invoice_is_read_stored_and_becomes_a_card_for_the_right_property(self):
        with mock.patch.object(hub, 'read_invoice', return_value=dict(READ)):
            r = self.client.post(self.upload_url, {'files': pdf_file()}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data['ok'])
        self.assertIn('Create payable', data['cards'][0]['html'])
        up = PayableUpload.objects.get()
        self.assertEqual((up.status, up.read['vendor'], up.read['total']), ('pending', 'Acme Plumbing LLC', '450.00'))
        self.assertTrue(up.file.name.startswith('payable_uploads/'))
        card = hub.build_card(up)
        self.assertEqual(card['prop'], self.prop)
        self.assertIn('the invoice says so', card['prop_how'])

    def test_several_files_in_a_plain_post_make_several_cards(self):
        with mock.patch.object(hub, 'read_invoice', return_value=dict(READ)):
            self.client.post(self.upload_url, {'files': [pdf_file('a.pdf'), pdf_file('b.pdf')]})
        self.assertEqual(PayableUpload.objects.count(), 2)

    def test_only_pdf_png_and_jpg_are_accepted(self):
        r = self.client.post(self.upload_url, {'files': SimpleUploadedFile('notes.txt', b'hello', content_type='text/plain')}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(r.status_code, 400)
        self.assertIn('only PDF, PNG and JPG', r.json()['error'])
        self.assertEqual(PayableUpload.objects.count(), 0)

    def test_an_image_is_accepted(self):
        with mock.patch.object(hub, 'read_invoice', return_value=dict(READ)):
            up = hub.ingest(SimpleUploadedFile('scan.jpg', b'\xff\xd8\xff fake', content_type='image/jpeg'), self.user)
        self.assertEqual(up.filename, 'scan.jpg')

    def test_when_the_invoice_cannot_be_read_there_is_still_a_card_to_fill_in_by_hand(self):
        with mock.patch.object(hub, 'read_invoice', side_effect=RuntimeError('The Claude connection is not set up.')):
            up = hub.ingest(pdf_file(), self.user)
        self.assertEqual(up.read, {})
        card = hub.build_card(up)
        self.assertIn('Claude connection', card['read_error'])
        self.assertFalse(card['can_create'])
        html = self.client.get(self.home).content.decode()
        self.assertIn('Claude connection is not set up', html)

    def test_a_statement_that_is_not_an_invoice_is_flagged(self):
        up = self.drop(dict(READ, is_invoice=False))
        self.assertIn('does not look like an invoice', up.read_error)

    def test_the_vendor_is_matched_by_name_tax_id_and_email_domain(self):
        v = self.vendor('ACME Plumbing, LLC.')
        self.assertEqual(hub.match_vendor(hub.clean_read(READ))[0], v)
        other = self.vendor('Totally Different Name Inc')
        VendorProfile.objects.create(contact=other, tax_id='12-3456789')
        v.delete()
        self.assertEqual(hub.match_vendor(hub.clean_read(dict(READ, vendor='Unknown Co')))[0], other)
        other.delete()
        third = self.vendor('Another Name Entirely')
        third.email = 'accounts@acmeplumbing.com'
        third.save()
        self.assertEqual(hub.match_vendor(hub.clean_read(dict(READ, vendor='Unknown Co', vendor_tax_id='')))[0], third)

    def test_no_vendor_match_offers_one_click_creation_with_the_details_ready(self):
        up = self.drop()
        card = hub.build_card(up)
        self.assertIsNone(card['vendor'])
        self.assertEqual((card['new_vendor']['name'], card['new_vendor']['street'], card['new_vendor']['tax_id']), ('Acme Plumbing LLC', '12 Main St', '12-3456789'))
        self.assertIn('Create the vendor', ' '.join(card['blocks']))
        html = self.client.get(self.home).content.decode()
        self.assertIn('Create this vendor', html)

    def test_the_property_falls_back_to_the_one_association_the_vendor_has_been_paid_by(self):
        v = self.vendor()
        VendorTerms.objects.create(association=self.prop, vendor=v)
        up = self.drop(dict(READ, bill_to='', property_address=''))
        card = hub.build_card(up)
        self.assertEqual(card['prop'], self.prop)
        self.assertIn('only association', card['prop_how'])

    def test_no_property_is_a_flag_and_blocks_creation(self):
        up = self.drop(dict(READ, bill_to='Some Other Place'))
        card = hub.build_card(up)
        self.assertIsNone(card['prop'])
        self.assertIn('Pick the property.', card['blocks'])


class CreateVendorTests(PayablesBase):
    def test_one_click_makes_the_vendor_its_address_and_its_terms(self):
        up = self.drop()
        r = self.client.post(self.card_url(up), {'action': 'create_vendor', 'is_1099': 'on', 'vendor_text': 'Acme Plumbing LLC', 'property_id': self.prop.pk, '_card': '1'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertTrue(r.json()['ok'])
        v = Contact.objects.get(name='Acme Plumbing LLC')
        self.assertEqual((v.contact_type, v.phone, v.email), ('vendor', '561-555-0100', 'billing@acmeplumbing.com'))
        p = VendorProfile.objects.get(contact=v)
        self.assertEqual((p.address_lines, p.tax_id, p.is_1099), (['12 Main St', 'Boca Raton, FL 33432'], '12-3456789', True))
        self.assertEqual(hub.vendor_address_lines(v), ['12 Main St', 'Boca Raton, FL 33432'])
        terms = VendorTerms.objects.get(association=self.prop, vendor=v)
        self.assertEqual((terms.pay_method, terms.is_1099), ('check', True))
        self.assertEqual(hub.build_card(PayableUpload.objects.get(pk=up.pk))['vendor'], v)

    def test_creating_again_does_not_make_a_twin(self):
        up = self.drop()
        self.client.post(self.card_url(up), {'action': 'create_vendor', 'vendor_text': 'Acme Plumbing LLC', '_card': '1'})
        up2 = self.drop()
        self.client.post(self.card_url(up2), {'action': 'create_vendor', 'vendor_text': 'acme plumbing llc', '_card': '1'})
        self.assertEqual(Contact.objects.filter(contact_type='vendor').count(), 1)


class BankAccountTests(PayablesBase):
    def test_a_single_operating_account_is_chosen_for_you_and_said_so(self):
        up = self.drop()
        card = hub.build_card(up)
        self.assertTrue(card['bank']['only'])
        self.assertEqual(card['bank']['selected'], self.bank)
        self.assertFalse(card['bank']['needs_confirm'])
        self.vendor()
        html = self.client.get(self.home).content.decode()
        self.assertIn('the only one', html)

    def test_more_than_one_account_is_flagged_and_must_be_confirmed(self):
        acct = Account.objects.filter(association=self.prop, code='1111').first() or Account.objects.create(association=self.prop, code='1111', name='Operating Cash #2', kind='asset')
        second = BankAccount.objects.create(association=self.prop, name='Operating 2', bank_name='Truist', last4='7777', account=acct)
        self.vendor()
        up = self.drop()
        card = hub.build_card(up)
        self.assertTrue(card['bank']['needs_confirm'])
        self.assertIn('more than one bank account', ' '.join(card['blocks']).lower())
        self.assertFalse(card['can_create'])
        hub.save_choices(up, {'_card': '1', 'bank_id': str(second.pk), 'bank_confirmed': 'on'})
        card = hub.build_card(PayableUpload.objects.get(pk=up.pk))
        self.assertEqual(card['bank']['selected'], second)
        self.assertTrue(card['bank']['confirmed'])
        self.assertNotIn('bank account', ' '.join(card['blocks']).lower())

    def test_reserve_only_properties_cannot_pay_an_ordinary_invoice_and_are_blocked_with_a_link(self):
        other = make_assoc('Reserve Only Condos')
        bank(other, name='Reserves', kind='reserve', code='1120')
        up = self.drop(dict(READ, bill_to='Reserve Only Condos'))
        card = hub.build_card(up)
        self.assertEqual(card['prop'], other)
        self.assertFalse(card['can_create'])
        self.assertIn('no active operating bank account', card['bank']['message'])
        self.assertTrue(card['bank']['fix_url'].endswith('#banks'))

    def test_a_property_with_no_bank_account_is_blocked_with_a_link_to_set_one_up(self):
        other = make_assoc('No Bank Condos')
        up = self.drop(dict(READ, bill_to='No Bank Condos'))
        card = hub.build_card(up)
        self.assertEqual(card['elig'][0], 'bank')
        self.assertFalse(card['can_create'])
        html = self.client.get(self.home).content.decode()
        self.assertIn('Set up a bank account', html)
        self.assertIn(f'/accounting/{other.pk}/settings/#banks', html)
        with self.assertRaises(payables.PayableError):
            hub.create_payable(up, self.user)


class DuplicateAndBudgetTests(PayablesBase):
    def setUp(self):
        super().setUp()
        self.v = self.vendor()
        payables.create_bill(self.prop, date(2026, 10, 1), [(self.acct(), D('450.00'), 'earlier')], vendor=self.v, number='INV-1001')

    def test_the_same_invoice_number_or_the_same_amount_is_warned_and_needs_a_tick(self):
        up = self.drop()
        card = hub.build_card(up)
        self.assertEqual([why for b, why in card['duplicates']], ['the same invoice number'])
        self.assertFalse(card['can_create'])
        up2 = self.drop(dict(READ, invoice_number='DIFFERENT-9'))
        self.assertEqual([why for b, why in hub.build_card(up2)['duplicates']], ['the same amount'])
        up3 = self.drop(dict(READ, invoice_number='DIFFERENT-9', total=451.0))
        self.assertEqual(hub.build_card(up3)['duplicates'], [])
        hub.save_choices(up, {'_card': '1', 'dup_ok': 'on'})
        card = hub.build_card(PayableUpload.objects.get(pk=up.pk))
        self.assertEqual(card['blocks'], [])
        self.assertTrue(card['can_create'])

    def test_a_voided_bill_is_not_a_duplicate(self):
        Bill.objects.update(status=Bill.Status.VOID)
        self.assertEqual(hub.build_card(self.drop())['duplicates'], [])

    def test_a_budget_with_no_line_for_the_account_warns_and_never_blocks(self):
        budget = Budget.objects.create(association=self.prop, year=2026)
        BudgetLine.objects.create(budget=budget, account=self.acct('6820'), annual_amount=D('12000'))
        up = self.drop()
        hub.save_choices(up, {'_card': '1', 'account_id': str(self.acct('6800').pk), 'dup_ok': 'on', 'vendor_text': 'Acme Plumbing LLC'})
        card = hub.build_card(PayableUpload.objects.get(pk=up.pk))
        self.assertIn('not budgeted for 2026', card['budget_warning'])
        self.assertTrue(card['can_create'])

    def test_a_budgeted_account_has_no_warning(self):
        budget = Budget.objects.create(association=self.prop, year=2026)
        BudgetLine.objects.create(budget=budget, account=self.acct('6800'), annual_amount=D('1200'))
        self.assertEqual(hub.budget_warning(self.prop, self.acct('6800'), date(2026, 11, 3)), '')

    def test_no_budget_loaded_means_no_warning_at_all(self):
        self.assertEqual(hub.budget_warning(self.prop, self.acct('6800'), date(2026, 11, 3)), '')
        Budget.objects.create(association=self.prop, year=2026)                  # a budget with no lines is the same as none
        self.assertEqual(hub.budget_warning(self.prop, self.acct('6800'), date(2026, 11, 3)), '')

    def test_a_zero_budget_line_warns(self):
        budget = Budget.objects.create(association=self.prop, year=2026)
        BudgetLine.objects.create(budget=budget, account=self.acct('6800'), annual_amount=D('0'))
        BudgetLine.objects.create(budget=budget, account=self.acct('6820'), annual_amount=D('500'))
        self.assertIn('not budgeted', hub.budget_warning(self.prop, self.acct('6800'), date(2026, 11, 3)))


class AccountSuggestionTests(PayablesBase):
    def test_the_vendors_usual_account_then_what_we_last_paid_then_the_invoice_words(self):
        v = self.vendor()
        terms = VendorTerms.objects.create(association=self.prop, vendor=v, default_account=self.acct('6210'))
        self.assertEqual(hub.suggest_account(self.prop, v, {})[0], self.acct('6210'))
        terms.default_account = None
        terms.save()
        payables.create_bill(self.prop, date(2026, 9, 1), [(self.acct('6906'), D('80'), '')], vendor=v, number='X1')
        account, why = hub.suggest_account(self.prop, v, {})
        self.assertEqual((account, why), (self.acct('6906'), 'what we last paid this vendor for'))
        account, why = hub.suggest_account(self.prop, self.vendor('Brand New Vendor'), {'description': 'Landscaping maintenance for November', 'lines': []})
        self.assertEqual(account.name.lower().count('landscaping'), 1)

    def test_nothing_to_go_on_gives_no_suggestion_so_the_card_asks(self):
        v = self.vendor('Mystery Vendor')
        self.assertEqual(hub.suggest_account(self.prop, v, {'description': 'misc', 'lines': []}), (None, ''))


class ApprovalChannelTests(PayablesBase):
    def setUp(self):
        super().setUp()
        self.vendor()
        profile = ledger.profile_for(self.prop)
        profile.spending_limit = D('500.00')
        profile.save()

    def pick(self, up, **extra):
        data = {'_card': '1', 'account_id': str(self.acct('6800').pk), 'vendor_text': 'Acme Plumbing LLC', 'property_id': str(self.prop.pk), 'prev_property_id': str(self.prop.pk)}
        data.update(extra)
        hub.save_choices(up, data)
        return PayableUpload.objects.get(pk=up.pk)

    def test_the_default_comes_from_the_spending_limit(self):
        self.assertEqual(hub.default_channel(self.prop, D('500.00')), 'management')
        self.assertEqual(hub.default_channel(self.prop, D('500.01')), 'board')
        self.assertEqual(hub.default_channel(self.prop, D('50'), reserve=True), 'board')
        profile = ledger.profile_for(self.prop)
        profile.spending_limit = None
        profile.save()
        self.assertEqual(hub.default_channel(self.prop, D('99999')), 'management')

    def test_under_the_limit_it_defaults_to_management_and_is_approved_and_posted(self):
        up = self.pick(self.drop(dict(READ, total=300.0)))
        self.assertEqual(hub.build_card(up)['channel'], 'management')
        bill = hub.create_payable(up, self.user)
        self.assertEqual((bill.status, bill.approval_channel, bill.bank_account, bill.created_by), (Bill.Status.APPROVED, 'management', self.bank, self.user))
        self.assertEqual(bill.amount, D('300.00'))
        self.assertTrue(bill.entry_id)                                                     # posted to the accrual book
        self.assertEqual(JournalEntry.objects.get(pk=bill.entry_id).source, 'bill')
        self.assertEqual(bill.invoice_read['vendor'], 'Acme Plumbing LLC')
        self.assertTrue(bill.file.name.startswith('payable_uploads/'))
        up.refresh_from_db()
        self.assertEqual((up.status, up.bill_id), ('created', bill.pk))
        terms = VendorTerms.objects.get(association=self.prop, vendor=bill.vendor)
        self.assertEqual(terms.default_account, self.acct('6800'))                        # it remembers the vendor's usual account

    def test_over_the_limit_it_defaults_to_the_board_and_waits(self):
        up = self.pick(self.drop(dict(READ, total=900.0)))
        self.assertEqual(hub.build_card(up)['channel'], 'board')
        bill = hub.create_payable(up, self.user)
        self.assertEqual((bill.status, bill.approval_channel), (Bill.Status.AWAITING_BOARD, 'board'))
        self.assertIsNone(bill.entry_id)                                                   # not posted until the board approves

    def test_a_small_invoice_can_still_be_sent_to_the_board(self):
        up = self.pick(self.drop(dict(READ, total=300.0)), channel='board')
        bill = hub.create_payable(up, self.user)
        self.assertEqual((bill.status, bill.approval_channel), (Bill.Status.AWAITING_BOARD, 'board'))

    def test_a_large_invoice_can_be_approved_by_management_on_purpose(self):
        up = self.pick(self.drop(dict(READ, total=900.0)), channel='management')
        bill = hub.create_payable(up, self.user)
        self.assertEqual((bill.status, bill.approval_channel), (Bill.Status.APPROVED, 'management'))
        self.assertEqual(payables.requirement(self.prop, bill), 'staff')

    def test_reserve_spending_is_always_the_board_and_needs_a_reserve_account(self):
        up = self.pick(self.drop(dict(READ, total=100.0)), reserve='on', channel='management')
        card = hub.build_card(up)
        self.assertEqual(card['channel'], 'board')
        self.assertFalse(card['can_create'])                                               # no reserve bank account yet
        bank(self.prop, name='Reserves', kind='reserve', code='1120')
        card = hub.build_card(PayableUpload.objects.get(pk=up.pk))
        self.assertTrue(card['can_create'])
        bill = hub.create_payable(PayableUpload.objects.get(pk=up.pk), self.user)
        self.assertEqual((bill.status, bill.reserve, bill.bank_account.kind), (Bill.Status.AWAITING_BOARD, True, 'reserve'))

    def test_the_old_rule_still_applies_to_bills_made_other_ways(self):
        small = payables.create_bill(self.prop, date(2026, 11, 1), [(self.acct(), D('100'), '')], vendor=self.vendor('Other Vendor'))
        big = payables.create_bill(self.prop, date(2026, 11, 1), [(self.acct(), D('900'), '')], vendor=self.vendor('Other Vendor 2'))
        self.assertEqual((payables.requirement(self.prop, small), payables.requirement(self.prop, big)), ('staff', 'board'))

    def test_the_board_approval_action_records_who_and_moves_it_to_ready_to_pay(self):
        up = self.pick(self.drop(dict(READ, total=900.0)))
        bill = hub.create_payable(up, self.user)
        r = self.client.post(self.home, {'action': 'board_ok', 'bill': bill.pk, 'by': 'Pat Smith', 'on': '2026-11-10', 'tab': 'board'})
        self.assertEqual(r.status_code, 302)
        bill.refresh_from_db()
        self.assertEqual((bill.status, bill.board_approved_by, bill.board_approved_on), (Bill.Status.APPROVED, 'Pat Smith', date(2026, 11, 10)))
        self.assertTrue(bill.entry_id)
        self.assertEqual(bill.bank_account, self.bank)
        self.assertEqual(bill.approved_by, self.user)

    def test_a_bill_already_in_the_books_is_not_posted_again_when_the_board_approves_it(self):
        bill = payables.create_bill(self.prop, date(2026, 11, 1), [(self.acct('6800'), D('900'), '')], vendor=self.vendor('Already Posted Vendor'), kind=Bill.Kind.ROUTINE)
        bill = payables.approve_bill(bill, self.user, board_by='Pat Smith')          # approved and posted
        self.assertEqual(bill.status, Bill.Status.APPROVED)
        first_entry = bill.entry_id
        bill.status, bill.approval_channel = Bill.Status.AWAITING_BOARD, 'board'      # put on the waiting list afterwards, the entry stays
        bill.save(update_fields=['status', 'approval_channel'])
        posted = JournalEntry.objects.filter(association=self.prop, source=JournalEntry.Source.BILL).count()
        r = self.client.post(self.home, {'action': 'board_ok', 'bill': bill.pk, 'by': 'Pat Smith', 'on': '2026-11-10', 'tab': 'board'})
        self.assertEqual(r.status_code, 302)
        bill.refresh_from_db()
        self.assertEqual((bill.status, bill.board_approved_by, bill.entry_id), (Bill.Status.APPROVED, 'Pat Smith', first_entry))
        self.assertEqual(JournalEntry.objects.filter(association=self.prop, source=JournalEntry.Source.BILL).count(), posted)

    def test_a_bill_whose_entry_was_reversed_is_posted_again_on_approval(self):
        bill = payables.create_bill(self.prop, date(2026, 11, 1), [(self.acct('6800'), D('900'), '')], vendor=self.vendor('Reversed Vendor'), kind=Bill.Kind.ROUTINE)
        bill = payables.approve_bill(bill, self.user, board_by='Pat Smith')
        ledger.reverse_entry(bill.entry, date(2026, 11, 2))
        bill.status = Bill.Status.AWAITING_BOARD
        bill.save(update_fields=['status'])
        payables.approve_bill(bill, self.user, board_by='Pat Smith')
        bill.refresh_from_db()
        self.assertEqual(bill.status, Bill.Status.APPROVED)
        self.assertEqual(JournalEntry.objects.filter(association=self.prop, source=JournalEntry.Source.BILL).count(), 2)

    def test_the_board_approval_needs_a_name(self):
        up = self.pick(self.drop(dict(READ, total=900.0)))
        bill = hub.create_payable(up, self.user)
        self.client.post(self.home, {'action': 'board_ok', 'bill': bill.pk, 'by': '  ', 'tab': 'board'})
        bill.refresh_from_db()
        self.assertEqual(bill.status, Bill.Status.AWAITING_BOARD)


class PagesTests(PayablesBase):
    def test_every_tab_opens(self):
        for tab in ('review', 'board', 'ready', 'checks', 'envelopes', 'paid'):
            r = self.client.get(self.home + f'?tab={tab}')
            self.assertEqual(r.status_code, 200, tab)
        html = self.client.get(self.home).content.decode()
        for words in ('Drop invoices here', 'To review', 'Waiting for approval', 'Ready to pay', 'Print checks', 'Print envelopes', 'Paid'):
            self.assertIn(words, html)

    def test_the_print_tabs_link_to_the_check_run_when_it_exists(self):
        for name in ('payables_print_checks', 'payables_print_envelopes'):
            try:
                url = reverse(f'accounting:{name}')
            except Exception:
                continue
            tab = 'checks' if 'checks' in name else 'envelopes'
            self.assertIn(url, self.client.get(self.home + f'?tab={tab}').content.decode())

    def test_the_inbox_drafts_show_in_to_review_once_and_are_fixed_in_place(self):
        v = self.vendor('Inbox Vendor')
        draft = payables.create_bill(self.prop, date(2026, 11, 2), [(self.acct('6902'), D('120.00'), 'from email')], vendor=v, number='E-77', source=Bill.Source.EMAIL, kind=Bill.Kind.EXCEPTION)
        self.assertEqual(draft.status, Bill.Status.DRAFT)
        html = self.client.get(self.home).content.decode()
        self.assertIn('From the invoice inbox', html)
        self.assertIn('Inbox Vendor', html)
        self.client.get(self.home)
        self.assertEqual(PayableUpload.objects.filter(bill=draft).count(), 1)
        up = PayableUpload.objects.get(bill=draft)
        hub.save_choices(up, {'_card': '1', 'account_id': str(self.acct('6800').pk), 'vendor_text': 'Inbox Vendor', 'property_id': str(self.prop.pk), 'prev_property_id': str(self.prop.pk)})
        made = hub.create_payable(PayableUpload.objects.get(pk=up.pk), self.user)
        self.assertEqual(made.pk, draft.pk)                                                # the same bill, now approved
        self.assertEqual(Bill.objects.count(), 1)
        self.assertEqual((made.status, made.bank_account), (Bill.Status.APPROVED, self.bank))

    def test_discarding_an_inbox_draft_voids_it(self):
        draft = payables.create_bill(self.prop, date(2026, 11, 2), [(self.acct('6902'), D('120.00'), '')], vendor=self.vendor('Inbox Vendor'), source=Bill.Source.EMAIL)
        self.client.get(self.home)
        up = PayableUpload.objects.get(bill=draft)
        self.client.post(self.card_url(up), {'action': 'discard'})
        draft.refresh_from_db()
        self.assertEqual(draft.status, Bill.Status.VOID)
        self.assertEqual(PayableUpload.objects.get(pk=up.pk).status, 'discarded')

    def test_ready_to_pay_groups_by_association_and_bank_and_flags_a_missing_bank(self):
        v = self.vendor()
        ok = payables.create_bill(self.prop, date(2026, 11, 2), [(self.acct(), D('200'), '')], vendor=v, number='A', kind=Bill.Kind.ROUTINE)
        ok.bank_account = self.bank
        ok.save()
        old = payables.create_bill(self.prop, date(2026, 11, 3), [(self.acct(), D('300'), '')], vendor=v, number='B', kind=Bill.Kind.ROUTINE)      # approved before banks were recorded
        groups = hub.ready_groups()
        self.assertEqual(len(groups), 1)
        self.assertEqual([(g['bank'], len(g['bills'])) for g in groups[0]['banks']], [(None, 1), (self.bank, 1)])
        self.assertEqual(groups[0]['total'], D('500.00'))
        html = self.client.get(self.home + '?tab=ready').content.decode()
        self.assertIn('No bank account chosen', html)
        self.client.post(self.home, {'action': 'set_bank', 'bill': old.pk, 'bank': self.bank.pk, 'tab': 'ready'})
        old.refresh_from_db()
        self.assertEqual(old.bank_account, self.bank)

    def test_the_card_buttons_work_without_the_script_too(self):
        up = self.drop()
        r = self.client.post(self.card_url(up), {'action': 'discard'})
        self.assertRedirects(r, self.home, fetch_redirect_response=False)

    def test_the_card_script_reads_the_form_address_from_its_attribute(self):
        """The card's buttons are named "action", and a field with that name shadows form.action in the browser (it becomes the list of fields, so the request went to "[object RadioNodeList]" and
        every card button failed on the live site). The script must read the address with getAttribute."""
        import re
        from pathlib import Path
        from django.conf import settings
        up = self.drop()
        html = self.client.get(self.home).content.decode()
        form = re.search(r'<form[^>]*data-card-form[^>]*>', html).group(0)
        self.assertIn(f'action="{self.card_url(up)}"', form)
        self.assertIn('name="action"', html)                                              # the button that shadows the property
        js = (Path(settings.BASE_DIR) / 'static' / 'js' / 'payables.js').read_text(encoding='utf-8')
        code = '\n'.join(line for line in js.splitlines() if not line.strip().startswith('//'))
        self.assertNotRegex(code, r'form\.action\b')
        self.assertIn("form.getAttribute('action')", code)

    def test_the_books_account_is_a_type_to_search_box(self):
        self.drop()
        html = self.client.get(self.home).content.decode()
        self.assertRegex(html, r'<select[^>]*name="account_id"[^>]*data-combo')
        self.assertIn('combo-list', html)                                                 # its styles ship with the page
        from pathlib import Path
        from django.conf import settings
        js = (Path(settings.BASE_DIR) / 'static' / 'js' / 'payables.js').read_text(encoding='utf-8')
        self.assertIn('select[data-combo]', js)

    def test_only_an_administrator_can_use_the_page(self):
        self.client.logout()
        self.client.force_login(get_user_model().objects.create_user('staffer', password='pw12345678'))
        self.assertNotEqual(self.client.get(self.home).status_code, 200)
        self.assertNotEqual(self.client.post(self.upload_url, {'files': pdf_file()}).status_code, 200)

    def test_it_is_linked_from_financials_and_the_hub(self):
        self.assertIn(self.home, self.client.get(reverse('ws_financials')).content.decode())
        self.assertIn(self.home, self.client.get(reverse('accounting:hub', args=[self.prop.pk])).content.decode())

    def test_the_refresh_keeps_choices_and_clears_the_account_when_the_property_changes(self):
        other = make_assoc('Other Condos')
        bank(other, name='Operating', last4='1234')
        up = self.drop()
        hub.save_choices(up, {'_card': '1', 'account_id': str(self.acct('6800').pk), 'property_id': str(self.prop.pk), 'prev_property_id': str(self.prop.pk)})
        self.assertEqual(PayableUpload.objects.get(pk=up.pk).draft['account_id'], self.acct('6800').pk)
        hub.save_choices(up, {'_card': '1', 'property_id': str(other.pk), 'prev_property_id': str(self.prop.pk)})
        draft = PayableUpload.objects.get(pk=up.pk).draft
        self.assertNotIn('account_id', draft)
        self.assertEqual(hub.build_card(PayableUpload.objects.get(pk=up.pk))['prop'], other)

    def test_a_property_that_is_not_ours_to_pay_for_cannot_be_created_against(self):
        up = self.drop(dict(READ, bill_to='Unbooked HOA'))
        Property.objects.create(name='Unbooked HOA', property_type=Property.Type.ASSOCIATION)
        card = hub.build_card(up)
        self.assertEqual(card['elig'][0], 'books')
        with self.assertRaises(payables.PayableError):
            hub.create_payable(up, self.user)
