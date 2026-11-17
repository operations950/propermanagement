"""Recurring journal entries (a loan payment split into principal and interest): post or skip each month, amounts changed first, the rest follows; and the Castlewood setup command."""
from datetime import date
from decimal import Decimal as D
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import Property

from . import bankrec, ledger, scheduled
from .models import BankAccount, BankLine, BankSplitTemplate, BankStatement, JournalEntry, JournalRule, JournalRuleLine, JournalRuleSkip
from .test_bank_accounts import STORAGES
from .tests import make_assoc

OCT, NOV = date(2026, 10, 1), date(2026, 11, 1)
TODAY = date(2026, 11, 20)


class EntryBase(TestCase):
    def setUp(self):
        self.prop = make_assoc('Castlewood Villas')
        self.bank = BankAccount.objects.create(association=self.prop, name='Wood 8021', bank_name='Seacoast', last4='8021', kind='reserve', account=ledger.account(self.prop, '1130'))
        self.principal, self.interest = ledger.account(self.prop, '2311'), ledger.account(self.prop, '6611')
        self.user = get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678')

    def loan_rule(self, auto=False):
        return scheduled.save_entry_rule(self.prop, 'Loan payment', 13, [
            {'account': self.principal, 'side': 'debit', 'amount': D('4232.73'), 'is_rest': True},
            {'account': self.interest, 'side': 'debit', 'amount': D('2322.03')},
            {'account': self.bank.account, 'side': 'credit', 'amount': D('6554.76')}], auto_post=auto)

    def line_pk(self, rule, code):
        return rule.lines.get(account__code=code).pk


class SavingTests(EntryBase):
    def test_a_rule_keeps_its_lines_and_the_rest_balances_the_entry(self):
        rule = self.loan_rule()
        self.assertEqual((rule.kind, rule.day_of_month, rule.auto_post), ('entry', 13, False))
        self.assertEqual([(l.account.code, l.side, l.amount, l.is_rest) for l in rule.lines.all()],
                         [('2311', 'debit', D('4232.73'), True), ('6611', 'debit', D('2322.03'), False), ('1130', 'credit', D('6554.76'), False)])

    def test_an_entry_that_does_not_balance_is_refused(self):
        with self.assertRaises(ledger.LedgerError) as e:
            scheduled.save_entry_rule(self.prop, 'Bad', 5, [{'account': self.interest, 'side': 'debit', 'amount': D('100')}, {'account': self.bank.account, 'side': 'credit', 'amount': D('90')}])
        self.assertIn('does not balance', str(e.exception))
        self.assertFalse(JournalRule.objects.filter(name='Bad').exists())

    def test_needs_two_lines_a_name_a_day_and_at_most_one_rest(self):
        two = [{'account': self.interest, 'side': 'debit', 'amount': D('100')}, {'account': self.bank.account, 'side': 'credit', 'amount': D('100')}]
        for kwargs in ({'name': '', 'day': 5, 'rows': two}, {'name': 'x', 'day': 40, 'rows': two}, {'name': 'x', 'day': 5, 'rows': two[:1]},
                       {'name': 'x', 'day': 5, 'rows': [dict(r, is_rest=True) for r in two]}):
            with self.assertRaises(ledger.LedgerError):
                scheduled.save_entry_rule(self.prop, kwargs['name'], kwargs['day'], kwargs['rows'])

    def test_changing_a_rule_replaces_its_lines(self):
        rule = self.loan_rule()
        scheduled.save_entry_rule(self.prop, 'Loan payment', 20, [{'account': self.interest, 'side': 'debit', 'amount': D('50')}, {'account': self.bank.account, 'side': 'credit', 'amount': D('50')}], rule=rule)
        rule.refresh_from_db()
        self.assertEqual((rule.day_of_month, rule.lines.count()), (20, 2))


