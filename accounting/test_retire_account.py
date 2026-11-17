"""retire_account: fold one account into another in every association and delete it, so nothing is left hidden."""
from datetime import date
from decimal import Decimal as D
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from . import ledger
from .models import Account, JournalLine, MasterAccount, MonthLock
from .tests import make_assoc


class RetireAccountTests(TestCase):
    def setUp(self):
        self.a, self.b = make_assoc('Alpha Condos'), make_assoc('Beta Condos')
        for prop in (self.a, self.b):                                    # the old account, as it was in every association
            Account.objects.get_or_create(association=prop, code='3831', defaults={'name': 'Reserve Fund Interest', 'kind': 'equity'})
        MasterAccount.objects.get_or_create(code='3831', defaults={'name': 'Reserve Fund Interest', 'kind': 'equity'})
        ledger.post_entry(self.a, date(2026, 10, 31), [{'account': '1120', 'debit': D('5.74')}, {'account': '3831', 'credit': D('5.74'), 'memo': 'Interest'}], memo='Interest', basis='both')
        MonthLock.objects.create(association=self.a, month=date(2026, 10, 1))        # a closed month: the move changes no total, so it is allowed

    def run_it(self, *args):
        out = StringIO()
        call_command('retire_account', '3831', '--into', '3813', *args, stdout=out)
        return out.getvalue()

    def test_a_dry_run_changes_nothing(self):
        self.assertIn('DRY RUN', self.run_it())
        self.assertTrue(Account.objects.filter(code='3831').exists())
        self.assertEqual(JournalLine.objects.filter(account__code='3831').count(), 1)

    def test_apply_moves_the_lines_and_deletes_the_account_everywhere(self):
        before = ledger.balances_as_of(self.a, date(2026, 11, 30), ledger.ACCRUAL)
        self.assertIn('APPLIED', self.run_it('--apply'))
        self.assertFalse(Account.objects.filter(code='3831').exists())
        self.assertFalse(MasterAccount.objects.filter(code='3831').exists())
        line = JournalLine.objects.get(credit=D('5.74'))
        self.assertEqual(line.account.code, '3813')
        self.assertIn('was 3831 Reserve Fund Interest', line.memo)
        after = ledger.balances_as_of(self.a, date(2026, 11, 30), ledger.ACCRUAL)
        self.assertEqual(after[ledger.account(self.a, '3813').pk], D('-5.74'))
        self.assertEqual(sum(before.values()), sum(after.values()))                    # nothing else moved

    def test_it_can_be_run_again(self):
        self.run_it('--apply')
        self.assertIn('APPLIED', self.run_it('--apply'))

    def test_it_refuses_and_changes_nothing_when_the_new_account_is_missing(self):
        Account.objects.filter(association=self.b, code='3813').delete()
        with self.assertRaises(CommandError):
            self.run_it('--apply')
        self.assertEqual(Account.objects.filter(code='3831').count(), 2)
        self.assertEqual(JournalLine.objects.filter(account__code='3831').count(), 1)

    def test_new_associations_no_longer_get_the_account(self):
        from . import standard_chart as sc
        self.assertNotIn('3831', [code for code, name, kind in sc.CHART])
