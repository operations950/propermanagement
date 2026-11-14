import shutil
import tempfile
from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from . import bankrec, ledger
from .models import BankAccount, BankLine, BankStatement, Payment
from .tests import make_assoc

STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}

COMBINED = {
    'period_end': '2026-10-30',
    'accounts': [
        {'account_name': 'ASSOC SVCS INTEREST CHECKING', 'last4': '9511', 'begin_balance': 5173.90, 'end_balance': 64854.99, 'lines': [
            {'date': '2026-10-05', 'description': 'DEPOSIT', 'amount': 7268.47, 'check_number': ''},
            {'date': '2026-10-12', 'description': 'CHECK 1001', 'amount': -375, 'check_number': '1001'}]},
        {'account_name': 'ASSOC SVCS MONEY MKT SAVINGS', 'last4': '9538', 'begin_balance': 60705.29, 'end_balance': 77200.01, 'lines': [
            {'date': '2026-10-30', 'description': 'INTEREST PAID', 'amount': 12.5, 'check_number': ''}]},
    ],
}


def upload(name='s.pdf', data=b'%PDF-1.4 statement'):
    return SimpleUploadedFile(name, data, content_type='application/pdf')


class ReadingEveryAccountTests(TestCase):
    """The reader reports each account on a statement on its own; a combined PDF is never read as one big account."""

    def read_with(self, found):
        message = SimpleNamespace(content=[SimpleNamespace(type='tool_use', input=found)])
        client = mock.Mock()
        client.messages.create.return_value = message
        with mock.patch('anthropic.Anthropic', return_value=client), mock.patch('core.app_settings.sanitized_setting', return_value='key'), override_settings(ANTHROPIC_API_KEY='key'):
            return bankrec.read_pdf_statement(b'%PDF'), client

    def test_a_combined_statement_comes_back_as_two_accounts_with_their_own_balances_and_lines(self):
        parsed, client = self.read_with(COMBINED)
        first, second = parsed['accounts']
        self.assertEqual((first['last4'], first['end_balance'], first['begin_balance'], len(first['lines'])), ('9511', D('64854.99'), D('5173.9'), 2))
        self.assertEqual((second['last4'], second['end_balance'], len(second['lines'])), ('9538', D('77200.01'), 1))
        self.assertEqual(parsed['period_end'], date(2026, 10, 30))
        self.assertNotIn('lines', parsed)                                               # nothing at the top for a single import to swallow whole
        self.assertEqual(second['lines'][0]['kind'], 'interest')
        tool = client.messages.create.call_args.kwargs['tools'][0]
        self.assertIn('accounts', tool['input_schema']['required'])                      # the tool asks for one entry per account, each with its last 4
        self.assertIn('last4', tool['input_schema']['properties']['accounts']['items']['properties'])

    def test_a_one_account_statement_still_carries_its_lines_at_the_top_like_a_csv(self):
        parsed, _ = self.read_with({'period_end': '2026-10-30', 'accounts': [COMBINED['accounts'][0]]})
        self.assertEqual((len(parsed['accounts']), parsed['end_balance'], len(parsed['lines'])), (1, D('64854.99'), 2))

    def test_an_answer_in_the_older_one_account_shape_still_reads(self):
        parsed, _ = self.read_with({'period_end': '2026-10-30', 'end_balance': 100, 'lines': [{'date': '2026-10-02', 'description': 'DEPOSIT', 'amount': 100}]})
        self.assertEqual((len(parsed['accounts']), parsed['end_balance'], parsed['accounts'][0]['last4']), (1, D('100'), ''))

    def test_a_quiet_account_with_only_a_balance_is_kept_but_nothing_at_all_is_refused(self):
        quiet = {'account_name': 'SAVINGS', 'last4': '9538', 'end_balance': 10, 'lines': []}
        parsed, _ = self.read_with({'period_end': '2026-10-30', 'accounts': [COMBINED['accounts'][0], quiet]})
        self.assertEqual(len(parsed['accounts']), 2)
        with self.assertRaises(bankrec.StatementError):
            self.read_with({'period_end': '2026-10-30', 'accounts': [quiet]})

    def test_one_import_refuses_a_combined_result_instead_of_filing_it_as_one_account(self):
        parsed = bankrec.parse_statement_found(COMBINED)
        with self.assertRaises(bankrec.StatementError):
            bankrec.import_statement(None, parsed)