class PostingTests(EntryBase):
    def test_posting_uses_the_amounts_and_dates_the_entry_on_its_day(self):
        rule = self.loan_rule()
        entry = scheduled.post_entry_rule(self.prop, rule, OCT, self.user)
        self.assertEqual(entry.date, date(2026, 10, 13))
        self.assertEqual({l.account.code: (l.debit, l.credit) for l in entry.lines.all()}, {'2311': (D('4232.73'), D('0.00')), '6611': (D('2322.03'), D('0.00')), '1130': (D('0.00'), D('6554.76'))})
        self.assertEqual(entry.source_ref, f'jrule:{rule.pk}:2026-10')

    def test_changing_the_interest_moves_the_principal_and_next_month_starts_from_it(self):
        rule = self.loan_rule()
        entry = scheduled.post_entry_rule(self.prop, rule, OCT, self.user, amounts={self.line_pk(rule, '6611'): D('2300.00')})
        self.assertEqual({l.account.code: l.debit for l in entry.lines.all() if l.debit}, {'2311': D('4254.76'), '6611': D('2300.00')})
        self.assertEqual({r['account'].code: r['amount'] for r in scheduled.entry_amounts(rule)}, {'2311': D('4254.76'), '6611': D('2300.00'), '1130': D('6554.76')})

    def test_an_amount_that_leaves_the_rest_below_zero_is_refused(self):
        rule = self.loan_rule()
        with self.assertRaises(ledger.LedgerError):
            scheduled.post_entry_rule(self.prop, rule, OCT, self.user, amounts={self.line_pk(rule, '6611'): D('7000.00')})
        self.assertFalse(JournalEntry.objects.filter(source_ref__startswith='jrule:').exists())

    def test_a_month_cannot_be_posted_twice(self):
        rule = self.loan_rule()
        scheduled.post_entry_rule(self.prop, rule, OCT, self.user)
        with self.assertRaises(ledger.LedgerError):
            scheduled.post_entry_rule(self.prop, rule, OCT, self.user)

    def test_a_closed_month_cannot_be_posted_into(self):
        rule = self.loan_rule()
        ledger.lock_month(self.prop, OCT, self.user)
        with self.assertRaises(ledger.LedgerError):
            scheduled.post_entry_rule(self.prop, rule, OCT, self.user)

    def test_a_reserve_rule_still_works_beside_it(self):
        JournalRule.objects.create(association=self.prop, kind='reserve', name='Reserve contribution', day_of_month=27)
        self.loan_rule()
        self.assertEqual(len(scheduled.pending_entries(self.prop, OCT, TODAY)), 2)

    def test_the_automatic_job_posts_only_entries_whose_own_switch_is_on(self):
        off, on = self.loan_rule(), scheduled.save_entry_rule(self.prop, 'Auto', 5, [{'account': self.interest, 'side': 'debit', 'amount': D('10')}, {'account': self.bank.account, 'side': 'credit', 'amount': D('10')}], auto_post=True)
        made = scheduled.post_due(self.prop, TODAY, automatic=True)
        self.assertEqual(sorted(e.source_ref for e in made), [f'jrule:{on.pk}:2026-10', f'jrule:{on.pk}:2026-11'])          # last month and this one, as for the reserve entry
        self.assertFalse(scheduled.posted_entry(self.prop, off, NOV))


