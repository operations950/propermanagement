"""Truist Web Vault: the payment report and unit lookup are read, tied to our units, recorded as Web Vault dates them, and tied to the lump deposits on the bank statement."""
import csv
import io
import os
from datetime import date
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import Unit

from . import bankrec, charges, ledger, webvault
from .models import BankAccount, BankLine, BankStatement, Charge, Reconciliation, Receipt, ReconLedgerItem, UnitProfile
from .test_bank_accounts import STORAGES, pdf_printing
from .tests import make_assoc, owner_of

DOWNLOADS = r'C:\Users\opera\Downloads'


def report_pdf(*rows, total=None):
    """A small PDF laid out the way the Web Vault report prints: a header, one line per row, a footer."""
    lines = ['Truist Association Services Web Vault', 'Proper Realty Co', 'St Andrews Glen Condominium-1100024815100', 'Account Number:0001100024815100', 'Posting Date: 10/01/2026 to 10/31/2026',
             'Date Posted Serial Acct. No. Bill Pay Number Amount Check Number ACH/eCheck Owner(s) Item Type']
    lines += list(rows)
    lines += [f'Number of Transactions:{len(rows)}', f'Amount of Transactions:{total or "0.00"}']
    return pdf_printing(*lines)


def lookup_csv(*units):
    """The unit lookup file: for each unit its Web Vault unit number, serial, owner and bill pay numbers (…1 is the regular assessment, …2 and …3 are special ones)."""
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(['textBox%d' % i for i in range(35)])
    for unit, serial, owner, bill in units:
        for n in (1, 2):
            row = ['6455', '0645500002'] + ['x'] * 23 + [unit, serial, owner, f'{bill}{n}', '$1,750.00 - 1/1/2026', 'N/A', 'N', 'N', '', '']
            w.writerow(row)
    return out.getvalue().encode()


class ReadingTheReportTests(TestCase):
    def test_a_coupon_is_printed_twice_and_counted_once(self):
        payments, meta = webvault.read_report(report_pdf('10/07/2026 SAG-3-313 16008463071 1750.00 000000000216 MultiCoupon', '10/07/2026 SAG-3-313 16008463071 1750.00 000000000216 Check/MultiCoupon',
                                                          '10/05/2026 SAG-1-114 16008462921 1750.00 1 Thomas Reul ACH', total='3500.00'))
        self.assertEqual([(p['serial'], p['type'], p['amount']) for p in payments], [('SAG-3-313', 'MultiCoupon', D('1750.00')), ('SAG-1-114', 'ACH', D('1750.00'))])
        self.assertEqual((meta['start'], meta['end'], meta['total'], meta['count']), (date(2026, 10, 1), date(2026, 10, 31), D('3500.00'), 3))

    def test_two_real_coupon_payments_the_same_day_are_two(self):
        rows = ['10/30/2026 SAG12123 15014573531 615.00 000000003067 Coupon', '10/30/2026 SAG12123 15014573531 615.00 000000003067 Check/Coupon',
                '10/30/2026 SAG12123 15014573531 615.00 000000003068 Coupon', '10/30/2026 SAG12123 15014573531 615.00 000000003068 Check/Coupon']
        payments, _ = webvault.read_report(report_pdf(*rows))
        self.assertEqual(len(payments), 2)

    def test_a_serial_that_wrapped_onto_two_lines_is_put_back_together(self):
        payments, _ = webvault.read_report(report_pdf('10/02/2026 SAG-1-', '124 16008462962 1750.00 3 Elec Bill Pay'))
        self.assertEqual((payments[0]['serial'], payments[0]['bill'], payments[0]['type']), ('SAG-1-124', '16008462962', 'Elec Bill Pay'))

    def test_every_kind_of_payment_is_known(self):
        rows = ['10/05/2026 109 16007863961 7268.47 3 Elec Bill Pay', '10/06/2026 119 16007864071 7268.47 4 stephanie cooper eCheck', '10/13/2026 107 16007864061 7268.47 2 Credit Card',
                '10/19/2026 106 16007864101 7836.94 4 Sandeep Mayer eCheck']
        payments, _ = webvault.read_report(report_pdf(*rows))
        self.assertEqual([p['type'] for p in payments], ['Elec Bill Pay', 'eCheck', 'Credit Card', 'eCheck'])
        self.assertEqual(payments[3]['amount'], D('7836.94'))

    def test_a_file_that_is_not_the_report_is_refused_plainly(self):
        with self.assertRaises(webvault.WebVaultError):
            webvault.read_report(pdf_printing('Nothing to see here'))
        with self.assertRaises(webvault.WebVaultError):
            webvault.read_report(b'not a pdf at all')

    def test_the_unit_lookup_is_read_one_row_per_bill_pay_number(self):
        rows = webvault.read_unit_lookup(lookup_csv(('122', 'SAG-1-122', 'GRAHAM HOLDER', '1600846296')))
        self.assertEqual([r['bill'] for r in rows], ['16008462961', '16008462962'])
        self.assertEqual((rows[0]['unit'], rows[0]['owner']), ('122', 'GRAHAM HOLDER'))
        with self.assertRaises(webvault.WebVaultError):
            webvault.read_unit_lookup(b'just,some,text\n1,2,3\n')

    def test_the_real_october_reports_read_to_the_numbers_on_their_last_page(self):
        for name, count, total in (('MyStatement.pdf', 23, D('14145.00')), ('MyStatement (1).pdf', 19, D('40327.00')), ('MyStatement (2).pdf', 4, D('29642.35'))):
            path = os.path.join(DOWNLOADS, name)
            if not os.path.exists(path):
                self.skipTest('the sample reports are not on this computer')
            payments, meta = webvault.read_report(open(path, 'rb').read())
            self.assertEqual((len(payments), sum(p['amount'] for p in payments), meta['count'] - len(payments) >= 0), (count, total, True), name)
            self.assertEqual(meta['total'], total, name)


