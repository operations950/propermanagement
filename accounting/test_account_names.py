"""Every association uses the company chart's names: there is no association-own wording for an account."""
from django.test import TestCase

from . import ledger
from .tests import make_assoc


class AccountNameTests(TestCase):
    def setUp(self):
        self.prop = make_assoc('Names Condos')

    def test_the_company_chart_sets_the_name_of_every_account(self):
        a = ledger.account(self.prop, '6205')
        master = a.name
        a.name = 'Something typed by hand'
        a.save()
        ledger.ensure_chart(self.prop)
        a.refresh_from_db()
        self.assertEqual(a.name, master)

    def test_there_is_no_way_to_keep_an_own_name(self):
        from .models import Account
        self.assertNotIn('name_locked', [f.name for f in Account._meta.get_fields()])

    def test_a_new_account_in_the_company_chart_is_added_with_its_name(self):
        ledger.account(self.prop, '6906').delete()
        self.assertEqual(ledger.ensure_chart(self.prop), 1)
