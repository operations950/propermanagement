"""The bank details a check needs and the built-in check face for the blank 3-part stock. Only dummy numbers (000000000 style) appear here."""
import io
import shutil
import tempfile
from datetime import date
from decimal import Decimal as D
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.urls import reverse
from pypdf import PdfReader
from reportlab.pdfgen import canvas

from core.models import Contact

from . import check_runs as cr
from . import checkface, fieldcrypt, ledger, payables
from .models import BankAccount, Bill, Payment
from .tests import make_assoc

STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}
DAY = date(2026, 11, 14)
ACCOUNT = '0000004465'          # a dummy: every number in these tests is made up
GOOD_ROUTING = '011000015'      # a valid routing number (the check digit works)


def pdf_text(data):
    return [p.extract_text() for p in PdfReader(io.BytesIO(data)).pages]


class RoutingNumberTests(TestCase):
    def test_the_check_digit_decides(self):
        self.assertTrue(checkface.aba_ok('000000000'))
        self.assertTrue(checkface.aba_ok(GOOD_ROUTING))
        self.assertFalse(checkface.aba_ok('123456789'))
        self.assertFalse(checkface.aba_ok('01100001'))

    def test_a_clear_message_says_what_is_wrong(self):
        self.assertEqual(checkface.clean_routing(' 011-000-015 '), GOOD_ROUTING)
        self.assertEqual(checkface.clean_routing(''), '')
        with self.assertRaisesMessage(ValueError, 'has 9 digits; this has 8'):
            checkface.clean_routing('01100001')
        with self.assertRaisesMessage(ValueError, 'check digit is wrong'):
            checkface.clean_routing('123456789')


class AccountNumberStorageTests(TestCase):
    def setUp(self):
        self.prop = make_assoc('Truist Condos')
        self.bank = BankAccount(association=self.prop, name='Operating', bank_name='Truist', last4='4465', account=ledger.account(self.prop, '1110'))
        self.bank.account_number = ACCOUNT
        self.bank.save()

    def test_it_round_trips_but_the_database_column_is_not_the_number(self):
        bank = BankAccount.objects.get(pk=self.bank.pk)
        self.assertEqual(bank.account_number, ACCOUNT)
        with connection.cursor() as cursor:
            cursor.execute('select account_number_enc from accounting_bankaccount where id = %s', [self.bank.pk])
            stored = cursor.fetchone()[0]
        self.assertNotIn(ACCOUNT, stored)
        self.assertNotIn('4465', stored)
        self.assertTrue(stored.startswith('gAAAA'))                          # a Fernet token

    def test_it_is_shown_masked_and_never_in_the_name_or_label(self):
        self.assertEqual(self.bank.masked_account_number, '•••• ••••• 4465')
        for text in (str(self.bank), self.bank.label, repr(self.bank.masked_account_number)):
            self.assertNotIn(ACCOUNT, text)
        self.assertNotIn('0000004465'[:6] + '4465', self.bank.label)

    def test_a_number_that_cannot_be_read_asks_to_be_typed_again(self):
        with override_settings(FIELD_ENCRYPTION_KEY='some other key entirely'):
            bank = BankAccount.objects.get(pk=self.bank.pk)
            self.assertTrue(bank.account_number_unreadable)
            self.assertEqual(bank.account_number, '')
            self.assertIn('account number', bank.print_gaps())

    def test_a_key_setting_makes_the_number_independent_of_the_secret_key(self):
        with override_settings(FIELD_ENCRYPTION_KEY='a long random phrase kept in the environment'):
            token = fieldcrypt.encrypt(ACCOUNT)
            self.assertEqual(fieldcrypt.decrypt(token), ACCOUNT)
        with override_settings(FIELD_ENCRYPTION_KEY='a long random phrase kept in the environment', SECRET_KEY='something else'):
            self.assertEqual(fieldcrypt.decrypt(token), ACCOUNT)
        self.assertEqual(fieldcrypt.encrypt(''), '')
        self.assertEqual(fieldcrypt.decrypt(''), '')


