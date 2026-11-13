import io
import tempfile
from datetime import date
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from pypdf import PdfReader, PdfWriter

from core.models import Contact, Unit

from . import charges, ledger, package
from .models import BankAccount, BankStatement, Charge, Receipt
from .tests import make_assoc, spend

MEDIA = tempfile.mkdtemp()
STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage', 'OPTIONS': {'location': MEDIA}},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}


def pdf_of(pages, label):
    """A small real PDF whose first page says `label`, so a test can tell which file landed where."""
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for n in range(pages):
        c.drawString(72, 700, f'{label} page {n + 1}')
        c.showPage()
    c.save()
    return buf.getvalue()


def text_of(page):
    return page.extract_text()


@override_settings(STORAGES=STORAGES)
class MonthlyPackageTests(TestCase):
    def setUp(self):
        self.prop = make_assoc('Package Condos')
        spend(self.prop, date(2026, 10, 12), '6906', '250')
        self.operating = BankAccount.objects.create(association=self.prop, name='Operating 4465', bank_name='Truist', last4='4465', account=ledger.account(self.prop, '1110'))
        self.reserve = BankAccount.objects.create(association=self.prop, name='Reserve 4039', bank_name='Truist', last4='4039', kind='reserve', account=ledger.account(self.prop, '1120'))
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))

    def statement(self, bank, period_end, label, pages):
        s = BankStatement.objects.create(bank_account=bank, period_end=period_end, end_balance=D('0'))
        s.file.save(f'{label}.pdf', ContentFile(pdf_of(pages, label)))
        return s

    def build(self, **kw):
        return PdfReader(io.BytesIO(package.build(self.prop, date(2026, 10, 1), **kw)))

    def test_the_package_runs_in_order_and_ends_with_the_bank_statements_operating_first(self):
        self.statement(self.reserve, date(2026, 10, 30), 'RESERVESTMT', 2)           # created first on purpose: order must come from the account kind, not creation
        self.statement(self.operating, date(2026, 10, 30), 'OPERATINGSTMT', 3)
        reader = self.build()
        texts = [text_of(p) for p in reader.pages]
        self.assertIn('Package Condos', texts[0])
        self.assertIn('Financial package for October 2026', texts[0])
        order = [next(i for i, t in enumerate(texts) if i > 0 and key in t) for key in ('Profit and loss', 'Balance sheet', 'Budget vs actual', 'Owner aging')]
        self.assertEqual(order, sorted(order))
        self.assertGreater(order[0], 0)
        tail = texts[-5:]
        self.assertEqual([t.split(' page')[0] for t in tail], ['OPERATINGSTMT'] * 3 + ['RESERVESTMT'] * 2)

    def test_the_profit_and_loss_has_this_month_and_year_to_date_only_and_the_budget_sheet_has_no_percent_column(self):
        reader = self.build()
        texts = [text_of(p) for p in reader.pages]
        pl = next(t for i, t in enumerate(texts) if i > 0 and 'Profit and loss' in t and 'Year to date' in t)
        self.assertIn('Oct 2026', pl)
        self.assertNotIn('Sep 2026', pl)
        bva = next(t for i, t in enumerate(texts) if i > 0 and 'Budget vs actual' in t and 'Annual' in t)
        self.assertNotIn('% of budget', bva)
        for head in ('Over /', 'Annual', 'Remaining'):
            self.assertIn(head, bva)

    def test_no_reconciliation_pages_and_the_cover_lists_what_is_in(self):
        self.statement(self.operating, date(2026, 10, 30), 'OPERATINGSTMT', 1)
        cover = text_of(self.build().pages[0])
        self.assertNotIn('reconciliation', cover.lower())
        self.assertIn('Bank statement: Operating 4465', cover)
        self.assertIn('Accrual book', cover)

    def test_a_bank_account_without_a_statement_file_is_named_on_the_cover(self):
        self.statement(self.operating, date(2026, 10, 30), 'OPERATINGSTMT', 1)
        BankStatement.objects.create(bank_account=self.reserve, period_end=date(2026, 10, 30), end_balance=D('0'))      # the numbers, but no file
        cover = text_of(self.build().pages[0])
        self.assertIn('Bank statement: Reserve 4039', cover)
        self.assertIn('Not included: statement file not on file', cover)

    def test_a_statement_from_another_month_is_not_included(self):
        self.statement(self.operating, date(2026, 9, 30), 'SEPTSTMT', 1)
        texts = [text_of(p) for p in self.build().pages]
        self.assertFalse(any('SEPTSTMT' in t for t in texts))
        self.assertIn('statement file not on file', texts[0])

    def test_the_cash_book_is_labelled(self):
        self.assertIn('Cash book', text_of(self.build(basis='cash').pages[0]))

    def test_the_reports_page_opens_it_as_a_pdf(self):
        self.statement(self.operating, date(2026, 10, 30), 'OPERATINGSTMT', 1)
        response = self.client.get(reverse('accounting:report_package', args=[self.prop.pk]) + '?month=2026-10&basis=accrual')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertIn('Financial package - 2026-10.pdf', response['Content-Disposition'])
        self.assertTrue(response.content.startswith(b'%PDF'))
        html = self.client.get(reverse('accounting:reports', args=[self.prop.pk])).content.decode()
        self.assertIn('Monthly financial package', html)

    def test_only_an_admin_can_open_it(self):
        self.client.logout()
        self.client.force_login(get_user_model().objects.create_user('staffer', password='pw12345678'))
        response = self.client.get(reverse('accounting:report_package', args=[self.prop.pk]) + '?month=2026-10')
        self.assertNotEqual(response.status_code, 200)


@override_settings(STORAGES=STORAGES)
class AgingAsOfTests(TestCase):
    """Owner aging as of a past date counts what was owed then: a payment received afterwards does not clear it."""

    def test_a_later_payment_does_not_reduce_the_earlier_balance(self):
        prop = make_assoc('Aging Condos')
        unit = Unit.objects.filter(property=prop).first()
        owner = Contact.objects.create(name='Pat Owner', contact_type='owner')
        charge = Charge.objects.create(association=prop, unit=unit, owner=owner, description='Assessment', due_date=date(2026, 9, 1), amount=D('500'))
        receipt = Receipt.objects.create(association=prop, owner=owner, unit=unit, date=date(2026, 11, 5), amount=D('500'))
        from .models import Application
        Application.objects.create(receipt=receipt, charge=charge, amount=D('500'), date=date(2026, 11, 5))
        self.assertEqual(charges.aging(prop, date(2026, 10, 31))['total'], D('500'))
        self.assertEqual(charges.aging(prop, date(2026, 11, 30))['total'], D('0'))