class FilingEachAccountTests(TestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, True)
        self.override = override_settings(STORAGES=STORAGES, MEDIA_ROOT=self.media)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.prop = make_assoc('Lapensee Condos')
        self.checking = BankAccount.objects.create(association=self.prop, name='LAP 9511', bank_name='Truist', last4='9511', account=ledger.account(self.prop, '1110'))
        self.savings = BankAccount.objects.create(association=self.prop, name='Escrow Cash', bank_name='Truist', last4='9538', kind='reserve', account=ledger.account(self.prop, '1120'))
        self.parsed = bankrec.parse_statement_found(COMBINED)

    def test_two_accounts_in_one_file_each_get_their_own_statement_lines_and_balance(self):
        result = bankrec.file_statement(self.checking, self.parsed, file=upload(), read_by='claude')
        self.assertTrue(result['multi'])
        self.assertEqual(result['skipped'], [])
        a = BankStatement.objects.get(bank_account=self.checking)
        b = BankStatement.objects.get(bank_account=self.savings)
        self.assertEqual((a.end_balance, a.begin_balance, a.lines.count()), (D('64854.99'), D('5173.90'), 2))
        self.assertEqual((b.end_balance, b.begin_balance, b.lines.count()), (D('77200.01'), D('60705.29'), 1))
        self.assertEqual(a.end_balance + b.end_balance, D('142055.00'))                  # the two add up to the combined total, not each to it
        self.assertEqual(a.file.name, b.file.name)                                       # the one stored file is behind both

    def test_an_account_no_bank_account_claims_is_reported_and_the_rest_are_kept(self):
        found = {'period_end': '2026-10-30', 'accounts': COMBINED['accounts'] + [{'account_name': 'SOMETHING ELSE', 'last4': '4444', 'end_balance': 1, 'lines': [
            {'date': '2026-10-03', 'description': 'FEE', 'amount': -1}]}]}
        result = bankrec.file_statement(self.checking, bankrec.parse_statement_found(found), file=upload(), read_by='claude')
        self.assertEqual(len(result['filed']), 2)
        self.assertEqual([s['last4'] for s in result['skipped']], ['4444'])
        self.assertEqual(BankStatement.objects.count(), 2)

    def test_when_nothing_matches_nothing_is_filed_and_it_says_what_to_do(self):
        self.checking.last4 = self.savings.last4 = ''
        self.checking.save()
        self.savings.save()
        with self.assertRaises(bankrec.StatementError) as ctx:
            bankrec.file_statement(self.checking, self.parsed, file=upload(), read_by='claude')
        self.assertIn('last 4', str(ctx.exception))
        self.assertEqual(BankStatement.objects.count(), 0)

    def test_two_sections_never_land_on_one_bank_account(self):
        twin = {'period_end': '2026-10-30', 'accounts': [dict(COMBINED['accounts'][0]), dict(COMBINED['accounts'][0], account_name='COPY')]}
        result = bankrec.file_statement(self.checking, bankrec.parse_statement_found(twin), file=upload(), read_by='claude')
        self.assertEqual((len(result['filed']), len(result['skipped'])), (1, 1))

    def test_an_account_does_not_get_filed_by_guessing_when_its_last_4_is_missing(self):
        found = {'period_end': '2026-10-30', 'accounts': [COMBINED['accounts'][0], dict(COMBINED['accounts'][1], last4='')]}
        result = bankrec.file_statement(self.checking, bankrec.parse_statement_found(found), file=upload(), read_by='claude')
        self.assertEqual((len(result['filed']), len(result['skipped'])), (1, 1))

    def test_a_typed_ending_balance_is_refused_for_a_file_with_several_accounts(self):
        with self.assertRaises(bankrec.StatementError):
            bankrec.file_statement(self.checking, self.parsed, file=upload(), read_by='claude', end_balance=D('1'))
        self.assertEqual(BankStatement.objects.count(), 0)

    def test_one_account_uploaded_to_the_wrong_bank_account_goes_to_the_one_with_its_last_4(self):
        savings_only = bankrec.parse_statement_found({'period_end': '2026-10-30', 'accounts': [COMBINED['accounts'][1]]})
        result = bankrec.file_statement(self.checking, savings_only, file=upload(), read_by='claude')
        self.assertTrue(result['moved'])
        self.assertEqual(result['filed'][0][0].pk, self.savings.pk)
        self.assertEqual(BankStatement.objects.filter(bank_account=self.checking).count(), 0)

    def test_one_account_with_no_matching_last_4_stays_where_it_was_uploaded(self):
        odd = bankrec.parse_statement_found({'period_end': '2026-10-30', 'accounts': [dict(COMBINED['accounts'][0], last4='1234')]})
        result = bankrec.file_statement(self.checking, odd, file=upload(), read_by='claude')
        self.assertEqual((result['moved'], result['filed'][0][0].pk), (False, self.checking.pk))

    def test_a_csv_is_filed_exactly_as_before(self):
        csv_result = {'lines': [{'date': date(2026, 10, 2), 'description': 'DEPOSIT', 'amount': D('100'), 'kind': 'deposit', 'check_number': ''}], 'end_balance': D('100'), 'begin_balance': None,
                      'period_end': date(2026, 10, 31)}
        result = bankrec.file_statement(self.checking, csv_result, file=upload('s.csv', b'x'), read_by='csv')
        self.assertEqual((result['multi'], result['moved'], len(result['filed'])), (False, False, 1))
        self.assertEqual(BankLine.objects.filter(statement__bank_account=self.checking).count(), 1)

    def test_filing_again_replaces_the_statement_for_the_same_period(self):
        bankrec.file_statement(self.checking, self.parsed, file=upload(), read_by='claude')
        bankrec.file_statement(self.checking, self.parsed, file=upload(), read_by='claude')
        self.assertEqual(BankStatement.objects.filter(bank_account=self.checking).count(), 1)
        self.assertEqual(BankStatement.objects.filter(bank_account=self.savings).count(), 1)

    def test_a_matched_statement_is_not_replaced_and_nothing_is_half_filed(self):
        bankrec.file_statement(self.checking, self.parsed, file=upload(), read_by='claude')
        line = BankLine.objects.filter(statement__bank_account=self.savings).first()
        line.match = bankrec.ReconMatch.objects.create(bank_account=self.savings, month=date(2026, 10, 1))
        line.save()
        with self.assertRaises(bankrec.StatementError) as ctx:
            bankrec.file_statement(self.checking, self.parsed, file=upload(), read_by='claude')
        self.assertIn('Escrow Cash', str(ctx.exception))                                  # it says which account stopped it
        self.assertEqual(BankStatement.objects.filter(bank_account=self.checking).count(), 1)
        self.assertEqual(BankStatement.objects.get(bank_account=self.checking).lines.count(), 2)

    def test_splitting_a_statement_that_was_read_as_one_account_files_each_account_where_it_belongs(self):
        """La Pensee's case: the combined file was read once and saved on both bank accounts, with the same lines and balance."""
        wrong = {'lines': [{'date': date(2026, 10, 5), 'description': 'DEPOSIT', 'amount': D('1'), 'kind': 'deposit', 'check_number': ''}], 'end_balance': D('142055.00'), 'begin_balance': D('65879.19'),
                 'period_end': date(2026, 10, 30)}
        first = bankrec.import_statement(self.checking, wrong, file=upload(), read_by='claude')
        bankrec.import_statement(self.savings, wrong, file=upload(), read_by='claude')
        with mock.patch.object(bankrec, 'read_pdf_statement', return_value=self.parsed):
            result = bankrec.split_combined_statement(first)
        self.assertEqual(len(result['filed']), 2)
        self.assertEqual(BankStatement.objects.get(bank_account=self.checking).end_balance, D('64854.99'))
        self.assertEqual(BankStatement.objects.get(bank_account=self.savings).end_balance, D('77200.01'))
        self.assertEqual(BankStatement.objects.count(), 2)

    def test_splitting_needs_a_stored_file(self):
        bare = BankStatement.objects.create(bank_account=self.checking, period_end=date(2026, 10, 30), end_balance=D('1'))
        with self.assertRaises(bankrec.StatementError):
            bankrec.split_combined_statement(bare)


