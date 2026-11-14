"""The check run's day-to-day workflow: ticking checks, vendors the bank pays by itself, envelopes that wait until printed or skipped, voiding a payable,
the signature and the top margin on the check face."""
import io
import shutil
import tempfile
from datetime import date
from decimal import Decimal as D
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image
from pypdf import PdfReader

from core.models import Contact

from . import check_runs as cr
from . import checkface, ledger, payables
from .models import BankAccount, Bill, Payment, VendorProfile, VendorTerms
from .tests import make_assoc

STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}


def png_bytes(size=(300, 90), color=(10, 30, 120, 255)):
    buf = io.BytesIO()
    Image.new('RGBA', size, color).save(buf, 'PNG')
    return buf.getvalue()


class Base(TestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, True)
        self.override = override_settings(STORAGES=STORAGES, MEDIA_ROOT=self.media)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.alpha = make_assoc('Alpha Condos')
        self.bank_a = BankAccount(association=self.alpha, name='Alpha Operating', bank_name='Truist', last4='1111', kind='operating', account=ledger.account(self.alpha, '1110'), routing_number='000000000',
                                  bank_address='1 Bank Plaza, Miami, FL 33131', payer_name='Alpha Condos Association, Inc.', payer_address='9 Main Street, Miami, FL 33131')
        self.bank_a.account_number = '0000001111'
        self.bank_a.save()
        self.acme = Contact.objects.create(name='Acme Pest Control', contact_type=Contact.ContactType.VENDOR)
        self.zed = Contact.objects.create(name='Zed Roofing', contact_type=Contact.ContactType.VENDOR)
        self.us = Contact.objects.create(name='Proper Realty Co', contact_type=Contact.ContactType.VENDOR)
        self.user = get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678')
        self.client.force_login(self.user)
        self.url = reverse('accounting:payables_print_checks')
        self.env_url = reverse('accounting:payables_print_envelopes')

    def bill(self, vendor, number, amount, due=date(2026, 10, 20), **kw):
        return payables.create_bill(self.alpha, date(2026, 10, 1), [('6906', D(amount), '')], vendor=vendor, number=number, due=due, kind=Bill.Kind.ROUTINE, **kw)

    def address(self, vendor, street='12 Oak St', city='Miami', state='FL', zip_code='33131'):
        return VendorProfile.objects.create(contact=vendor, street=street, city=city, state=state, zip_code=zip_code)

    def print_bills(self, *bills, date_='2026-11-14'):
        return self.client.post(self.url, {'action': 'print', 'date': date_, 'bill': [b.pk for b in bills]})


class TickingAndAutopayTests(Base):
    def test_every_check_has_a_checkbox_and_the_page_can_select_all_or_none(self):
        self.bill(self.acme, 'A-1', '100.00')
        self.bill(self.acme, 'A-2', '50.00')
        html = self.client.get(self.url).content.decode()
        self.assertIn('data-master', html)                                                 # a box for the whole check
        self.assertIn('data-select="all"', html)
        self.assertIn('data-select="none"', html)
        import re
        self.assertEqual(len(re.findall(r'<input type="checkbox"[^>]*name="bill"', html)), 2)      # the invoice boxes still carry what is paid

    def test_only_the_ticked_checks_are_printed(self):
        a, z = self.bill(self.acme, 'A-1', '100.00'), self.bill(self.zed, 'Z-1', '200.00')
        r = self.print_bills(a)
        self.assertEqual(r['Content-Type'], 'application/pdf')
        a.refresh_from_db(), z.refresh_from_db()
        self.assertEqual((a.status, z.status), (Bill.Status.PAID, Bill.Status.APPROVED))
        self.assertEqual(Payment.objects.filter(method='check').count(), 1)

    def test_a_vendor_the_bank_pays_by_itself_is_not_in_the_check_run(self):
        a, z = self.bill(self.acme, 'A-1', '100.00'), self.bill(self.zed, 'Z-1', '200.00')
        VendorTerms.objects.create(association=self.alpha, vendor=self.zed, pay_method=VendorTerms.Pay.ACH)
        plan = cr.plan()
        self.assertEqual([c['payee'] for g in plan['groups'] for c in g['checks']], ['Acme Pest Control'])
        self.assertEqual([b.pk for b in plan['autopay']], [z.pk])
        html = self.client.get(self.url).content.decode()
        self.assertIn('Paid by the bank (1)', html)
        self.assertIn('Already paid', html)
        self.print_bills(a, z)                                                             # even if its id is posted, no check is written for it
        z.refresh_from_db()
        self.assertEqual(z.status, Bill.Status.APPROVED)

    def test_recording_an_automatic_payment_pays_the_bill_without_using_a_check_number(self):
        z = self.bill(self.zed, 'Z-1', '200.00')
        VendorTerms.objects.create(association=self.alpha, vendor=self.zed, pay_method=VendorTerms.Pay.ACH)
        before = ledger.profile_for(self.alpha).next_check_number
        r = self.client.post(self.url, {'action': 'mark_paid', 'bill': z.pk, 'date': '2026-10-16'}, follow=True)
        self.assertContains(r, 'recorded as paid by the bank')
        z.refresh_from_db()
        self.assertEqual(z.status, Bill.Status.PAID)
        p = Payment.objects.get()
        self.assertEqual((p.method, p.date, p.number, p.amount, p.envelope_state), ('ach', date(2026, 10, 16), '', D('200.00'), ''))
        self.assertEqual(ledger.profile_for(self.alpha).next_check_number, before)