class MicrLineTests(TestCase):
    def test_the_line_is_in_the_standard_business_check_order(self):
        self.assertEqual(checkface.micr_line('067005158', '4407131101', 123), 'C000123C A067005158A 4407131101C')
        self.assertEqual(checkface.micr_line('000000000', '0000004465', '237'), 'C000237C A000000000A 0000004465C')
        self.assertEqual(checkface.micr_line('', '0000004465', 5), '')

    def test_the_font_is_registered_and_its_glyphs_are_nine_points_wide(self):
        name, size, is_micr = checkface.micr_font()
        self.assertEqual((name, size, is_micr), ('GnuMICR', 12.0, True))
        from reportlab.pdfbase.pdfmetrics import stringWidth
        self.assertAlmostEqual(stringWidth('C000123C A000000000A 0000004465C', 'GnuMICR', 12), 32 * 9.012, places=1)

    def test_without_the_font_files_plain_digits_in_courier_bold_are_used(self):
        with mock.patch.object(checkface, 'FONT_DIR', checkface.FONT_DIR / 'missing'), mock.patch.dict(checkface._micr_state, clear=True), \
                mock.patch('reportlab.pdfbase.pdfmetrics.getRegisteredFontNames', return_value=[]):
            self.assertEqual(checkface.micr_font(), ('Courier-Bold', 11.0, False))
            self.assertEqual(checkface.numbers_text('000000000', ACCOUNT, 123), '000123 000000000 0000004465')