@override_settings(STORAGES=STORAGES)
class WebVaultBase(TestCase):
    """Three units with an October charge each, and a statement with the lump deposits Web Vault makes."""
    def setUp(self):
        self.prop = make_assoc('Webvault Glen')
        self.bank = BankAccount.objects.create(association=self.prop, name='Operating', bank_name='Truist', last4='5100', account=ledger.account(self.prop, '1110'))
        self.units = {}
        for label, name in (('7601-122', 'Graham Holder'), ('7601-124', 'Carol Silver'), ('7621-211', 'Laverne Rodriguez')):
            unit = Unit.objects.create(property=self.prop, label=label)
            owner = owner_of(self.prop, unit, name)
            charges.post_charge(self.prop, unit, owner, Charge.Kind.ASSESSMENT, 'Regular assessment Oct 2026', date(2026, 10, 1), D('1750'))
            self.units[label] = (unit, owner)
        self.stmt = BankStatement.objects.create(bank_account=self.bank, period_end=date(2026, 10, 30), end_balance=D('0'))
        self.two = BankLine.objects.create(statement=self.stmt, date=date(2026, 10, 2), description='CONSOL ELEC BILL PAY DEPOSIT 2', amount=D('3500.00'), kind='deposit')
        self.ach = BankLine.objects.create(statement=self.stmt, date=date(2026, 10, 5), description='CONSOL ELEC DEPOSIT 1', amount=D('1750.00'), kind='deposit')
        self.payments = [
            {'date': date(2026, 10, 2), 'serial': 'SAG-1-122', 'bill': '16008462962', 'amount': D('1750.00'), 'type': 'Elec Bill Pay', 'text': ''},
            {'date': date(2026, 10, 2), 'serial': 'SAG-1-124', 'bill': '16008462982', 'amount': D('1750.00'), 'type': 'Elec Bill Pay', 'text': ''},
            {'date': date(2026, 10, 5), 'serial': 'SAG-2-211', 'bill': '16008462971', 'amount': D('1750.00'), 'type': 'ACH', 'text': ''},
        ]
        self.lookup = [{'bill': '16008462961', 'unit': '122', 'serial': 'SAG-1-122', 'owner': 'GRAHAM HOLDER'}, {'bill': '16008462981', 'unit': '124', 'serial': 'SAG-1-124', 'owner': 'CAROL SILVER'},
                       {'bill': '16008462971', 'unit': '211', 'serial': 'SAG-2-211', 'owner': 'LAVERNE RODRIGUEZ'}]
        self.payments[0]['bill'], self.payments[1]['bill'] = '16008462961', '16008462981'      # the report's bill pay numbers are the regular ones

    def run_confirm(self, **kw):
        return webvault.confirm(self.prop, self.bank, self.stmt, self.payments, self.lookup, user=None, **kw)