class VoidTests(Base):
    def test_a_payable_can_be_voided_from_the_check_run(self):
        a = self.bill(self.acme, 'A-1', '100.00')
        r = self.client.post(self.url, {'action': 'void_bill', 'bill': a.pk}, follow=True)
        self.assertContains(r, 'is void')
        a.refresh_from_db()
        self.assertEqual(a.status, Bill.Status.VOID)

    def test_a_balance_that_came_over_from_the_old_system_offers_already_paid_not_void(self):
        a = self.bill(self.acme, 'A-1', '100.00', source=Bill.Source.OPENING)
        html = self.client.get(self.url).content.decode()
        self.assertIn('Already paid', html)
        self.assertNotIn('form="voidForm"', html)
        r = self.client.post(self.url, {'action': 'void_bill', 'bill': a.pk}, follow=True)
        self.assertContains(r, 'opening balances')
        a.refresh_from_db()
        self.assertEqual(a.status, Bill.Status.APPROVED)

    def test_the_payable_page_can_void_or_record_it_as_paid(self):
        a, z = self.bill(self.acme, 'A-1', '100.00'), self.bill(self.zed, 'Z-1', '200.00')
        detail = lambda b: reverse('accounting:bill', args=[self.alpha.pk, b.pk])
        html = self.client.get(detail(a)).content.decode()
        self.assertIn('value="void"', html)
        self.assertIn('value="mark_paid"', html)
        self.client.post(detail(a), {'action': 'void'})
        a.refresh_from_db()
        self.assertEqual(a.status, Bill.Status.VOID)
        self.client.post(detail(z), {'action': 'mark_paid', 'date': '2026-10-31'})
        z.refresh_from_db()
        self.assertEqual(z.status, Bill.Status.PAID)