class SkippingAndStatusTests(EntryBase):
    def test_a_rule_is_pending_until_it_is_posted_or_skipped(self):
        rule = self.loan_rule()
        self.assertEqual([r['rule'].pk for r in scheduled.pending_entries(self.prop, OCT, TODAY)], [rule.pk])
        st = scheduled.rule_status(self.prop, rule, OCT, TODAY)
        self.assertEqual((st['posted'], st['skipped'], st['pending'], st['total']), (False, False, True, D('6554.76')))
        scheduled.skip_rule(self.prop, rule, OCT, self.user, 'paid by the developer')
        st = scheduled.rule_status(self.prop, rule, OCT, TODAY)
        self.assertEqual((st['posted'], st['skipped'], st['pending']), (False, True, False))
        self.assertEqual(scheduled.pending_entries(self.prop, OCT, TODAY), [])
        self.assertEqual(len(scheduled.pending_entries(self.prop, NOV, TODAY)), 1)           # another month is its own question

    def test_posting_a_skipped_month_takes_the_skip_away(self):
        rule = self.loan_rule()
        scheduled.skip_rule(self.prop, rule, OCT, self.user)
        scheduled.post_entry_rule(self.prop, rule, OCT, self.user)
        self.assertFalse(JournalRuleSkip.objects.exists())
        self.assertTrue(scheduled.rule_status(self.prop, rule, OCT, TODAY)['posted'])

    def test_a_posted_month_cannot_be_skipped_and_a_skip_can_be_undone(self):
        rule = self.loan_rule()
        scheduled.post_entry_rule(self.prop, rule, OCT, self.user)
        with self.assertRaises(ledger.LedgerError):
            scheduled.skip_rule(self.prop, rule, OCT, self.user)
        scheduled.skip_rule(self.prop, rule, NOV, self.user)
        scheduled.unskip_rule(rule, NOV)
        self.assertTrue(scheduled.rule_status(self.prop, rule, NOV, TODAY)['pending'])

    def test_a_reversed_entry_makes_the_month_pending_again(self):
        rule = self.loan_rule()
        entry = scheduled.post_entry_rule(self.prop, rule, OCT, self.user)
        ledger.reverse_entry(entry, date(2026, 10, 14), self.user)
        self.assertTrue(scheduled.rule_status(self.prop, rule, OCT, TODAY)['pending'])

    def test_a_stopped_rule_is_not_asked_about(self):
        rule = self.loan_rule()
        JournalRule.objects.filter(pk=rule.pk).update(is_active=False)
        self.assertEqual(scheduled.pending_entries(self.prop, OCT, TODAY), [])

    def test_the_old_status_shape_still_works_for_the_close_sheet(self):
        rule = self.loan_rule()
        st = scheduled.status(self.prop, rule, OCT, TODAY)
        self.assertEqual((st['state'], st['day'], st['split']['total']), ('due', date(2026, 10, 13), D('6554.76')))
        scheduled.post_entry_rule(self.prop, rule, OCT, self.user)
        st = scheduled.status(self.prop, rule, OCT, TODAY)
        self.assertEqual((st['state'], st['entry'].date), ('posted', date(2026, 10, 13)))

    def test_the_reserve_rule_can_be_skipped_too(self):
        reserve = JournalRule.objects.create(association=self.prop, kind='reserve', name='Reserve contribution', day_of_month=27)
        scheduled.skip_rule(self.prop, reserve, OCT, self.user)
        self.assertEqual(scheduled.status(self.prop, reserve, OCT, TODAY)['state'], 'skipped')
        self.assertTrue(scheduled.rule_status(self.prop, reserve, OCT, TODAY)['skipped'])