class MappingUnitsTests(WebVaultBase):
    def test_a_unit_is_found_by_its_label_or_the_end_of_it_and_remembered(self):
        mapped, undecided = webvault.map_unit_rows(self.prop, self.lookup)
        self.assertEqual({pre: u.label for pre, u in mapped.items()}, {'1600846296': '7601-122', '1600846298': '7601-124', '1600846297': '7621-211'})
        self.assertEqual(undecided, [])
        self.assertEqual(webvault.save_units(mapped, self.lookup), 3)
        self.assertEqual(UnitProfile.objects.get(unit=self.units['7601-122'][0]).webvault_bill_pay, '16008462961')
        self.assertEqual(webvault.save_units(mapped, self.lookup), 0)                      # nothing changes the second time

    def test_a_unit_with_no_matching_label_is_found_by_its_owners_name(self):
        rows = [{'bill': '15014573381', 'unit': 'SAG12076', 'serial': 'SAG12076', 'owner': 'Carol, Silver'}]
        mapped, undecided = webvault.map_unit_rows(self.prop, rows)
        self.assertEqual(mapped['1501457338'], self.units['7601-124'][0])
        self.assertEqual(undecided, [])

    def test_two_units_with_the_same_owner_are_left_for_a_person_not_guessed(self):
        unit = Unit.objects.create(property=self.prop, label='7621-212')
        owner = self.units['7601-124'][1]
        from core.models import Contact
        from .models import Ownership
        Ownership.objects.create(unit=unit, person=owner, start_date=date(2020, 1, 1), is_billed=True)
        rows = [{'bill': '15014573531', 'unit': 'SAG12123', 'serial': 'SAG12123', 'owner': 'Carol Silver'}, {'bill': '15014573811', 'unit': 'SAG12126', 'serial': 'SAG12126', 'owner': 'Carol Silver'}]
        mapped, undecided = webvault.map_unit_rows(self.prop, rows)
        self.assertEqual(mapped, {})
        self.assertEqual(len(undecided), 2)
        self.assertEqual({u.label for u in undecided[0]['candidates']}, {'7601-124', '7621-212'})

    def test_a_unit_nobody_can_be_found_for_is_undecided_with_no_candidates(self):
        mapped, undecided = webvault.map_unit_rows(self.prop, [{'bill': '19999999991', 'unit': 'ZZ9', 'serial': 'ZZ9', 'owner': 'Nobody Known'}])
        self.assertEqual((mapped, len(undecided), undecided[0]['candidates']), ({}, 1, []))