class EnvelopeQueueTests(Base):
    def setUp(self):
        super().setUp()
        for v in (self.acme, self.zed):
            self.address(v)

    def run_of(self, *vendors):
        bills = [self.bill(v, f'N-{i}-{v.pk}', '100.00') for i, v in enumerate(vendors)]
        self.print_bills(*bills)
        return bills

    def test_a_printed_check_waits_for_its_envelope(self):
        self.run_of(self.acme)
        p = Payment.objects.get()
        self.assertEqual(p.envelope_state, 'waiting')
        self.assertEqual([x.pk for x in cr.waiting_envelopes()], [p.pk])

    def test_envelopes_from_two_runs_pile_up_until_they_are_dealt_with(self):
        self.run_of(self.acme)
        self.run_of(self.zed)
        html = self.client.get(self.env_url).content.decode()
        self.assertIn('Acme Pest Control', html)
        self.assertIn('Zed Roofing', html)
        self.assertIn('2 waiting', html)

    def test_printing_ticked_envelopes_marks_them_printed_and_leaves_the_rest(self):
        self.run_of(self.acme, self.zed)
        acme_check = Payment.objects.get(vendor=self.acme)
        r = self.client.post(self.env_url, {'action': 'print', 'id': [acme_check.pk]})
        self.assertEqual(r['Content-Type'], 'application/pdf')
        self.assertEqual(len(PdfReader(io.BytesIO(r.content)).pages), 1)
        acme_check.refresh_from_db()
        self.assertEqual(acme_check.envelope_state, 'printed')
        self.assertEqual([p.payee for p in cr.waiting_envelopes()], ['Zed Roofing'])

    def test_skipping_takes_an_envelope_off_the_list_and_it_can_be_put_back(self):
        self.run_of(self.acme)
        p = Payment.objects.get()
        self.client.post(self.env_url, {'action': 'skipped', 'id': [p.pk]})
        p.refresh_from_db()
        self.assertEqual(p.envelope_state, 'skipped')
        self.assertEqual(cr.waiting_envelopes(), [])
        self.assertContains(self.client.get(self.env_url), 'Put back')
        self.client.post(self.env_url, {'action': 'waiting', 'id': [p.pk]})
        p.refresh_from_db()
        self.assertEqual(p.envelope_state, 'waiting')

    def test_a_check_to_proper_realty_starts_unticked(self):
        self.address(self.us)
        self.run_of(self.us, self.acme)
        r = self.client.get(self.env_url)
        ticked = {row['owner']: row['ticked'] for row in r.context['rows']}
        self.assertEqual(ticked, {'Proper Realty Co': False, 'Acme Pest Control': True})
        self.assertContains(r, 'Our own check: not mailed.')

    def test_a_voided_check_drops_off_the_envelope_list(self):
        self.run_of(self.acme)
        p = Payment.objects.get()
        self.client.post(self.url, {'action': 'void', 'payment': p.pk})
        p.refresh_from_db()
        self.assertEqual(p.envelope_state, '')
        self.assertEqual(cr.waiting_envelopes(), [])

    def test_a_vendor_address_saved_on_the_vendor_profile_reaches_the_envelope(self):
        """It used to look somewhere else, so a vendor made from an invoice never had an address on its envelope."""
        vendor = Contact.objects.create(name='Total Power', contact_type=Contact.ContactType.VENDOR)
        VendorProfile.objects.create(contact=vendor, street='1330 West Industrial Avenue, #107', city='Boynton Beach', state='FL', zip_code='33426')
        self.assertEqual(cr.vendor_address_lines(vendor), ['1330 West Industrial Avenue, #107', 'Boynton Beach, FL 33426'])

    def test_an_invoice_street_with_a_bar_in_it_is_saved_as_one_clean_line(self):
        from . import payables_hub as hub
        from .models import PayableUpload
        up = PayableUpload.objects.create(read={'vendor': 'Total Power Generation LLC', 'vendor_street': '1330 West Industrial Avenue | #107', 'vendor_city': 'Boynton Beach', 'vendor_state': 'FL', 'vendor_zip': '33426'}, draft={})
        vendor = hub.create_vendor(up)
        self.assertEqual(vendor.vendor_profile.street, '1330 West Industrial Avenue, #107')
        self.assertEqual(vendor.vendor_profile.address_lines, ['1330 West Industrial Avenue, #107', 'Boynton Beach, FL 33426'])


