"""Every report screen downloads as a PDF or an Excel file, with the same figures as the screen, and has no Print button."""
import io
from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from . import report_export
from .tests import cash_in, cash_out, make_assoc

REPORTS = ('report_bs', 'report_pl', 'report_bva', 'report_tb', 'report_is', 'report_aging', 'report_payables', 'report_checks', 'report_gl')


@override_settings(STORAGES={'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                             'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class ReportExportTests(TestCase):
    def setUp(self):
        self.prop = make_assoc()
        cash_in(self.prop, date(2026, 10, 5), '2000.82')
        cash_out(self.prop, date(2026, 10, 9), '300.10', '6820')
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))

    def url(self, name, **query):
        q = '&'.join(f'{k}={v}' for k, v in {'end': '2026-10-31', **query}.items())
        return reverse(f'accounting:{name}', args=[self.prop.pk]) + '?' + q

    def test_every_report_downloads_as_pdf_and_excel(self):
        for name in REPORTS:
            pdf = self.client.get(self.url(name, export='pdf'))
            self.assertEqual(pdf.status_code, 200, name)
            self.assertEqual(pdf['Content-Type'], 'application/pdf', name)
            self.assertTrue(pdf.content.startswith(b'%PDF'), name)
            self.assertIn('attachment', pdf['Content-Disposition'])
            xl = self.client.get(self.url(name, export='xlsx'))
            self.assertEqual(xl.status_code, 200, name)
            self.assertIn('spreadsheetml', xl['Content-Type'], name)

    def test_the_excel_file_has_the_screen_figures_as_numbers(self):
        import openpyxl
        xl = self.client.get(self.url('report_pl', export='xlsx'))
        ws = openpyxl.load_workbook(io.BytesIO(xl.content)).active
        cells = [c for row in ws.iter_rows(values_only=True) for c in row if c is not None]
        self.assertEqual(ws['A1'].value, 'Profit and loss')
        self.assertIn(2000.82, cells)                                 # the income, as a number Excel can add
        self.assertTrue(any(isinstance(c, str) and 'Net income' in c for c in cells))
        self.assertIn('Test Condos', ws['A2'].value)

    def test_the_general_ledger_download_carries_its_lines(self):
        import openpyxl
        from .models import Account
        acct = Account.objects.get(association=self.prop, code='4500')
        xl = self.client.get(self.url('report_gl', export='xlsx', start='2026-01-01', account=acct.pk))
        ws = openpyxl.load_workbook(io.BytesIO(xl.content)).active
        rows = list(ws.iter_rows(values_only=True))
        self.assertTrue(any(r and r[0] == 'Beginning balance' for r in rows))
        self.assertTrue(any(2000.82 in r for r in rows))

    def test_a_report_screen_offers_pdf_and_excel_and_no_print(self):
        for name in REPORTS:
            page = self.client.get(self.url(name)).content.decode()
            self.assertIn('export=pdf', page, name)
            self.assertIn('export=xlsx', page, name)
            self.assertNotIn('window.print()', page, name)

    def test_the_bank_reconciliation_report_has_no_print_button_either(self):
        from .models import BankAccount
        bank = BankAccount.objects.filter(association=self.prop).first()
        if bank is None:
            self.skipTest('no bank account in this fixture')
        page = self.client.get(reverse('accounting:report_rec', args=[self.prop.pk, bank.pk, '2026-10'])).content.decode()
        self.assertNotIn('window.print()', page)
        self.assertEqual(self.client.get(reverse('accounting:report_rec', args=[self.prop.pk, bank.pk, '2026-10']) + '?export=pdf')['Content-Type'], 'application/pdf')

    def test_the_parser_reads_a_statement_and_leaves_the_controls_out(self):
        html = ('<div class="rp-title"><h2>Profit and loss</h2><span>Lakeside &middot; Oct</span></div><form class="ac-noprint"><button>Show</button></form>'
                '<table><thead><tr><th></th><th>YTD</th></tr></thead><tr class="ln"><td><span class="code">4500</span>Fees</td><td class="num">(1,234.50)</td></tr></table>')
        page = report_export.parse(html)
        self.assertEqual(page['title'], 'Profit and loss')
        rows = page['blocks'][0][1]
        self.assertEqual(rows[1]['cells'][0]['text'], '4500 Fees')
        self.assertEqual(report_export._number(rows[1]['cells'][1]['text']), -1234.5)
        self.assertNotIn('Show', str(page))