class PreviewTests(WebVaultBase):
    def test_nothing_is_written_and_each_payment_has_a_status(self):
        before = Receipt.objects.count()
        plan = webvault.build(self.prop, self.bank, self.stmt, self.payments, self.lookup)
        self.assertEqual(Receipt.objects.count(), before)
        self.assertEqual([x['status'] for x in plan['items']], ['record', 'record', 'record'])
        self.assertEqual((plan['to_record'], plan['recorded'], plan['problems']), (3, 0, 0))
        self.assertEqual([len(g['items']) for g in plan['deposits']], [2, 1])             # two bill pay payments for the $3,500, the ACH for the $1,750
        self.assertTrue(all(g['ok'] for g in plan['deposits']))

    def test_a_payment_already_in_the_books_is_listed_as_recorded_and_its_date_is_left_alone(self):
        unit, owner = self.units['7601-122']
        existing = charges.receive_payment(self.prop, unit, owner, date(2026, 10, 1), D('1750'), 'ach', reference='Yardi R-1', bank_account=self.bank)
        plan = webvault.build(self.prop, self.bank, self.stmt, self.payments, self.lookup)
        x = plan['items'][0]
        self.assertEqual((x['status'], x['receipt'].pk), ('recorded', existing.pk))
        self.assertIn('Web Vault says Oct 2', x['note'])
        self.assertIn('left as it is', x['note'])
        self.assertEqual(plan['to_record'], 2)
        self.assertEqual(len(plan['deposits'][0]['items']), 2)                           # it still counts toward the $3,500 deposit

    def test_a_payment_with_no_unit_is_a_problem_and_says_what_to_do(self):
        plan = webvault.build(self.prop, self.bank, self.stmt, self.payments, lookup_rows=None)
        self.assertEqual([x['status'] for x in plan['items']], ['problem'] * 3)
        self.assertIn('unit lookup', plan['items'][0]['note'])

    def test_a_unit_without_an_owner_is_a_problem(self):
        unit, owner = self.units['7601-122']
        unit.ownerships.all().delete()
        Charge.objects.filter(unit=unit).delete()
        plan = webvault.build(self.prop, self.bank, self.stmt, self.payments, self.lookup)
        self.assertEqual(plan['items'][0]['status'], 'problem')
        self.assertIn('no owner', plan['items'][0]['note'])


class RecordingTests(WebVaultBase):
    def test_payments_are_recorded_with_the_web_vault_date_method_and_reference(self):
        done = self.run_confirm()
        self.assertEqual((done['recorded'], done['tied']), (3, 2))
        r = Receipt.objects.get(unit=self.units['7601-122'][0])
        self.assertEqual((r.date, r.amount, r.method, r.reference), (date(2026, 10, 2), D('1750.00'), 'online', 'Webvault SAG-1-122 Elec Bill Pay'))
        self.assertEqual(Receipt.objects.get(unit=self.units['7621-211'][0]).method, 'ach')
        self.assertEqual(r.bank_account, self.bank)
        self.assertEqual(r.applications.get().charge.description, 'Regular assessment Oct 2026')

    def test_two_bill_pay_payments_on_the_same_day_tie_to_the_one_lump_deposit(self):
        self.run_confirm()
        self.two.refresh_from_db()
        self.assertIsNotNone(self.two.match_id)
        self.assertEqual((self.two.match.how, self.two.match.note), ('Web Vault deposit', 'Web Vault 10/02'))
        self.assertEqual(self.two.match.ledger_items.count(), 2)
        self.ach.refresh_from_db()
        self.assertEqual(self.ach.match.ledger_items.count(), 1)
        s = bankrec.summary(self.bank, date(2026, 10, 1))
        self.assertEqual(s['unmatched_bank'], [])

    def test_an_existing_payment_is_tied_to_its_deposit_and_never_redated(self):
        unit, owner = self.units['7601-122']
        existing = charges.receive_payment(self.prop, unit, owner, date(2026, 10, 1), D('1750'), 'ach', reference='Yardi R-1', bank_account=self.bank)
        done = self.run_confirm()
        self.assertEqual(done['recorded'], 2)
        existing.refresh_from_db()
        self.assertEqual(existing.date, date(2026, 10, 1))
        self.assertEqual(Receipt.objects.filter(unit=unit).count(), 1)
        self.two.refresh_from_db()
        self.assertTrue(ReconLedgerItem.objects.filter(match=self.two.match, line__entry=existing.entry).exists())

    def test_confirming_again_records_and_ties_nothing_twice(self):
        self.run_confirm()
        receipts, matches = Receipt.objects.count(), self.stmt.lines.filter(match__isnull=False).count()
        again = self.run_confirm()
        self.assertEqual((again['recorded'], again['tied']), (0, 0))
        self.assertEqual((Receipt.objects.count(), self.stmt.lines.filter(match__isnull=False).count()), (receipts, matches))

    def test_a_closed_reconciliation_is_refused_and_nothing_is_written(self):
        Reconciliation.objects.create(bank_account=self.bank, month=date(2026, 10, 1), status=Reconciliation.Status.CLOSED)
        with self.assertRaises(webvault.WebVaultError):
            self.run_confirm()
        self.assertEqual(Receipt.objects.count(), 0)

    def test_it_is_all_or_nothing(self):
        from unittest import mock
        real = charges.receive_payment
        calls = []

        def flaky(*a, **k):
            calls.append(1)
            if len(calls) == 3:
                raise charges.ChargeError('boom')
            return real(*a, **k)
        with mock.patch.object(charges, 'receive_payment', side_effect=flaky), self.assertRaises(charges.ChargeError):
            self.run_confirm()
        self.assertEqual(Receipt.objects.count(), 0)
        self.assertEqual(self.stmt.lines.filter(match__isnull=False).count(), 0)

    def test_the_units_bill_pay_numbers_are_saved_for_next_month(self):
        self.run_confirm()
        self.assertEqual(UnitProfile.objects.get(unit=self.units['7601-124'][0]).webvault_bill_pay, '16008462981')
        plan = webvault.build(self.prop, self.bank, self.stmt, self.payments, lookup_rows=None)        # no lookup file this time
        self.assertEqual([x['status'] for x in plan['items']], ['recorded'] * 3)

    def test_a_person_can_answer_a_unit_the_file_could_not_place(self):
        rows = [{'bill': '16008462961', 'unit': 'SAG12122', 'serial': 'SAG12122', 'owner': 'Somebody Else'}]
        plan = webvault.build(self.prop, self.bank, self.stmt, self.payments[:1], rows)
        self.assertEqual(len(plan['undecided']), 1)
        pre = plan['undecided'][0]['prefix']
        chosen = webvault.build(self.prop, self.bank, self.stmt, self.payments[:1], rows, {pre: self.units['7601-122'][0].pk})
        self.assertEqual((chosen['items'][0]['status'], chosen['undecided']), ('record', []))


