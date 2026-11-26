"""An association can keep its own name for an account (its old system's wording) while the company chart is kept in line."""
from django.test import TestCase

from . import ledger
from .tests import make_assoc


class AccountNameTests(TestCase):
    def setUp(self):
        self.prop = make_assoc('Names Condos')

    def test_the_company_chart_still_sets_the_names_of_an_ordinary_account(self):
        a = ledger.account(self.prop, '6205')
        master = a.name
        a.name = 'Something typed by hand'
        a.save()
        ledger.ensure_chart(self.prop)
        a.refresh_from_db()
        self.assertEqual(a.name, master)

    def test_a_locked_name_stays_while_the_rest_still_follows_the_company_chart(self):
        a = ledger.account(self.prop, '6205')
        a.name, a.name_locked, a.is_active = 'Land Lease - Monthly', True, False
        a.save()
        ledger.ensure_chart(self.prop)
        a.refresh_from_db()
        self.assertEqual(a.name, 'Land Lease - Monthly')
        self.assertTrue(a.is_active)                              # switched on again by the company chart

    def test_a_new_account_in_the_company_chart_is_added_with_its_name(self):
        ledger.account(self.prop, '6906').delete()
        self.assertEqual(ledger.ensure_chart(self.prop), 1)
        self.assertFalse(ledger.account(self.prop, '6906').name_locked)
