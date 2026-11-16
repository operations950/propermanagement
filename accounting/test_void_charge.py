from datetime import date
from decimal import Decimal as D

from django.test import TestCase

from core.models import Unit

from . import charges, ledger
from .models import Charge, JournalEntry
from .tests import make_assoc, owner_of


def balance_of(prop, code, day=date(2026, 10, 31)):
    for row in ledger.trial_balance(prop, date(2026, 10, 1), day)['rows']:
        if row['account'].code == code:
            return row['ending']
    return D('0')


class VoidingACharge(TestCase):
    def setUp(self):
        self.prop = make_assoc()
        self.unit = Unit.objects.filter(property=self.prop).first()
        self.owner = owner_of(self.prop, self.unit)

    def opening_charge(self, amount='1280.80'):
        """A balance carried from the old system: the receivable is on the books, the income never was."""
        c = Charge.objects.create(association=self.prop, unit=self.unit, owner=self.owner, kind=Charge.Kind.OPENING, description='Balance forward from Yardi, 31 to 60 days',
                                  due_date=date(2026, 8, 16), amount=D(amount), income_account=ledger.account(self.prop, '4500'))
        ledger.post_entry(self.prop, date(2026, 9, 30), [{'account': '1300', 'debit': D(amount), 'unit': self.unit, 'owner': self.owner},
                                                         {'account': '3070', 'credit': D(amount)}], memo='Opening balances', basis='accrual', source=JournalEntry.Source.OPENING)
        return c

    def test_voiding_an_opening_charge_comes_out_of_the_fund_balance_not_income(self):
        c = self.opening_charge()
        charges.void_charge(c, day=date(2026, 10, 31))
        entry = JournalEntry.objects.get(source=JournalEntry.Source.REVERSAL)
        accounts = {l.account.code: (l.debit, l.credit) for l in entry.lines.select_related('account')}
        self.assertEqual(accounts, {'3070': (D('1280.80'), D('0.00')), '1300': (D('0.00'), D('1280.80'))})
        self.assertEqual(balance_of(self.prop, '4500'), D('0'))            # October income is untouched
        self.assertEqual(balance_of(self.prop, '1300'), D('0'))            # nothing is owed any more
        self.assertTrue(Charge.objects.get(pk=c.pk).is_void)

    def test_voiding_an_ordinary_charge_still_takes_back_its_income(self):
        c = charges.post_charge(self.prop, self.unit, self.owner, Charge.Kind.ASSESSMENT, 'Monthly assessment', date(2026, 10, 1), D('615'))
        charges.void_charge(c, day=date(2026, 10, 31))
        self.assertEqual(balance_of(self.prop, '4500'), D('0'))            # the charge's income and its void cancel
        self.assertEqual(balance_of(self.prop, '1300'), D('0'))
        self.assertTrue(Charge.objects.get(pk=c.pk).is_void)
        entry = JournalEntry.objects.get(source=JournalEntry.Source.REVERSAL)
        self.assertEqual({l.account.code for l in entry.lines.select_related('account')}, {'4500', '1300'})
