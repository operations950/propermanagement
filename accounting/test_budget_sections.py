from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from . import budgeting as bg
from .models import Account, MasterAccount
from .tests import close, make_assoc, spend

STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}


@override_settings(STORAGES=STORAGES)
class BudgetSectionOfAnAccountTests(TestCase):
    """Where an expense account sits in the budget (Administrative, Utilities, Operating) is set on the chart of accounts, once, for every association."""

    def setUp(self):
        self.prop = make_assoc('Section Condos')
        self.other = make_assoc('Other Condos')
        for m in range(1, 7):
            spend(self.prop, date(2026, m, 10), '6906', '600')
        close(self.prop, range(1, 7))
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.url = reverse('accounting:budget_year', args=[self.prop.pk, 2027])
        self.client.post(self.url, {'action': 'start'})
        self.chart = reverse('accounting:master_chart')

    def addable(self):
        from .models import Budget
        have = bg.versions(Budget.objects.get(association=self.prop, year=2027))
        ver = have['draft']
        state = bg.editor_state(ver, have)
        return {sec: [a['label'] for a in rows] for sec, rows in state['addable'].items()}

    def section(self, prop, code):
        return bg.section_of(Account.objects.get(association=prop, code=code))

    def test_insurance_financing_sits_with_the_insurance_in_administrative(self):
        self.assertEqual(MasterAccount.objects.get(code='6903').budget_section, 'Administrative')
        for prop in (self.prop, self.other):
            self.assertEqual(self.section(prop, '6903'), 'Administrative')
            self.assertEqual(self.section(prop, '6882'), 'Administrative')
        self.assertTrue(any(l.startswith('6903') for l in self.addable()['Administrative']))
        self.assertFalse(any(l.startswith('6903') for l in self.addable().get('Operating', [])))

    def test_nothing_else_moved(self):
        self.assertEqual((self.section(self.prop, '6410'), self.section(self.prop, '6893'), self.section(self.prop, '6906'), self.section(self.prop, '6320')),
                         ('Utilities', 'Operating', 'Operating', 'Administrative'))

    def test_changing_the_section_on_the_chart_moves_the_account_in_every_association_and_its_budget_list(self):
        master = MasterAccount.objects.get(code='6903')
        response = self.client.post(self.chart, {'action': 'section', 'account': master.pk, 'budget_section': 'Operating'}, follow=True)
        self.assertContains(response, '6903 Insurance Financing is in the Operating section of every budget.')
        for prop in (self.prop, self.other):
            self.assertEqual(self.section(prop, '6903'), 'Operating')
        self.assertTrue(any(l.startswith('6903') for l in self.addable()['Operating']))
        self.assertFalse(any(l.startswith('6903') for l in self.addable()['Administrative']))

    def test_a_section_can_change_even_when_entries_are_posted_to_the_account(self):
        spend(self.prop, date(2026, 8, 10), '6903', '100')
        master = MasterAccount.objects.get(code='6903')
        self.client.post(self.chart, {'action': 'section', 'account': master.pk, 'budget_section': 'Utilities'})
        self.assertEqual(self.section(self.prop, '6903'), 'Utilities')

    def test_a_bad_section_or_a_non_expense_account_is_refused(self):
        master = MasterAccount.objects.get(code='6903')
        self.client.post(self.chart, {'action': 'section', 'account': master.pk, 'budget_section': 'Reserves'})
        self.assertEqual(MasterAccount.objects.get(code='6903').budget_section, 'Administrative')
        income = MasterAccount.objects.get(code='4800')
        response = self.client.post(self.chart, {'action': 'section', 'account': income.pk, 'budget_section': 'Operating'}, follow=True)
        self.assertContains(response, 'Only expense accounts have a place in the budget sections.')
        self.assertEqual(MasterAccount.objects.get(code='4800').budget_section, '')

    def test_a_new_account_is_added_with_its_section_everywhere(self):
        self.client.post(self.chart, {'action': 'add', 'code': '6921', 'name': 'Pool Maintenance', 'kind': 'expense', 'budget_section': 'Utilities'})
        self.assertEqual(MasterAccount.objects.get(code='6921').budget_section, 'Utilities')
        for prop in (self.prop, self.other):
            self.assertEqual(self.section(prop, '6921'), 'Utilities')
        self.assertTrue(any(l.startswith('6921') for l in self.addable()['Utilities']))
        self.client.post(self.chart, {'action': 'add', 'code': '4999', 'name': 'Misc Income Two', 'kind': 'income', 'budget_section': 'Utilities'})
        self.assertEqual(MasterAccount.objects.get(code='4999').budget_section, '')                                # only expenses have one

    def test_a_new_expense_account_defaults_to_operating(self):
        self.client.post(self.chart, {'action': 'add', 'code': '6922', 'name': 'Another Expense', 'kind': 'expense'})
        self.assertEqual(MasterAccount.objects.get(code='6922').budget_section, 'Operating')

    def test_an_account_with_no_section_set_falls_back_to_the_old_rule_by_number(self):
        Account.objects.filter(association=self.prop, code__in=['6882', '6410', '6893']).update(budget_section='')
        self.assertEqual((self.section(self.prop, '6882'), self.section(self.prop, '6410'), self.section(self.prop, '6893')), ('Administrative', 'Utilities', 'Operating'))

    def test_the_chart_page_has_a_section_picker_for_each_expense_account_and_the_add_form(self):
        html = self.client.get(self.chart).content.decode()
        self.assertIn('name="budget_section"', html)
        self.assertIn('Budget section', html)
        self.assertIn('Budget section of 6903 Insurance Financing', html)
        self.assertNotIn('Budget section of 4800', html)

    def test_the_chart_page_has_a_search_box_that_filters_as_you_type(self):
        MasterAccount.objects.filter(code='6906').update(is_active=False)
        html = self.client.get(self.chart).content.decode()
        self.assertIn('id="chart-q"', html)
        self.assertIn('placeholder="Search by number or name', html)
        self.assertIn('data-acct="6903 insurance financing"', html)                                               # every row carries its number and name to search
        self.assertIn('data-acct="6906 pest control off"', html)                                                   # a switched-off account can be found by typing "off"
        self.assertIn("q.addEventListener('input', run)", html)
        self.assertIn("e.key === 'Escape'", html)
        self.assertEqual(html.count('" data-group>'), 5)                                                              # one block per kind of account, hidden when nothing in it matches
