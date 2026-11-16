"""The close sheet's reserve check (with its acknowledgement) and its last step, sending the financials to the board."""
import re
from datetime import date
from decimal import Decimal as D
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import Contact

from . import board_send, closing, ledger, reserve_check
from .models import BankAccount, BoardPackageSend, BoardTerm, MonthLock, ReserveAcknowledgement
from .tests import bank_for, make_assoc

OCT = date(2026, 10, 1)
LOCMEM = 'django.core.mail.backends.locmem.EmailBackend'
STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}


@override_settings(STORAGES=STORAGES)
class Base(TestCase):
    def setUp(self):
        self.prop = make_assoc()
        bank_for(self.prop)
        self.user = get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678')
        self.client.force_login(self.user)

    def items(self):
        return {i['key']: i for i in closing.close_sheet(self.prop, OCT, today=date(2026, 11, 10))['items']}

    def reserve_bank(self):
        return BankAccount.objects.create(association=self.prop, name='Reserve', kind='reserve', account=ledger.account(self.prop, '1120'))

    def fund(self, cash, funds):
        """Reserve cash and reserve funds, which differ by `cash - funds`."""
        ledger.post_entry(self.prop, date(2026, 10, 5), [{'account': ledger.account(self.prop, '1120'), 'debit': D(cash)}, {'account': ledger.account(self.prop, '1110'), 'credit': D(cash)}], memo='cash to reserves')
        ledger.post_entry(self.prop, date(2026, 10, 5), [{'account': ledger.account(self.prop, '1110'), 'debit': D(funds)}, {'account': ledger.account(self.prop, '3834'), 'credit': D(funds)}], memo='fund')

    def move_cash(self, amount):
        ledger.post_entry(self.prop, date(2026, 10, 6), [{'account': ledger.account(self.prop, '1120'), 'debit': D(amount)}, {'account': ledger.account(self.prop, '1110'), 'credit': D(amount)}], memo='more cash to reserves')

    def board(self, name, position, email):
        person = Contact.objects.create(name=name, contact_type=Contact.ContactType.BOARD_MEMBER, email=email)
        return BoardTerm.objects.create(association=self.prop, person=person, position=position)


class ReserveCheckTests(Base):
    def test_no_reserve_bank_means_no_step(self):
        self.assertNotIn('reserve_tie', self.items())

    def test_the_card_says_where_it_stands_without_any_dollar_amounts(self):
        self.reserve_bank()
        self.fund('1000', '1000')
        card = self.items()['reserve_tie']
        self.assertEqual((card['status'], card['detail']), ('done', 'Matches.'))
        self.assertNotIn('$', card['detail'])
        self.move_cash('250')                                          # cash now 1,250 and funds 1,000
        card = self.items()['reserve_tie']
        self.assertEqual((card['status'], card['detail']), ('attention', 'Differs: look at it.'))
        self.assertNotIn('$', card['detail'])
        self.assertFalse(card['blocks'])
        html = self.client.get(reverse('accounting:close', args=[self.prop.pk, '2026-10'])).content.decode()
        self.assertIn('Reserve funds equal reserve cash', html)
        self.assertNotIn('1,250', html)                                # the numbers live on the reserve screen, not on the card

    def test_the_card_opens_the_reserve_screen(self):
        self.reserve_bank()
        self.assertEqual(self.items()['reserve_tie']['url'], reverse('accounting:reserve_check', args=[self.prop.pk, '2026-10']))

    def test_the_screen_shows_every_reserve_account_and_fund_and_the_difference(self):
        self.reserve_bank()
        self.fund('1250', '1000')
        html = self.client.get(reverse('accounting:reserve_check', args=[self.prop.pk, '2026-10'])).content.decode()
        self.assertIn('Reserve Fund Roof Replacement', html)
        self.assertIn('1,250.00', html)
        self.assertIn('1,000.00', html)
        self.assertIn('Difference: $250.00', html)
        self.assertIn('Acknowledge the difference', html)

    def test_acknowledging_with_a_reason_completes_the_step_and_it_can_be_removed(self):
        self.reserve_bank()
        self.fund('1250', '1000')
        url = reverse('accounting:reserve_check', args=[self.prop.pk, '2026-10'])
        r = self.client.post(url, {'action': 'acknowledge', 'reason': ''}, follow=True)
        self.assertContains(r, 'a reason is needed')
        self.assertFalse(ReserveAcknowledgement.objects.exists())
        self.assertEqual(self.items()['reserve_tie']['status'], 'attention')
        self.client.post(url, {'action': 'acknowledge', 'reason': 'Roof deposit paid from operating'})
        ack = ReserveAcknowledgement.objects.get()
        self.assertEqual((ack.difference, ack.reason, ack.acknowledged_by), (D('250.00'), 'Roof deposit paid from operating', self.user))
        card = self.items()['reserve_tie']
        self.assertEqual((card['status'], card['detail']), ('done', 'Difference acknowledged.'))
        self.client.post(url, {'action': 'remove'})
        self.assertFalse(ReserveAcknowledgement.objects.exists())
        self.assertEqual(self.items()['reserve_tie']['status'], 'attention')

    def test_an_acknowledgement_stops_counting_once_the_difference_changes(self):
        self.reserve_bank()
        self.fund('1250', '1000')
        reserve_check.acknowledge(self.prop, OCT, 'Roof deposit', self.user)
        self.assertEqual(self.items()['reserve_tie']['status'], 'done')
        ledger.post_entry(self.prop, date(2026, 10, 20), [{'account': ledger.account(self.prop, '1120'), 'debit': D('40')}, {'account': ledger.account(self.prop, '1110'), 'credit': D('40')}])
        self.assertEqual(self.items()['reserve_tie']['status'], 'attention')
        html = self.client.get(reverse('accounting:reserve_check', args=[self.prop.pk, '2026-10'])).content.decode()
        self.assertIn('needs acknowledging again', html)

    def test_there_is_nothing_to_acknowledge_when_they_match(self):
        self.reserve_bank()
        self.fund('500', '500')
        with self.assertRaises(ValueError):
            reserve_check.acknowledge(self.prop, OCT, 'No need', self.user)