class TheUploadOnTheBankScreenTests(TestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, True)
        self.override = override_settings(STORAGES=STORAGES, MEDIA_ROOT=self.media)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.prop = make_assoc('Lapensee Condos')
        self.checking = BankAccount.objects.create(association=self.prop, name='LAP 9511', bank_name='Truist', last4='9511', account=ledger.account(self.prop, '1110'))
        self.savings = BankAccount.objects.create(association=self.prop, name='Escrow Cash', bank_name='Truist', last4='9538', kind='reserve', account=ledger.account(self.prop, '1120'))
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.url = reverse('accounting:bank', args=[self.prop.pk, self.checking.pk, '2026-10'])

    def post(self, parsed, **extra):
        with mock.patch.object(bankrec, 'read_statement', return_value=(parsed, 'claude')):
            return self.client.post(self.url, {'action': 'upload', 'file': upload(), **extra}, follow=True)

    def test_a_combined_pdf_is_filed_on_both_accounts_and_the_message_says_so(self):
        response = self.post(bankrec.parse_statement_found(COMBINED))
        self.assertEqual(BankStatement.objects.count(), 2)
        text = ' '.join(str(m) for m in response.context['messages'])
        self.assertIn('Filed on LAP 9511', text)
        self.assertIn('Filed on Escrow Cash', text)
        self.assertEqual(response.request['PATH_INFO'], self.url)                        # back on the account it was uploaded to

    def test_the_bank_tabs_show_the_name_and_last_4(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn('LAP 9511</a>', html)                                              # the name already carries its digits
        self.assertIn('Escrow Cash (ending 9538)</a>', html)

    def test_an_account_nobody_claims_is_a_warning_not_a_failure(self):
        found = {'period_end': '2026-10-30', 'accounts': COMBINED['accounts'] + [{'account_name': 'OTHER', 'last4': '4444', 'end_balance': 1, 'lines': [{'date': '2026-10-03', 'description': 'FEE', 'amount': -1}]}]}
        response = self.post(bankrec.parse_statement_found(found))
        text = ' '.join(str(m) for m in response.context['messages'])
        self.assertIn('Not filed: OTHER ending 4444', text)
        self.assertEqual(BankStatement.objects.count(), 2)

    def test_one_account_uploaded_to_the_wrong_tab_is_filed_on_the_right_one_and_the_page_follows(self):
        response = self.post(bankrec.parse_statement_found({'period_end': '2026-10-30', 'accounts': [COMBINED['accounts'][1]]}))
        self.assertEqual(BankStatement.objects.get().bank_account_id, self.savings.pk)
        self.assertIn(f'/bank/{self.savings.pk}/', response.request['PATH_INFO'])
        self.assertIn('filed on Escrow Cash (ending 9538), not on LAP 9511', ' '.join(str(m) for m in response.context['messages']))

    def test_a_typed_balance_with_a_combined_file_is_an_error_and_nothing_is_filed(self):
        response = self.post(bankrec.parse_statement_found(COMBINED), end_balance='100')
        self.assertEqual(BankStatement.objects.count(), 0)
        self.assertIn('only for a statement with one account', ' '.join(str(m) for m in response.context['messages']))


class EditingABankAccountTests(TestCase):
    def setUp(self):
        self.override = override_settings(STORAGES=STORAGES)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.prop = make_assoc('Lapensee Condos')
        self.op = BankAccount.objects.create(association=self.prop, name='Operating', bank_name='Truist', last4='9511', account=ledger.account(self.prop, '1110'))
        self.esc = BankAccount.objects.create(association=self.prop, name='Escrow Cash', kind='reserve', account=ledger.account(self.prop, '1120'))
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.url = reverse('accounting:settings', args=[self.prop.pk])

    def edit(self, bank, **changes):
        data = {'action': 'bank_edit', 'bank': bank.pk, 'name': bank.name, 'bank_name': bank.bank_name, 'last4': bank.last4, 'kind': bank.kind, 'account': bank.account_id, **changes}
        return self.client.post(self.url, data, follow=True)

    def messages(self, response):
        return ' '.join(str(m) for m in response.context['messages'])

    def test_a_bank_account_is_renamed_in_place_with_no_duplicate(self):
        pk = self.op.pk
        response = self.edit(self.op, name='LAP 9511', bank_name='Truist Bank')
        self.assertEqual(BankAccount.objects.filter(association=self.prop).count(), 2)
        op = BankAccount.objects.get(pk=pk)
        self.assertEqual((op.name, op.bank_name, op.last4), ('LAP 9511', 'Truist Bank', '9511'))
        self.assertIn('Saved: LAP 9511', self.messages(response))

    def test_the_last_4_and_type_can_be_set_and_only_digits_are_kept(self):
        self.edit(self.esc, name='LAPRES 9538', last4='x9538', kind='reserve')
        esc = BankAccount.objects.get(pk=self.esc.pk)
        self.assertEqual((esc.name, esc.last4, esc.kind), ('LAPRES 9538', '9538', 'reserve'))
        response = self.edit(self.esc, last4='abcd')
        self.assertIn('digits', self.messages(response))
        self.assertEqual(BankAccount.objects.get(pk=self.esc.pk).last4, '9538')

    def test_a_name_another_bank_account_already_has_is_refused(self):
        response = self.edit(self.esc, name='operating')
        self.assertIn('already called', self.messages(response))
        self.assertEqual(BankAccount.objects.get(pk=self.esc.pk).name, 'Escrow Cash')

    def test_the_cash_account_can_change_only_while_nothing_is_tied_to_it(self):
        spare = ledger.account(self.prop, '1121')
        self.edit(self.esc, account=spare.pk)
        self.assertEqual(BankAccount.objects.get(pk=self.esc.pk).account_id, spare.pk)
        BankStatement.objects.create(bank_account=self.esc, period_end=date(2026, 10, 30), end_balance=D('1'))
        response = self.edit(self.esc, account=ledger.account(self.prop, '1120').pk)
        self.assertIn('cannot change here', self.messages(response))
        self.assertEqual(BankAccount.objects.get(pk=self.esc.pk).account_id, spare.pk)

    def test_a_payment_also_ties_the_cash_account_down(self):
        Payment.objects.create(association=self.prop, bank_account=self.esc, method='check', amount=D('5'), date=date(2026, 10, 1), payee_text='Garden Co')
        response = self.edit(self.esc, account=ledger.account(self.prop, '1121').pk)
        self.assertIn('cannot change here', self.messages(response))

    def test_a_cash_account_already_used_by_another_active_bank_account_is_refused(self):
        response = self.edit(self.esc, account=self.op.account_id)
        self.assertIn('already another bank account', self.messages(response))
        self.assertEqual(BankAccount.objects.get(pk=self.esc.pk).account.code, '1120')

    def test_stop_using_and_use_again(self):
        self.client.post(self.url, {'action': 'bank_off', 'bank': self.esc.pk})
        self.assertFalse(BankAccount.objects.get(pk=self.esc.pk).is_active)
        self.client.post(self.url, {'action': 'bank_on', 'bank': self.esc.pk})
        self.assertTrue(BankAccount.objects.get(pk=self.esc.pk).is_active)

    def test_another_associations_bank_account_cannot_be_edited_here(self):
        other = make_assoc('Other Condos')
        theirs = BankAccount.objects.create(association=other, name='Theirs', account=ledger.account(other, '1110'))
        response = self.client.post(self.url, {'action': 'bank_edit', 'bank': theirs.pk, 'name': 'Mine now', 'account': ledger.account(self.prop, '1110').pk}, follow=True)
        self.assertEqual(BankAccount.objects.get(pk=theirs.pk).name, 'Theirs')
        self.assertNotIn('Saved', self.messages(response))

    def test_the_settings_card_has_an_edit_form_for_each_bank_account_and_flags_a_missing_last_4(self):
        html = self.client.get(self.url).content.decode()
        self.assertEqual(html.count('name="action" value="bank_edit"'), 2)
        self.assertIn('No last 4', html)                                                  # the escrow account has none yet
        self.assertIn('Cash account in the books', html)

    def test_adding_a_bank_account_still_works(self):
        self.client.post(self.url, {'action': 'bank', 'name': 'LAPRES 7254', 'bank_name': 'Citizens', 'last4': '7254', 'kind': 'reserve', 'account': ledger.account(self.prop, '1121').pk})
        added = BankAccount.objects.get(association=self.prop, name='LAPRES 7254')
        self.assertEqual((added.last4, added.kind), ('7254', 'reserve'))

    def test_label_shows_the_last_4_unless_the_name_has_it(self):
        self.assertEqual(self.op.label, 'Operating (ending 9511)')
        self.op.name = 'LAP 9511'
        self.assertEqual(self.op.label, 'LAP 9511')
        self.assertEqual(self.esc.label, 'Escrow Cash')
