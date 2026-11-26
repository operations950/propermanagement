"""The old per-association Pay bills screen and its print page are retired: the addresses still work (bookmarks) but only redirect, and nothing is recorded by visiting or posting to them.
Paying happens in Payables > Print checks."""
from datetime import date
from decimal import Decimal as D

from django.urls import reverse

from . import payables
from .models import Bill, Payment
from .test_check_run_workflow import Base


class RetiredPayScreenTests(Base):
    def setUp(self):
        super().setUp()
        self.b = self.bill(self.acme, 'A-1', '100.00')
        self.pay_url = reverse('accounting:pay', args=[self.alpha.pk])
        self.print_url = reverse('accounting:check_print', args=[self.alpha.pk])

    def test_the_old_pay_bills_address_goes_to_print_checks(self):
        self.assertRedirects(self.client.get(self.pay_url), self.url)

    def test_posting_to_the_old_pay_bills_address_records_nothing(self):
        before = Payment.objects.count()
        response = self.client.post(self.pay_url, {'bill': [self.b.pk], 'bank': self.bank_a.pk, 'method': 'check', 'date': '2026-11-14'})
        self.assertRedirects(response, self.url)
        self.assertEqual(Payment.objects.count(), before)
        self.b.refresh_from_db()
        self.assertEqual(self.b.status, Bill.Status.APPROVED)

    def test_the_old_print_page_goes_to_the_check_pdfs(self):
        pdf = reverse('accounting:payables_checks_pdf')
        self.assertRedirects(self.client.get(self.print_url + '?sample=1'), f'{pdf}?test={self.alpha.pk}', fetch_redirect_response=False)
        made = self.print_bills(self.b)
        self.assertEqual(made.status_code, 200)
        payment = Payment.objects.get(association=self.alpha, method='check')
        self.assertRedirects(self.client.get(self.print_url + f'?ids={payment.pk}'), f'{pdf}?payment={payment.pk}', fetch_redirect_response=False)
        self.assertRedirects(self.client.get(self.print_url + '?ids=999999'), self.url)           # not this association's check: to the list, never a PDF
        self.assertRedirects(self.client.get(self.print_url), self.url)

    def test_the_old_print_page_cannot_mark_checks_printed_any_more(self):
        self.print_bills(self.b)
        payment = Payment.objects.get(association=self.alpha, method='check')
        Payment.objects.filter(pk=payment.pk).update(printed_at=None)
        self.client.post(self.print_url + f'?ids={payment.pk}', {'action': 'printed'})
        payment.refresh_from_db()
        self.assertIsNone(payment.printed_at)

    def test_the_bills_page_points_at_print_checks_not_the_old_screen(self):
        html = self.client.get(reverse('accounting:bills', args=[self.alpha.pk])).content.decode()
        self.assertIn(self.url, html)
        self.assertNotIn(self.pay_url, html)
        self.assertNotIn('Pay bills', html)

    def test_the_checks_register_reprints_through_the_pdf_screen(self):
        self.print_bills(self.b)
        payment = Payment.objects.get(association=self.alpha, method='check')
        html = self.client.get(reverse('accounting:checks', args=[self.alpha.pk])).content.decode()
        pdf = reverse('accounting:payables_checks_pdf')
        self.assertIn(f'{pdf}?payment={payment.pk}', html)
        self.assertIn(f'{pdf}?test={self.alpha.pk}', html)
        self.assertNotIn(self.print_url, html)
        self.assertEqual(self.client.get(f'{pdf}?payment={payment.pk}').status_code, 200)

    def test_the_check_stock_page_tests_through_the_pdf_screen(self):
        html = self.client.get(reverse('accounting:check_setup', args=[self.alpha.pk])).content.decode()
        self.assertIn(f"{reverse('accounting:payables_checks_pdf')}?test={self.alpha.pk}", html)
        self.assertNotIn(self.print_url, html)