@override_settings(STORAGES=STORAGES)
class CheckFaceTests(TestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, True)
        override = override_settings(STORAGES=STORAGES, MEDIA_ROOT=self.media)
        override.enable()
        self.addCleanup(override.disable)
        self.prop = make_assoc('Lakeside Condos')
        self.bank = self.make_bank(self.prop, 'Operating', '4465')
        self.acme = Contact.objects.create(name='Acme Pest Control', contact_type=Contact.ContactType.VENDOR)
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.url = reverse('accounting:payables_print_checks')
        profile = ledger.profile_for(self.prop)
        profile.next_check_number = 237
        profile.save()

    def make_bank(self, prop, name, last4, ready=True):
        bank = BankAccount(association=prop, name=name, bank_name='Truist', last4=last4, account=ledger.account(prop, '1110'))
        if ready:
            bank.routing_number, bank.bank_address = GOOD_ROUTING, '214 N Tryon St, Charlotte, NC 28202'
            bank.payer_name, bank.payer_address = 'Lakeside Condominium Association, Inc.', '100 Lakeside Dr, Miami, FL 33131'
            bank.account_number = '000000' + last4
        bank.save()
        return bank

    def bill(self, number, amount, due):
        return payables.create_bill(self.prop, date(2026, 10, 1), [('6906', D(amount), '')], vendor=self.acme, number=number, due=due, kind=Bill.Kind.ROUTINE)

    def print_checks(self):
        self.bill('A-100', '250.00', date(2026, 10, 20))
        self.bill('A-101', '300.00', date(2026, 11, 20))
        response = self.client.post(self.url, {'action': 'print', 'date': '2026-11-14', **{'bill': [str(b.pk) for b in Bill.objects.all()]}})
        self.assertEqual(response['Content-Type'], 'application/pdf')
        return response.content

    # ---- the page
    def test_the_check_has_every_part_and_both_stubs_with_their_invoices(self):
        data = self.print_checks()
        reader = PdfReader(io.BytesIO(data))
        self.assertEqual(len(reader.pages), 1)
        self.assertEqual([float(x) for x in reader.pages[0].mediabox[2:]], [612.0, 792.0])
        text = reader.pages[0].extract_text()
        for needle in ('PAY TO THE ORDER OF', 'Acme Pest Control', '550.00', 'Five hundred fifty and 00/100', 'DOLLARS', '11/14/2026', 'DATE', 'MEMO', 'AUTHORIZED SIGNATURE',
                       'Lakeside Condominium Association, Inc.', '100 Lakeside Dr', 'Truist', '214 N Tryon St', '237',
                       'VENDOR COPY', 'RECORDS COPY', 'Check No. 237', 'A-100', 'A-101', '250.00', '300.00', 'Total  $550.00', 'Lakeside Condos  -  Operating'):
            self.assertIn(needle, text)
        self.assertEqual(text.count('Check No. 237'), 2)                                        # on both stubs
        self.assertEqual(text.count('A-101'), 5)                                                # the memo on the check, then the memo line and the invoice row on each of the two stubs
        self.assertNotIn('6906', text)                                                          # no books account on the printed check or either stub
        self.assertNotIn('BOOKS ACCOUNT', text)
        self.assertNotIn('approved', text.lower())                                              # and no "Management approved: name" line

    def test_the_bank_numbers_line_is_the_micr_string_and_the_full_number_appears_only_there(self):
        text = pdf_text(self.print_checks())[0]
        line = f'C000237C A{GOOD_ROUTING}A 0000004465C'
        self.assertEqual(checkface.micr_line(GOOD_ROUTING, '0000004465', 237), line)
        self.assertIn(line, text)
        self.assertEqual(text.count('0000004465'), 1)                                           # nowhere else on the page: the stubs show only the masked label
        self.assertNotIn('•', text.replace('•••• ••••• 4465', ''))

    def test_payment_posting_is_unchanged_by_the_new_face(self):
        self.print_checks()
        payment = Payment.objects.get()
        self.assertEqual((payment.number, payment.amount, payment.method, payment.bank_account_id), ('237', D('550.00'), 'check', self.bank.pk))
        self.assertEqual(ledger.profile_for(self.prop).next_check_number, 238)

    def test_the_standard_face_leaves_the_seal_and_hologram_clear(self):
        class Spy(canvas.Canvas):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self.drawn = []

            def drawString(self, x, y, text, *a, **k):
                self.drawn.append((x, 792 - y, text, self._fontname, self._fontsize))
                return super().drawString(x, y, text, *a, **k)
        data = checkface.sample_data(self.prop)
        c = Spy(io.BytesIO(), pagesize=(612, 792))
        checkface.draw_check(c, data)
        left, top, right, bottom = checkface.KEEP_OUT
        from reportlab.pdfbase.pdfmetrics import stringWidth
        for x, y, text, font, size in c.drawn:
            if 4.7 < y < 244.3 and text:
                width = stringWidth(text, font, size)
                clear = (x + width < left or x > right or y + 2 < top or y - size > bottom)
                self.assertTrue(clear, f'{text!r} runs over the seal at {x:.0f},{y:.0f}')
        micr = [d for d in c.drawn if d[3] == 'GnuMICR'][0]
        self.assertAlmostEqual(micr[0], 26.0)                                                    # a quarter inch in from the left of the face
        self.assertLess(micr[1], 244.3)
        self.assertGreater(micr[1] - 8.4, 244.3 - 45)                                            # inside the bottom five-eighths inch band

    def test_many_invoices_still_fit_on_the_stubs(self):
        for i in range(30):
            self.bill(f'INV-{i:03d}', '10.00', date(2026, 11, 1) if i % 2 else date(2026, 10, 1))
        data = self.client.post(self.url, {'action': 'print', 'date': '2026-11-14', 'bill': [str(b.pk) for b in Bill.objects.all()]}).content
        text = pdf_text(data)[0]
        self.assertIn('INV-000', text)
        self.assertIn('more invoices', text)                                                     # the overflow says so instead of running off the stub
        self.assertEqual(len(PdfReader(io.BytesIO(data)).pages), 1)

    # ---- the test print
    def test_the_test_print_says_test_page_and_uses_no_real_numbers(self):
        test_url = reverse('accounting:payables_checks_pdf') + f'?test={self.prop.pk}'
        plain = self.client.get(test_url)
        self.assertIn('TEST PAGE', pdf_text(plain.content)[0])
        self.assertIn('C000123C A000000000A 0000000000C', pdf_text(plain.content)[0])
        self.assertNotIn(GOOD_ROUTING, pdf_text(plain.content)[0])
        guides = self.client.get(test_url + '&guides=1')
        self.assertIn('TEST PAGE', pdf_text(guides.content)[0])
        self.assertIn('seal and hologram', pdf_text(guides.content)[0])
        self.assertNotIn('seal and hologram', pdf_text(plain.content)[0])
        self.assertEqual(Payment.objects.count(), 0)

    def test_a_reprint_draws_the_same_face(self):
        self.print_checks()
        payment = Payment.objects.get()
        again = self.client.get(reverse('accounting:payables_checks_pdf') + f'?payment={payment.pk}')
        self.assertIn('Check No. 237', pdf_text(again.content)[0])
        self.assertEqual(Payment.objects.count(), 1)

    # ---- a bank account that is not ready
    def test_print_checks_lists_a_bank_account_with_missing_details_under_cant_print_yet(self):
        BankAccount.objects.all().delete()
        bank = self.make_bank(self.prop, 'Operating', '4465', ready=False)
        self.bill('A-100', '250.00', date(2026, 10, 20))
        result = cr.plan()
        self.assertEqual(result['groups'], [])
        self.assertEqual(len(result['blocked']), 1)
        self.assertEqual(result['blocked'][0]['gaps'], ['routing number', 'account number', 'payer name', 'payer address'])
        html = self.client.get(self.url).content.decode()
        self.assertIn("Can&#x27;t print yet", html.replace("'", '&#x27;'))
        self.assertIn('Missing: routing number, account number, payer name, payer address', html)
        self.assertIn(reverse('accounting:settings', args=[self.prop.pk]) + '#banks', html)
        self.assertEqual(self.client.post(self.url, {'action': 'print', 'date': '2026-11-14', 'bill': [str(b.pk) for b in Bill.objects.all()]}).status_code, 302)
        self.assertEqual(Payment.objects.count(), 0)
        bank.routing_number, bank.payer_name, bank.payer_address = GOOD_ROUTING, 'Lakeside Condominium Association, Inc.', '100 Lakeside Dr, Miami, FL 33131'
        bank.account_number = ACCOUNT
        bank.save()
        self.assertEqual(cr.plan()['blocked'], [])

    def test_the_no_check_layout_warning_is_gone_for_the_default_face(self):
        self.bill('A-100', '250.00', date(2026, 10, 20))
        self.assertFalse(any('No check layout' in w['text'] for w in cr.plan()['groups'][0]['warnings']))
        self.assertNotIn('Set up the check stock', self.client.get(self.url).content.decode())

    def test_an_association_with_a_learned_layout_keeps_it(self):
        from . import checkprint
        BankAccount.objects.all().delete()
        self.make_bank(self.prop, 'Operating', '4465', ready=False)                              # no numbers needed: the stock is pre-printed
        profile = ledger.profile_for(self.prop)
        profile.check_layout = dict(checkprint.DEFAULT_LAYOUT, learned=True)
        profile.save()
        self.bill('A-100', '250.00', date(2026, 10, 20))
        self.assertEqual(cr.plan()['blocked'], [])
        text = pdf_text(self.client.post(self.url, {'action': 'print', 'date': '2026-11-14', 'bill': [str(b.pk) for b in Bill.objects.all()]}).content)[0]
        self.assertIn('Nov 14, 2026', text)                                                      # the old layout's date style
        self.assertNotIn('VENDOR COPY', text)