@override_settings(STORAGES=STORAGES)
class TheScreenTests(EntryBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        self.url = reverse('accounting:recurring', args=[self.prop.pk])

    def acct(self, code):
        return ledger.account(self.prop, code).pk

    def test_a_new_entry_is_made_from_the_screen(self):
        r = self.client.post(self.url, {'action': 'je_save', 'month': '2026-10', 'name': 'Loan payment', 'day_of_month': '13',
                                        'je_account_1': self.acct('2311'), 'je_side_1': 'debit', 'je_rest': '1',
                                        'je_account_2': self.acct('6611'), 'je_side_2': 'debit', 'je_amount_2': '2,322.03',
                                        'je_account_3': self.acct('1130'), 'je_side_3': 'credit', 'je_amount_3': '6554.76'}, follow=True)
        self.assertContains(r, 'comes up on the close sheet')
        rule = JournalRule.objects.get(name='Loan payment')
        self.assertEqual([(l.account.code, l.amount) for l in rule.lines.all()], [('2311', D('4232.73')), ('6611', D('2322.03')), ('1130', D('6554.76'))])

    def test_posting_with_a_changed_amount_and_skipping_from_the_screen(self):
        rule = self.loan_rule()
        r = self.client.post(self.url, {'action': 'je_post', 'rule': rule.pk, 'month': '2026-10', f'amt_{self.line_pk(rule, "6611")}': '2300.00', f'amt_{self.line_pk(rule, "1130")}': '6554.76'}, follow=True)
        self.assertContains(r, 'Posted J-')
        self.assertEqual(JournalEntry.objects.get(source_ref=f'jrule:{rule.pk}:2026-10').lines.get(account__code='2311').debit, D('4254.76'))
        r = self.client.post(self.url, {'action': 'je_skip', 'rule': rule.pk, 'month': '2026-11'}, follow=True)
        self.assertContains(r, 'is skipped for November 2026')
        self.assertTrue(JournalRuleSkip.objects.filter(rule=rule, month=NOV).exists())
        self.client.post(self.url, {'action': 'je_unskip', 'rule': rule.pk, 'month': '2026-11'})
        self.assertFalse(JournalRuleSkip.objects.exists())

    def test_the_entries_tab_shows_the_entry_with_post_and_skip(self):
        self.loan_rule()
        html = self.client.get(self.url + '?tab=entries&month=2026-10').content.decode()
        self.assertIn('Loan payment', html)
        self.assertIn('Post October', html)
        self.assertIn('Skip this month', html)
        self.assertIn('New recurring entry', html)

    def test_a_reserve_rule_cannot_be_posted_as_an_entry_and_the_other_way_round(self):
        reserve = JournalRule.objects.create(association=self.prop, kind='reserve', name='Reserve contribution', day_of_month=27)
        r = self.client.post(self.url, {'action': 'je_post', 'rule': reserve.pk, 'month': '2026-10'})
        self.assertEqual(r.status_code, 404)
        loan = self.loan_rule()
        r = self.client.post(self.url, {'action': 'jr_post', 'rule': loan.pk, 'month': '2026-10'})
        self.assertEqual(r.status_code, 404)


class CastlewoodSetupTests(TestCase):
    def setUp(self):
        self.prop = make_assoc('Castlewood Villas')
        self.bank = BankAccount.objects.create(association=self.prop, name='Wood 8021 Assessment Acct', bank_name='Seacoast', last4='8021', kind='reserve', account=ledger.account(self.prop, '1130'))

    def run_command(self, *args):
        out = StringIO()
        call_command('setup_castlewood_loan_entry', *args, stdout=out)
        return out.getvalue()

    def test_a_dry_run_changes_nothing(self):
        out = self.run_command()
        self.assertIn('DRY RUN', out)
        self.assertFalse(JournalRule.objects.filter(kind='entry').exists())
        self.assertFalse(BankSplitTemplate.objects.exists())

    def test_apply_makes_the_entry_and_the_remembered_split_once(self):
        self.run_command('--apply')
        self.run_command('--apply')
        rule = JournalRule.objects.get(kind='entry')
        self.assertEqual((rule.name, rule.day_of_month, rule.auto_post, rule.is_active), ('Monthly Spec Assmnt Loan Payment', 13, False, True))
        self.assertEqual([(l.account.code, l.side, l.amount, l.is_rest) for l in rule.lines.all()],
                         [('2311', 'debit', D('4232.73'), True), ('6611', 'debit', D('2322.03'), False), ('1130', 'credit', D('6554.76'), False)])
        tpl = BankSplitTemplate.objects.get(bank_account=self.bank)
        self.assertEqual((tpl.words, tpl.outgoing), ('automatic ln paymt automatic loan pay', True))

    def test_the_remembered_split_shows_up_on_the_statement_line_and_the_entry_posts_to_the_same_cash(self):
        self.run_command('--apply')
        stmt = BankStatement.objects.create(bank_account=self.bank, period_end=date(2026, 11, 30), end_balance=D('0'))
        line = BankLine.objects.create(statement=stmt, date=date(2026, 11, 13), description='#Automatic Ln Paymt AUTOMATIC LOAN PAY', amount=D('-6554.76'), kind='withdrawal')
        rows = bankrec.suggested_splits([line])[line.pk]['rows']
        self.assertEqual([(r['account'].code, r['amount']) for r in rows], [('2311', D('4232.73')), ('6611', D('2322.03'))])
        rule = JournalRule.objects.get(kind='entry')
        entry = scheduled.post_entry_rule(self.prop, rule, date(2026, 11, 1), None)
        self.assertEqual(entry.lines.get(credit=D('6554.76')).account_id, self.bank.account_id)

    def test_it_stops_with_a_reason_when_castlewood_or_the_8021_account_is_missing(self):
        BankAccount.objects.all().delete()
        with self.assertRaises(CommandError):
            self.run_command('--apply')
        Property.objects.all().delete()
        with self.assertRaises(CommandError):
            self.run_command()