@override_settings(STORAGES=STORAGES)
class TheScreenTests(WebVaultBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.url = reverse('accounting:deposits', args=[self.prop.pk, self.bank.pk, '2026-10'])
        self.report = report_pdf('10/02/2026 SAG-1-122 16008462961 1750.00 3 Elec Bill Pay', '10/02/2026 SAG-1-124 16008462981 1750.00 3 Elec Bill Pay', '10/05/2026 SAG-2-211 16008462971 1750.00 1 Laverne ACH',
                                 total='5250.00')
        self.csv = lookup_csv(('122', 'SAG-1-122', 'GRAHAM HOLDER', '1600846296'), ('124', 'SAG-1-124', 'CAROL SILVER', '1600846298'), ('211', 'SAG-2-211', 'LAVERNE RODRIGUEZ', '1600846297'))

    def preview(self):
        return self.client.post(self.url, {'action': 'wv_preview', 'report': SimpleUploadedFile('r.pdf', self.report, content_type='application/pdf'),
                                           'lookup': SimpleUploadedFile('u.csv', self.csv, content_type='text/csv')})

    def test_the_deposits_screen_offers_the_import(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn('Import a Web Vault report', html)
        self.assertIn('name="report"', html)

    def test_the_preview_shows_the_payments_and_the_deposits_and_writes_nothing(self):
        response = self.preview()
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('Will be recorded', html)
        self.assertIn('CONSOL ELEC BILL PAY DEPOSIT 2', html)
        self.assertIn('2 payments on Oct 2', html)
        self.assertIn('wv_confirm', html)
        self.assertEqual(Receipt.objects.count(), 0)

    def test_the_button_records_and_ties_and_a_second_press_does_nothing_more(self):
        token = self.preview().context['wv']['token']
        first = self.client.post(self.url, {'action': 'wv_confirm', 'wv_token': token}, follow=True)
        self.assertIn('Recorded 3 payments and tied 2 bank deposits', ' '.join(str(m) for m in first.context['messages']))
        second = self.client.post(self.url, {'action': 'wv_confirm', 'wv_token': token}, follow=True)
        self.assertIn('Recorded 0 payments and tied 0 bank deposits', ' '.join(str(m) for m in second.context['messages']))
        self.assertEqual(Receipt.objects.count(), 3)

    def test_a_changed_preview_is_refused(self):
        token = self.preview().context['wv']['token']
        response = self.client.post(self.url, {'action': 'wv_confirm', 'wv_token': token + 'x'}, follow=True)
        self.assertIn('expired', ' '.join(str(m) for m in response.context['messages']))
        self.assertEqual(Receipt.objects.count(), 0)

    def test_a_closed_month_says_so_on_the_screen(self):
        Reconciliation.objects.create(bank_account=self.bank, month=date(2026, 10, 1), status=Reconciliation.Status.CLOSED)
        token = self.preview().context['wv']['token']
        response = self.client.post(self.url, {'action': 'wv_confirm', 'wv_token': token}, follow=True)
        self.assertIn('is closed', ' '.join(str(m) for m in response.context['messages']))
        self.assertEqual(Receipt.objects.count(), 0)

    def test_a_file_that_is_not_the_report_is_a_message_not_a_crash(self):
        response = self.client.post(self.url, {'action': 'wv_preview', 'report': SimpleUploadedFile('r.pdf', b'nope', content_type='application/pdf')}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('could not be read', ' '.join(str(m) for m in response.context['messages']))


class SavedLookupBase(WebVaultBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.url = reverse('accounting:deposits', args=[self.prop.pk, self.bank.pk, '2026-10'])
        self.report = report_pdf('10/02/2026 SAG-1-122 16008462961 1750.00 3 Elec Bill Pay', '10/02/2026 SAG-1-124 16008462981 1750.00 3 Elec Bill Pay', '10/05/2026 SAG-2-211 16008462971 1750.00 1 Laverne ACH',
                                 total='5250.00')
        self.csv = lookup_csv(('122', 'SAG-1-122', 'GRAHAM HOLDER', '1600846296'), ('124', 'SAG-1-124', 'CAROL SILVER', '1600846298'), ('211', 'SAG-2-211', 'LAVERNE RODRIGUEZ', '1600846297'))

    def pdf(self):
        return SimpleUploadedFile('r.pdf', self.report, content_type='application/pdf')

    def stored(self, label):
        return UnitProfile.objects.get(unit=self.units[label][0]).webvault_bill_pay


class TheLookupIsKeptTests(SavedLookupBase):
    def test_the_status_line_counts_saved_units_of_active_units(self):
        self.assertEqual(webvault.lookup_status(self.prop), (0, 3 + 4))                  # make_assoc makes four more units
        self.assertIn('Unit lookup saved for 0 of 7 units', self.client.get(self.url).content.decode())
        webvault.save_lookup(self.prop, self.lookup)
        self.assertEqual(webvault.lookup_status(self.prop), (3, 7))
        Unit.objects.filter(property=self.prop, label='100').update(is_active=False)
        self.assertEqual(webvault.lookup_status(self.prop), (3, 6))
        self.assertIn('Unit lookup saved for 3 of 6 units', self.client.get(self.url).content.decode())

    def test_the_csv_field_is_offered_only_until_something_is_saved(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn('name="lookup"', html)                                             # nothing saved yet: the first import can bring the file
        webvault.save_lookup(self.prop, self.lookup)
        html = self.client.get(self.url).content.decode()
        self.assertNotIn('name="lookup"', html)
        self.assertIn('Update the unit lookup', html)
        self.assertIn('name="csv"', html)                                                # the standalone upload is still there

    def test_the_import_works_with_only_the_pdf_once_the_numbers_are_saved(self):
        webvault.save_lookup(self.prop, self.lookup)
        response = self.client.post(self.url, {'action': 'wv_preview', 'report': self.pdf()})
        plan = response.context['wv']['plan']
        self.assertEqual((plan['problems'], plan['to_record']), (0, 3))
        self.assertNotIn('Add the unit lookup file', response.content.decode())          # nothing to ask for
        done = self.client.post(self.url, {'action': 'wv_confirm', 'wv_token': response.context['wv']['token']}, follow=True)
        self.assertIn('Recorded 3 payments and tied 2 bank deposits', ' '.join(str(m) for m in done.context['messages']))

    def test_the_preview_asks_for_the_lookup_only_when_a_payment_cannot_be_placed(self):
        webvault.save_lookup(self.prop, self.lookup[:2])                                 # the third unit was never saved
        response = self.client.post(self.url, {'action': 'wv_preview', 'report': self.pdf()})
        self.assertEqual(response.context['wv']['plan']['problems'], 1)
        html = response.content.decode()
        self.assertIn('Add the unit lookup file', html)
        self.assertIn('could not be placed', html)

    def test_the_first_import_saves_the_lookup_it_was_given(self):
        response = self.client.post(self.url, {'action': 'wv_preview', 'report': self.pdf(), 'lookup': SimpleUploadedFile('u.csv', self.csv, content_type='text/csv')})
        self.client.post(self.url, {'action': 'wv_confirm', 'wv_token': response.context['wv']['token']})
        self.assertEqual(webvault.lookup_status(self.prop)[0], 3)


class StandaloneSaveTests(SavedLookupBase):
    def test_a_lookup_file_alone_saves_every_certain_unit_at_once(self):
        response = self.client.post(self.url, {'action': 'wv_lookup', 'csv': SimpleUploadedFile('u.csv', self.csv, content_type='text/csv')}, follow=True)
        self.assertIn('Saved the Web Vault bill pay number for 3 units', ' '.join(str(m) for m in response.context['messages']))
        self.assertEqual(self.stored('7601-122'), '16008462961')
        self.assertEqual(webvault.lookup_status(self.prop)[0], 3)
        self.assertEqual(Receipt.objects.count(), 0)                                      # no report, nothing recorded

    def test_the_rows_it_cannot_place_come_back_with_dropdowns_and_the_answers_are_saved_too(self):
        rows = lookup_csv(('122', 'SAG-1-122', 'GRAHAM HOLDER', '1600846296'), ('ZZ9', 'SAG-9-9', 'Nobody Known', '1999999999'))
        response = self.client.post(self.url, {'action': 'wv_lookup', 'csv': SimpleUploadedFile('u.csv', rows, content_type='text/csv')})
        self.assertEqual(self.stored('7601-122'), '16008462961')                         # the certain one is already saved
        html = response.content.decode()
        self.assertIn('Nobody Known', html)
        self.assertIn('name="map_1999999999"', html)
        token = response.context['wvl']['token']
        self.client.post(self.url, {'action': 'wv_lookup_apply', 'wv_token': token, 'map_1999999999': self.units['7601-124'][0].pk})
        self.assertEqual(self.stored('7601-124'), '19999999991')

    def test_a_file_that_is_not_the_lookup_is_a_message(self):
        response = self.client.post(self.url, {'action': 'wv_lookup', 'csv': SimpleUploadedFile('u.csv', b'a,b\n1,2\n', content_type='text/csv')}, follow=True)
        self.assertIn('No units were found', ' '.join(str(m) for m in response.context['messages']))


class NoSilentOverwriteTests(SavedLookupBase):
    def test_a_different_saved_number_is_never_replaced_unless_a_person_says_so(self):
        UnitProfile.objects.create(unit=self.units['7601-122'][0], webvault_bill_pay='19999999991')
        done = webvault.save_lookup(self.prop, self.lookup)
        self.assertEqual((done['saved'], [x['unit'].label for x in done['changed']]), (2, ['7601-122']))
        self.assertEqual(self.stored('7601-122'), '19999999991')
        webvault.save_lookup(self.prop, self.lookup, accept=['1600846296'])
        self.assertEqual(self.stored('7601-122'), '16008462961')

    def test_a_number_another_unit_holds_is_a_change_too(self):
        UnitProfile.objects.create(unit=self.units['7601-124'][0], webvault_bill_pay='16008462961')    # 124 holds 122's number
        plan = webvault.plan_lookup(self.prop, self.lookup)
        status = {x['unit'].label: x['status'] for x in plan['rows']}
        self.assertEqual(status['7601-122'], 'changed')
        webvault.save_lookup(self.prop, self.lookup)
        self.assertEqual(self.stored('7601-124'), '16008462961')                          # untouched

    def test_the_screen_lists_the_changed_units_and_saves_only_the_ticked_ones(self):
        UnitProfile.objects.create(unit=self.units['7601-122'][0], webvault_bill_pay='19999999991')
        response = self.client.post(self.url, {'action': 'wv_lookup', 'csv': SimpleUploadedFile('u.csv', self.csv, content_type='text/csv')})
        html = response.content.decode()
        self.assertIn('already have a different Web Vault number', html)
        self.assertIn('name="accept_1600846296"', html)
        self.assertEqual(self.stored('7601-122'), '19999999991')
        token = response.context['wvl']['token']
        self.client.post(self.url, {'action': 'wv_lookup_apply', 'wv_token': token})                       # nothing ticked
        self.assertEqual(self.stored('7601-122'), '19999999991')
        self.client.post(self.url, {'action': 'wv_lookup_apply', 'wv_token': token, 'accept_1600846296': 'on'})
        self.assertEqual(self.stored('7601-122'), '16008462961')

    def test_an_import_never_replaces_a_saved_number_either(self):
        UnitProfile.objects.create(unit=self.units['7601-122'][0], webvault_bill_pay='19999999991')
        webvault.confirm(self.prop, self.bank, self.stmt, self.payments, self.lookup)
        self.assertEqual(self.stored('7601-122'), '19999999991')


class TheCommandTests(SavedLookupBase):
    def path(self, data):
        import tempfile
        f = tempfile.NamedTemporaryFile(suffix='.csv', delete=False)
        f.write(data)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def run_command(self, *extra, data=None):
        from django.core.management import call_command
        out = io.StringIO()
        call_command('load_webvault_lookup', '--association', 'Webvault Glen', '--csv', self.path(data or self.csv), *extra, stdout=out)
        return out.getvalue()

    def test_the_dry_run_prints_what_it_would_save_and_saves_nothing(self):
        text = self.run_command()
        self.assertIn('To save: 3', text)
        self.assertIn('7601-122', text)
        self.assertIn('Nothing was saved', text)
        self.assertEqual(UnitProfile.objects.exclude(webvault_bill_pay='').count(), 0)

    def test_apply_saves_the_certain_units_and_lists_the_rest(self):
        data = lookup_csv(('122', 'SAG-1-122', 'GRAHAM HOLDER', '1600846296'), ('ZZ9', 'SAG-9-9', 'Nobody Known', '1999999999'))
        text = self.run_command('--apply', data=data)
        self.assertIn('Ambiguous or not found: 1', text)
        self.assertIn('Nobody Known', text)
        self.assertIn('Saved 1 unit', text)
        self.assertEqual(self.stored('7601-122'), '16008462961')
        again = self.run_command('--apply', data=data)
        self.assertIn('Saved 0 units; 1 already saved', again)

    def test_a_different_saved_number_is_left_unless_asked(self):
        UnitProfile.objects.create(unit=self.units['7601-122'][0], webvault_bill_pay='19999999991')
        text = self.run_command('--apply')
        self.assertIn('A different number is already saved: 1', text)
        self.assertEqual(self.stored('7601-122'), '19999999991')
        self.run_command('--apply', '--accept-changes')
        self.assertEqual(self.stored('7601-122'), '16008462961')

    def test_an_unknown_association_or_file_is_a_plain_error(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command('load_webvault_lookup', '--association', 'No Such Place', '--csv', self.path(self.csv))
        with self.assertRaises(CommandError):
            call_command('load_webvault_lookup', '--association', 'Webvault Glen', '--csv', self.path(b'a,b\n1,2\n'))

    def test_the_real_glen_file_reads_to_36_units_when_present(self):
        path = os.path.join(DOWNLOADS, 'UnitLookup.csv')
        if not os.path.exists(path):
            self.skipTest('the sample lookup files are not on this computer')
        rows = webvault.read_unit_lookup(open(path, 'rb').read())
        self.assertEqual(len({webvault.prefix(r['bill']) for r in rows}), 36)               # St Andrews Glen: 36 units, three bill pay numbers each