@override_settings(EMAIL_BACKEND=LOCMEM, STORAGES=STORAGES)
class BoardSendTests(Base):
    def setUp(self):
        super().setUp()
        self.url = reverse('accounting:send_board', args=[self.prop.pk, '2026-10'])
        patcher = mock.patch('accounting.board_send.package.build', return_value=b'%PDF-1.4 test package')
        patcher.start()
        self.addCleanup(patcher.stop)

    def close_month(self):
        ledger.lock_month(self.prop, OCT, self.user)

    def post(self, **extra):
        data = {'subject': 'Subject line', 'body': 'Hello board', 'approved': '1', **extra}
        return self.client.post(self.url, data, follow=True)

    def test_the_step_is_last_and_never_blocks_the_close(self):
        sheet = closing.close_sheet(self.prop, OCT, today=date(2026, 11, 10))
        keys = [i['key'] for i in sheet['items']]
        self.assertEqual(keys[-2:], ['lock', 'send_board'])
        send = sheet['items'][-1]
        self.assertFalse(send['blocks'])
        self.assertEqual(send['status'], 'not_started')

    def test_the_step_is_ready_once_the_month_is_closed_and_done_only_after_it_is_sent(self):
        self.close_month()
        self.assertEqual(self.items()['send_board']['status'], 'attention')
        self.assertFalse(closing.close_sheet(self.prop, OCT)['sent'])
        a = self.board('Pat President', 'President', 'pat@example.com')
        self.post(to=[a.pk])
        self.assertEqual(self.items()['send_board']['status'], 'done')
        self.assertTrue(closing.close_sheet(self.prop, OCT)['sent'])

    def test_a_month_is_fully_closed_only_when_it_is_locked_and_sent(self):
        self.assertNotEqual(closing.association_status(self.prop, OCT)[0], 'done')
        self.close_month()
        status, detail = closing.association_status(self.prop, OCT)
        self.assertEqual((status, detail), ('attention', 'Closed, financials not sent'))
        self.assertNotEqual(closing.checklist_step(OCT)[0], 'done')                 # the company checklist counts it closed only once it is sent
        a = self.board('Pat President', 'President', 'pat@example.com')
        self.post(to=[a.pk])
        status, detail = closing.association_status(self.prop, OCT)
        self.assertEqual(status, 'done')
        self.assertIn('sent', detail)
        self.assertEqual(closing.checklist_step(OCT)[0], 'done')

    def test_the_screen_lists_the_current_board_and_flags_anyone_without_an_email(self):
        self.close_month()
        self.board('Pat President', 'President', 'pat@example.com')
        self.board('Sam Secretary', 'Secretary', '')
        former = self.board('Old Member', 'Treasurer', 'old@example.com')
        former.end_date = date(2026, 1, 1)
        former.save()
        html = self.client.get(self.url).content.decode()
        self.assertIn('pat@example.com', html)
        self.assertIn('no email on file', html)
        self.assertNotIn('Old Member', html)
        self.assertIn('admin@proper-realty.com', html)
        self.assertIn('ana@proper-realty.com', html)
        self.assertIn('justin@proper-realty.com', html)
        self.assertIn('Open and review the package', html)

    def test_the_email_goes_from_admin_with_ana_and_justin_copied_with_the_package_attached(self):
        self.close_month()
        a = self.board('Pat President', 'President', 'pat@example.com')
        b = self.board('Val Vice', 'Vice President', 'val@example.com')
        r = self.post(to=[a.pk, b.pk])
        self.assertContains(r, 'Sent on')
        self.assertEqual(len(mail.outbox), 1)
        m = mail.outbox[0]
        self.assertEqual(m.from_email, 'admin@proper-realty.com')
        self.assertEqual(sorted(m.to), ['pat@example.com', 'val@example.com'])
        self.assertEqual(m.cc, ['ana@proper-realty.com', 'justin@proper-realty.com'])
        self.assertEqual(m.subject, 'Subject line')
        self.assertEqual(len(m.attachments), 1)
        self.assertEqual(m.attachments[0][2], 'application/pdf')
        rec = BoardPackageSend.objects.get()
        self.assertEqual((rec.association, rec.month, rec.sent_by), (self.prop, OCT, self.user))
        self.assertEqual({x['email'] for x in rec.recipients}, {'pat@example.com', 'val@example.com'})
        self.assertEqual(rec.cc, ['ana@proper-realty.com', 'justin@proper-realty.com'])

    def test_nothing_is_ever_sent_to_a_blank_address(self):
        self.close_month()
        a = self.board('Sam Secretary', 'Secretary', '')
        r = self.post(to=[a.pk])                                      # a person with no email cannot be picked
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(BoardPackageSend.objects.exists())
        self.assertContains(r, 'has no email address on file')
        r = self.post()                                                # nobody chosen at all
        self.assertContains(r, 'at least one board member who has an email')
        html = self.client.get(self.url).content.decode()
        self.assertRegex(html, r'<button class="btn btn-primary"\s+disabled')
        self.assertIn('No board member has an email address on file', html)
        with self.assertRaises(board_send.BoardSendError):             # and the code refuses it even if asked directly
            board_send.send(self.prop, OCT, [{'name': 'Sam', 'email': ''}], 'S', 'B')
        with self.assertRaises(board_send.BoardSendError):
            board_send.send(self.prop, OCT, [{'name': 'Sam', 'email': 'not-an-address'}], 'S', 'B')
        self.assertEqual(len(mail.outbox), 0)

    def test_an_unreviewed_package_is_not_sent(self):
        self.close_month()
        a = self.board('Pat President', 'President', 'pat@example.com')
        r = self.client.post(self.url, {'subject': 'S', 'body': 'B', 'to': [a.pk]}, follow=True)
        self.assertContains(r, 'I have reviewed the package')
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(BoardPackageSend.objects.exists())

    def test_an_open_month_cannot_be_sent(self):
        a = self.board('Pat President', 'President', 'pat@example.com')
        r = self.post(to=[a.pk])
        self.assertContains(r, 'Close October 2026 first')
        self.assertEqual(len(mail.outbox), 0)
        self.assertIn('Close October 2026 first', self.client.get(self.url).content.decode())

    def test_once_sent_it_shows_who_it_went_to_and_a_second_send_needs_send_again(self):
        self.close_month()
        a = self.board('Pat President', 'President', 'pat@example.com')
        self.post(to=[a.pk])
        html = self.client.get(self.url).content.decode()
        self.assertIn('Pat President', html)
        self.assertIn('pat@example.com', html)
        self.assertIn('Send again', html)
        r = self.post(to=[a.pk])
        self.assertContains(r, 'Choose &quot;Send again&quot;')
        self.assertEqual(len(mail.outbox), 1)
        self.post(to=[a.pk], again='1')
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(BoardPackageSend.objects.count(), 2)

    def test_the_default_message_is_friendly_and_has_no_names_accounts_or_amounts(self):
        body = board_send.default_body(self.prop, OCT)
        self.assertTrue(body.startswith('Hello,'))
        self.assertIn('October 2026', body)
        self.assertIn('Proper Realty management', body)
        self.assertIsNone(re.search(r'\$|\d{4,}', body.replace('2026', '')))
        self.assertEqual(board_send.default_subject(self.prop, OCT), f'{self.prop.name} financial package for October 2026')

    def test_a_mail_server_failure_records_nothing(self):
        self.close_month()
        a = self.board('Pat President', 'President', 'pat@example.com')
        with mock.patch('django.core.mail.EmailMessage.send', side_effect=OSError('boom')):
            r = self.post(to=[a.pk])
        self.assertContains(r, 'could not be sent')
        self.assertFalse(BoardPackageSend.objects.exists())

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.console.EmailBackend', STORAGES=STORAGES)
    def test_with_no_email_delivery_set_up_it_says_so_and_sends_nothing(self):
        self.close_month()
        a = self.board('Pat President', 'President', 'pat@example.com')
        r = self.post(to=[a.pk])
        self.assertContains(r, 'No email delivery is set up yet')
        self.assertFalse(BoardPackageSend.objects.exists())