class CheckFaceTests(Base):
    def test_the_whole_page_is_drawn_5_mm_lower_but_the_guides_stay_with_the_stock(self):
        c = mock.MagicMock()
        data = checkface.sample_data(self.alpha)
        with mock.patch.object(checkface, '_draw_face'), mock.patch.object(checkface, '_draw_stub'), mock.patch.object(checkface, '_draw_guides') as guides:
            order = []
            c.translate.side_effect = lambda *a: order.append(('translate', a))
            c.restoreState.side_effect = lambda: order.append(('restore', ()))
            guides.side_effect = lambda cc: order.append(('guides', ()))
            checkface.draw_check(c, data, guides=True)
        self.assertEqual(order[0], ('translate', (0, -checkface.TOP_MARGIN)))
        self.assertAlmostEqual(checkface.TOP_MARGIN, 14.17, delta=0.1)                      # 5 mm
        self.assertEqual([name for name, _ in order], ['translate', 'restore', 'guides'])   # the guides are drawn after the shift is undone

    def test_a_signature_on_file_is_drawn_on_the_check_and_none_leaves_the_line_empty(self):
        plain = checkface.draw_check
        profile = ledger.profile_for(self.alpha)
        from reportlab.pdfgen import canvas

        def pdf(data):
            buf = io.BytesIO()
            c = canvas.Canvas(buf, pagesize=(checkface.W, checkface.H))
            plain(c, data)
            c.showPage(), c.save()
            return buf.getvalue()
        without = pdf(checkface.sample_data(self.alpha))
        self.assertNotIn(b'/Subtype /Image', without)
        profile.check_signature.save('sig.png', SimpleUploadedFile('sig.png', png_bytes()), save=True)
        data = checkface.sample_data(self.alpha)
        self.assertTrue(data['signature'])
        self.assertIn(b'/Subtype /Image', pdf(data))
        data['signature'] = b'not an image'                                                  # a damaged file must not break the check
        self.assertNotIn(b'/Subtype /Image', pdf(data))

    def test_the_signature_is_on_the_real_check_too(self):
        profile = ledger.profile_for(self.alpha)
        profile.check_signature.save('sig.png', SimpleUploadedFile('sig.png', png_bytes()), save=True)
        a = self.bill(self.acme, 'A-1', '100.00')
        r = self.print_bills(a)
        self.assertIn(b'/Subtype /Image', r.content)


class SignatureSettingsTests(Base):
    def setUp(self):
        super().setUp()
        self.beta = make_assoc('Beta Condos')
        self.settings_url = reverse('accounting:settings', args=[self.alpha.pk])

    def upload(self, content=None, name='sig.png', **extra):
        return self.client.post(self.settings_url, {'action': 'signature', 'signature': SimpleUploadedFile(name, content or png_bytes()), **extra}, follow=True)

    def test_a_signature_image_is_saved_and_shown(self):
        r = self.upload()
        self.assertContains(r, 'will print on this association')
        self.assertTrue(ledger.profile_for(self.alpha).check_signature)
        self.assertFalse(ledger.profile_for(self.beta).check_signature)
        img = self.client.get(reverse('accounting:check_signature', args=[self.alpha.pk]))
        self.assertEqual(img.status_code, 200)
        self.assertEqual(self.client.get(reverse('accounting:check_signature', args=[self.beta.pk])).status_code, 404)

    def test_one_upload_can_cover_every_association(self):
        self.upload(all_associations='1')
        self.assertTrue(ledger.profile_for(self.beta).check_signature)
        self.assertEqual(checkface.signature_bytes(ledger.profile_for(self.beta)), checkface.signature_bytes(ledger.profile_for(self.alpha)))

    def test_something_that_is_not_a_picture_is_refused(self):
        r = self.upload(b'just some text', 'sig.png')
        self.assertContains(r, 'not a picture')
        self.assertFalse(ledger.profile_for(self.alpha).check_signature)

    def test_a_signature_can_be_removed_without_touching_the_other_associations(self):
        self.upload(all_associations='1')
        self.client.post(self.settings_url, {'action': 'signature_remove'})
        self.assertFalse(ledger.profile_for(self.alpha).check_signature)
        self.assertTrue(ledger.profile_for(self.beta).check_signature)

    def test_only_an_administrator_can_see_the_signature(self):
        self.upload()
        self.client.logout()
        self.client.force_login(get_user_model().objects.create_user('staffer', password='pw12345678'))
        self.assertNotEqual(self.client.get(reverse('accounting:check_signature', args=[self.alpha.pk])).status_code, 200)


class OwnerPaymentsEntryTests(Base):
    def test_financials_has_a_tile_that_leads_to_recording_an_owner_payment(self):
        html = self.client.get(reverse('ws_financials')).content.decode()
        self.assertIn(reverse('accounting:receipt_pick'), html)
        self.assertIn('Record owner payments', html)

    def test_the_picker_lists_the_associations_whose_books_we_keep(self):
        make_assoc('Beta Condos')
        r = self.client.get(reverse('accounting:receipt_pick'))
        self.assertEqual(r.status_code, 200)
        profile = ledger.profile_for(self.alpha)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, reverse('accounting:receipt_new', args=[self.alpha.pk]))
        self.assertContains(r, 'Beta Condos')
