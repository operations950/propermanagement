"""Two fixes from the Castlewood go-live: a contra-asset (the allowance for doubtful accounts) reduces the assets on the balance sheet, and a PDF too big for the file storage is split into parts."""
import io
from datetime import date
from decimal import Decimal as D
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from pypdf import PdfReader, PdfWriter

from accounting import documents, ledger
from accounting.models import Account, AssociationDocument
from commercial.tests import STORAGES, make_commercial


class ContraAssetTests(TestCase):
    def test_an_allowance_with_a_credit_balance_reduces_the_assets_and_the_sheet_balances(self):
        prop = make_commercial('Castlewood Test')
        Account.objects.filter(association=prop, code='1310').update(kind='liability')          # how the allowance was filed before: as a liability
        ledger.post_entry(prop, date(2026, 10, 5), [{'account': '1300', 'debit': D('1000.00')}, {'account': '4500', 'credit': D('1000.00')}], memo='charge')
        ledger.post_entry(prop, date(2026, 10, 6), [{'account': '6920', 'debit': D('200.00')}, {'account': '1310', 'credit': D('200.00')}], memo='bad debt allowance')
        bs = ledger.balance_sheet(prop, date(2026, 10, 31), 'accrual')
        self.assertEqual(bs['assets'], D('800.00'))
        self.assertEqual(bs['assets'], bs['liabilities'] + bs['equity'])
        self.assertTrue(bs['balanced'])

    def test_the_standard_chart_files_the_allowance_as_an_asset(self):
        prop = make_commercial('New Plaza')
        self.assertEqual(Account.objects.get(association=prop, code='1310').kind, 'asset')


def make_pdf(pages):
    writer = PdfWriter()
    for i in range(pages):
        page = writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


class SplitPdfTests(TestCase):
    def test_a_pdf_is_cut_into_parts_under_the_limit_that_cover_every_page_once(self):
        data = make_pdf(12)
        cut = documents.split_pdf(data, limit=len(data) // 3)
        self.assertGreaterEqual(len(cut), 3)
        self.assertTrue(all(len(blob) <= len(data) // 3 for _, _, blob in cut))
        pages = []
        for first, last, blob in cut:
            self.assertEqual(len(PdfReader(io.BytesIO(blob)).pages), last - first + 1)
            pages += list(range(first, last + 1))
        self.assertEqual(pages, list(range(1, 13)))

    def test_one_page_that_cannot_fit_is_reported(self):
        with self.assertRaises(documents.DocumentError) as e:
            documents.split_pdf(make_pdf(2), limit=100)
        self.assertIn('Page 1 alone', str(e.exception))

    @override_settings(STORAGES=STORAGES)
    def test_uploading_a_big_pdf_makes_named_parts_and_a_small_one_stays_whole(self):
        prop = make_commercial('Castlewood Test')
        data = make_pdf(12)
        with mock.patch.object(documents, 'STORE_LIMIT', len(data) // 3):
            first = documents.upload(prop, 'declaration', SimpleUploadedFile('Declaration.pdf', data, content_type='application/pdf'))
        names = list(AssociationDocument.objects.filter(property=prop).order_by('pk').values_list('name', flat=True))
        self.assertGreaterEqual(len(names), 3)
        self.assertEqual(names[0], f'Declaration (part 1 of {len(names)})')
        self.assertEqual(len(first.parts), len(names))
        small = documents.upload(prop, 'bylaws', SimpleUploadedFile('Bylaws.pdf', make_pdf(1), content_type='application/pdf'))
        self.assertEqual(small.name, 'Bylaws')
        self.assertEqual(len(small.parts), 1)

    @override_settings(STORAGES=STORAGES)
    def test_a_big_file_that_is_not_a_pdf_is_refused_with_a_plain_reason(self):
        prop = make_commercial('Castlewood Test')
        with mock.patch.object(documents, 'STORE_LIMIT', 10):
            with self.assertRaises(documents.DocumentError) as e:
                documents.upload(prop, 'bylaws', SimpleUploadedFile('scan.png', b'x' * 50, content_type='image/png'))
        self.assertIn('not a PDF', str(e.exception))

    @override_settings(STORAGES=STORAGES)
    def test_a_storage_failure_is_a_plain_message_not_an_error_page(self):
        prop = make_commercial('Castlewood Test')
        with mock.patch.object(AssociationDocument.objects, 'create', side_effect=RuntimeError('File size too large')):
            with self.assertRaises(documents.DocumentError) as e:
                documents.upload(prop, 'bylaws', SimpleUploadedFile('Bylaws.pdf', make_pdf(1), content_type='application/pdf'))
        self.assertIn('could not be saved right now', str(e.exception))