@override_settings(STORAGES=STORAGES)
class BankEditorTests(TestCase):
    def setUp(self):
        self.prop = make_assoc('Truist Condos')
        self.bank = BankAccount(association=self.prop, name='Operating', bank_name='Truist', last4='4465', account=ledger.account(self.prop, '1110'))
        self.bank.account_number = ACCOUNT
        self.bank.routing_number = GOOD_ROUTING
        self.bank.save()
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.url = reverse('accounting:settings', args=[self.prop.pk])

    def edit(self, **changes):
        data = {'action': 'bank_edit', 'bank': self.bank.pk, 'name': 'Operating', 'bank_name': 'Truist', 'last4': '4465', 'kind': 'operating', 'account': self.bank.account_id,
                'routing_number': GOOD_ROUTING, 'bank_address': '214 N Tryon St, Charlotte, NC', 'payer_name': 'Truist Condominium Association, Inc.', 'payer_address': '5 Oak St, Miami, FL 33131', 'account_number': '', **changes}
        return self.client.post(self.url, data, follow=True)

    def said(self, response):
        return ' '.join(str(m) for m in response.context['messages'])

    def test_saving_the_details_keeps_the_account_number_when_that_box_is_blank(self):
        response = self.edit()
        self.assertIn('Saved', self.said(response))
        bank = BankAccount.objects.get(pk=self.bank.pk)
        self.assertEqual((bank.routing_number, bank.payer_name, bank.payer_address, bank.bank_address), (GOOD_ROUTING, 'Truist Condominium Association, Inc.', '5 Oak St, Miami, FL 33131', '214 N Tryon St, Charlotte, NC'))
        self.assertEqual(bank.account_number, ACCOUNT)
        self.assertEqual(bank.print_gaps(), [])

    def test_a_new_account_number_replaces_it_and_the_last4_follows(self):
        self.edit(account_number='000-000 7788', last4='4465')
        bank = BankAccount.objects.get(pk=self.bank.pk)
        self.assertEqual((bank.account_number, bank.last4), ('0000007788', '7788'))

    def test_a_bad_routing_number_is_refused_with_a_clear_message_and_nothing_changes(self):
        response = self.edit(routing_number='123456789', payer_name='Changed')
        self.assertIn('check digit is wrong', self.said(response))
        bank = BankAccount.objects.get(pk=self.bank.pk)
        self.assertEqual((bank.routing_number, bank.payer_name), (GOOD_ROUTING, ''))
        self.assertIn('has 9 digits; this has 5', self.said(self.edit(routing_number='12345')))

    def test_an_account_number_with_letters_or_the_wrong_length_is_refused(self):
        self.assertIn('4 to 17 digits', self.said(self.edit(account_number='12AB')))
        self.assertIn('4 to 17 digits', self.said(self.edit(account_number='123')))
        self.assertEqual(BankAccount.objects.get(pk=self.bank.pk).account_number, ACCOUNT)

    def test_a_last4_that_disagrees_with_the_number_on_file_is_refused(self):
        self.assertIn('do not match the account number on file', self.said(self.edit(last4='9999')))

    def test_the_page_shows_the_number_masked_and_never_in_full(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn('•••• ••••• 4465', html)
        self.assertNotIn(ACCOUNT, html)
        self.assertNotIn('value="0000004465"', html)
        self.assertIn('name="account_number"', html)                                             # the Change box exists, empty

    def test_the_readiness_chip_says_ready_or_what_is_missing(self):
        self.assertIn('Not ready to print checks: needs payer name, payer address', self.client.get(self.url).content.decode())
        self.edit()
        html = self.client.get(self.url).content.decode()
        self.assertIn('Ready to print checks', html)
        self.assertNotIn('Not ready to print checks', html)

    def test_the_settings_list_says_whether_check_printing_is_ready(self):
        from . import views_setup

        def row():
            return next(r for r in views_setup.settings_rows(self.prop) if r['label'] == 'Check printing')
        self.assertIn('Missing for', row()['value'])
        self.assertFalse(row()['ok'])
        self.edit()
        self.assertIn('Ready: standard 3-part check face', row()['value'])
        self.assertTrue(row()['ok'])

    def test_only_an_admin_can_open_the_editor(self):
        self.client.logout()
        self.client.force_login(get_user_model().objects.create_user('staffer', password='pw12345678'))
        self.assertNotEqual(self.client.get(self.url).status_code, 200)
