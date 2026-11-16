"""The association month-end close in its new order, the automatic-posting switches, and the Owner collections screen with its 30 and 45 day alerts."""
import importlib
from datetime import date, timedelta
from unittest import mock

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import Contact

from . import charges, closing, delinquency, ledger, owner_collections, payables, scheduled
from .models import Bill, CaseEvent, Charge, ChargeRule, DelinquencyCase, JournalEntry, JournalRule, RecurringBill
from .tests import D, DelinquencyBase, bank_for, budget_with_reserves, make_assoc

STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}


def keys(sheet):
    return [i['key'] for i in sheet['items']]


@override_settings(STORAGES=STORAGES)
class TheCloseSheetInItsNewOrderTests(TestCase):
    def setUp(self):
        self.prop = make_assoc()
        self.bank = bank_for(self.prop)
        vendor = Contact.objects.create(name='FPL')
        self.fixed_vendor = Contact.objects.create(name='Pool Co')
        self.variable = RecurringBill.objects.create(association=self.prop, vendor=vendor, description='Electricity', account=ledger.account(self.prop, '6410'), amount=None, next_date=date(2026, 10, 24))
        self.fixed = RecurringBill.objects.create(association=self.prop, vendor=self.fixed_vendor, description='Pool service', account=ledger.account(self.prop, '6410'), amount=D('300'), next_date=date(2026, 10, 9))
        self.today = date(2026, 11, 10)
        self.month = date(2026, 10, 1)
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))

    def sheet(self):
        return closing.close_sheet(self.prop, self.month, today=self.today)

    def test_the_order_is_charges_bills_entries_then_the_bank_then_the_checks_late_fees_and_collections(self):
        from .models import BankAccount
        BankAccount.objects.create(association=self.prop, name='Reserve', kind='reserve', account=ledger.account(self.prop, '1120'))
        ks = keys(self.sheet())
        self.assertEqual(ks[:3], ['charges', 'recurring', 'entries'])
        banks = [k for k in ks if k.startswith('bank')]
        self.assertEqual(len(banks), 2)
        order = ['entries'] + banks + ['reserve_tie', 'variable_bills', 'late_fees', 'collections', 'lock', 'send_board']
        self.assertEqual([ks.index(k) for k in order], sorted(ks.index(k) for k in order))
        self.assertEqual(ks[-2:], ['lock', 'send_board'])

    def test_bills_waiting_is_no_longer_a_step_and_the_old_delinquencies_step_is_late_fees(self):
        ks = keys(self.sheet())
        self.assertNotIn('bills', ks)
        self.assertNotIn('delinquency', ks)
        self.assertIn('late_fees', ks)
        by = {i['key']: i for i in self.sheet()['items']}
        self.assertEqual(by['late_fees']['title'], 'Charge late fees and interest')
        self.assertEqual(by['entries']['title'], 'Recurring journal entries')

    def test_only_the_bank_reconciliations_block_the_month(self):
        by = {i['key']: i for i in self.sheet()['items']}
        self.assertEqual({k for k, i in by.items() if i['blocks'] and k != 'lock'}, {'bank%d' % self.bank.pk})
        self.assertFalse(by['collections']['blocks'])
        self.assertFalse(by['late_fees']['blocks'])

    def test_a_variable_bill_is_not_flagged_on_the_recurring_bills_step(self):
        by = {i['key']: i for i in self.sheet()['items']}
        self.assertEqual(by['recurring']['status'], 'attention')                              # the fixed pool bill has not posted
        self.assertIn('Pool service', by['recurring']['detail'])
        self.assertNotIn('Electricity', by['recurring']['detail'])
        self.assertIn('1 variable bill', by['recurring']['detail'])
        payables.post_recurring(self.prop, date(2026, 10, 31))
        by = {i['key']: i for i in self.sheet()['items']}
        self.assertEqual(by['recurring']['status'], 'done')                                   # nothing but the variable bill is left, and it is not this step's business
        self.assertIn('All fixed bills posted', by['recurring']['detail'])

    def test_the_variable_bills_have_their_own_check_after_the_bank_reconciliation(self):
        by = {i['key']: i for i in self.sheet()['items']}
        self.assertEqual(by['variable_bills']['status'], 'attention')
        self.assertIn('Electricity', by['variable_bills']['detail'])
        payables.create_bill(self.prop, date(2026, 10, 24), [(self.variable.account, D('212.40'), 'Electric')], vendor=self.variable.vendor, recurring=self.variable, kind=Bill.Kind.ROUTINE)
        by = {i['key']: i for i in self.sheet()['items']}
        self.assertEqual(by['variable_bills']['status'], 'done')

    def test_without_any_variable_bill_there_is_no_second_check(self):
        self.variable.delete()
        self.assertNotIn('variable_bills', keys(self.sheet()))

    def test_owner_charges_step_checks_the_month_and_says_when_automatic_posting_is_off(self):
        ChargeRule.objects.create(association=self.prop, name='Monthly assessment', frequency='monthly', first_due=date(2026, 10, 1), amount=D('615'), income_account=ledger.account(self.prop, '4500'))
        from .models import Ownership
        for unit in self.prop.units.all():
            person = Contact.objects.create(name=f'Owner {unit.label}', contact_type=Contact.ContactType.OWNER)
            Ownership.objects.create(unit=unit, person=person, start_date=date(2020, 1, 1), is_billed=True)
        profile = ledger.profile_for(self.prop)
        profile.auto_post = False
        profile.save()
        item = {i['key']: i for i in self.sheet()['items']}['charges']
        self.assertEqual(item['status'], 'attention')
        self.assertIn('not posted yet', item['detail'])
        self.assertIn('Automatic posting is off', item['detail'])
        self.assertIn('tab=charges', item['url'])
        charges.post_due(self.prop, date(2026, 10, 1))
        item = {i['key']: i for i in self.sheet()['items']}['charges']
        self.assertEqual(item['status'], 'done')

    def test_the_recurring_journal_entries_step_covers_every_entry_and_replaces_the_reserve_step(self):
        budget_with_reserves(self.prop)
        rule = JournalRule.objects.create(association=self.prop, day_of_month=27)
        by = {i['key']: i for i in self.sheet()['items']}
        self.assertEqual(by['entries']['status'], 'attention')
        self.assertIn('Reserve contribution: not posted yet', by['entries']['detail'])
        self.assertIn('posts itself on its day', by['entries']['detail'])
        rule.auto_post = False
        rule.save()
        self.assertIn('Automatic posting is off for it', {i['key']: i for i in self.sheet()['items']}['entries']['detail'])
        scheduled.post_contribution(self.prop, rule, self.month)
        by = {i['key']: i for i in self.sheet()['items']}
        self.assertEqual(by['entries']['status'], 'done')
        self.assertIn('posted Oct 27', by['entries']['detail'])
        self.assertFalse(any(k.startswith('journal') for k in keys(self.sheet())))

    def test_with_no_recurring_entries_the_step_is_done(self):
        item = {i['key']: i for i in self.sheet()['items']}['entries']
        self.assertEqual((item['status'], item['detail']), ('done', 'No recurring journal entries are set up.'))

    def test_the_close_page_shows_the_new_steps_in_order(self):
        with mock.patch('django.utils.timezone.localdate', return_value=self.today):
            html = self.client.get(reverse('accounting:close', args=[self.prop.pk, '2026-10'])).content.decode()
        positions = [html.index(t) for t in ('Owner charges posted', '>Recurring bills<', 'Recurring journal entries', f'{self.bank.label} bank reconciliation', 'Charge late fees and interest', '>Owner collections<', 'Review and close the month')]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn('Bills waiting', html)


@override_settings(STORAGES=STORAGES)
class AutomaticPostingSwitchTests(TestCase):
    def setUp(self):
        self.prop = make_assoc()
        bank_for(self.prop)
        budget_with_reserves(self.prop)
        self.rule = JournalRule.objects.create(association=self.prop, day_of_month=27)
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.url = reverse('accounting:recurring', args=[self.prop.pk])

    def test_the_association_switch_is_on_the_recurring_screen(self):
        self.assertTrue(ledger.profile_for(self.prop).auto_post)
        html = self.client.get(self.url + '?tab=charges&month=2026-10').content.decode()
        self.assertIn('Automatic posting is <strong>on</strong>', html)
        r = self.client.post(self.url, {'action': 'auto_post', 'auto': 'off', 'tab': 'charges', 'month': '2026-10'}, follow=True)
        self.assertContains(r, 'Automatic posting is off: nothing posts by itself')
        self.assertFalse(ledger.profile_for(self.prop).auto_post)
        self.client.post(self.url, {'action': 'auto_post', 'auto': 'on', 'tab': 'charges', 'month': '2026-10'})
        self.assertTrue(ledger.profile_for(self.prop).auto_post)

    def test_each_recurring_journal_entry_has_its_own_switch_and_the_scheduled_job_respects_it(self):
        html = self.client.get(self.url + '?tab=entries&month=2026-10').content.decode()
        self.assertIn('Automatic posting: <strong>on</strong>', html)
        self.client.post(self.url, {'action': 'jr_auto', 'rule': self.rule.pk, 'auto': 'off', 'tab': 'entries', 'month': '2026-10'})
        self.rule.refresh_from_db()
        self.assertFalse(self.rule.auto_post)
        self.assertEqual(scheduled.post_due(self.prop, date(2026, 10, 28), automatic=True), [])         # the scheduled job leaves it alone
        self.assertTrue(scheduled.post_due(self.prop, date(2026, 10, 28)))                                # a person can still post it
        JournalEntry.objects.filter(association=self.prop, source_ref__startswith='jrule:').delete()
        self.client.post(self.url, {'action': 'jr_auto', 'rule': self.rule.pk, 'auto': 'on', 'tab': 'entries'})
        self.assertTrue(scheduled.post_due(self.prop, date(2026, 10, 28), automatic=True))

    def test_the_recurring_bills_tab_keeps_the_variable_bills_apart(self):
        vendor = Contact.objects.create(name='FPL')
        RecurringBill.objects.create(association=self.prop, vendor=vendor, description='Electricity', account=ledger.account(self.prop, '6410'), amount=None, next_date=date(2026, 10, 24))
        RecurringBill.objects.create(association=self.prop, vendor=vendor, description='Pool service', account=ledger.account(self.prop, '6410'), amount=D('300'), next_date=date(2026, 10, 9))
        html = self.client.get(self.url + '?tab=bills&month=2026-10').content.decode()
        fixed_part, variable_part = html.split('Variable bills: settled during the bank reconciliation')
        self.assertIn('Pool service', fixed_part)
        self.assertNotIn('Electricity', fixed_part)
        self.assertIn('Electricity', variable_part)
        self.assertIn('Waits for the bank reconciliation', variable_part)
        self.assertNotIn('has not posted', html.split('Pool service')[0][-400:])                             # the strip names only the fixed bill that is missing


class CollectionsBase(DelinquencyBase):
    """Late Owner (unit 100) owes the October assessment and has no case; today is Nov 12."""

    def make_letter(self, mailed=None):
        from legalforms import services
        from legalforms.models import DocumentTemplate
        template = DocumentTemplate.objects.get(slug='nola')
        values = services.initial_values(template, self.prop, self.late_unit, self.staff.user)
        doc, errors = services.save_document(template, self.prop, self.late_unit, self.staff.user, {k: '' if v is None else str(v) for k, v in values.items()})
        self.assertFalse(errors)
        if mailed:
            doc.mailed_on = mailed
            doc.save(update_fields=['mailed_on'])
        return doc

    def tab(self, key, today=None):
        board = owner_collections.board(self.prop, today or self.today)
        return next(t for t in board['tabs'] if t['key'] == key)


class TheBoardAndItsAlertsTests(CollectionsBase):
    def test_an_owner_behind_with_no_letter_is_on_the_first_tab_and_needs_a_letter(self):
        tab = self.tab('none')
        self.assertEqual([r['owner'].name for r in tab['rows']], ['Late Owner'])
        self.assertEqual(tab['alerts'], 1)
        self.assertEqual((tab['rows'][0]['months'], tab['rows'][0]['days_late']), (1, 30))      # worked out as of the end of last month: Oct 1 to Oct 31 is 30 days
        self.assertEqual(owner_collections.attention(self.prop, self.today)['bits'], ['1 need a late letter'])

    def test_the_first_letter_moves_the_owner_to_letter_sent(self):
        self.make_letter()
        self.assertEqual(self.tab('none')['count'], 0)
        row = self.tab('letter')['rows'][0]
        self.assertEqual(row['owner'].name, 'Late Owner')
        self.assertEqual(row['case'].stage, 'letter')
        self.assertEqual(row['case'].stage_since, row['case'].opened_on)

    def test_a_letter_mailed_more_than_30_days_ago_can_go_to_the_lawyer_and_exactly_30_cannot(self):
        self.make_letter(mailed=self.today - timedelta(days=30))
        row = self.tab('letter')['rows'][0]
        self.assertEqual((row['alert'], row['days_since_letter']), ('', 30))
        self.assertEqual(self.tab('letter')['alerts'], 0)
        DelinquencyCase.objects.update(next_review=self.today + timedelta(days=20))        # keep the 30-day review out of this
        row = self.tab('letter', self.today + timedelta(days=1))['rows'][0]
        self.assertIn('Letter sent 31 days ago: can go to the lawyer', row['alert'])
        self.assertEqual(self.tab('letter', self.today + timedelta(days=1))['alerts'], 1)
        self.assertEqual(row['move_label'], 'Send to the lawyer')

    def test_a_letter_that_is_still_a_draft_has_not_started_the_30_days(self):
        self.make_letter()
        row = self.tab('letter', self.today + timedelta(days=90))['rows'][0]
        self.assertIsNone(row['sent_on'])
        self.assertIn('still a draft', row['alert'])

    def test_a_letter_noted_by_hand_counts_from_its_own_date(self):
        self.make_letter()
        case = DelinquencyCase.objects.get()
        delinquency.add_event(case, CaseEvent.Kind.LETTER, 'Sent by certified mail', on=self.today - timedelta(days=40))
        row = self.tab('letter')['rows'][0]
        self.assertEqual(row['sent_on'], self.today - timedelta(days=40))
        self.assertIn('40 days ago', row['alert'])

    def test_each_lawyer_notice_alerts_45_days_after_the_owner_entered_it(self):
        self.make_letter(mailed=self.today)
        case = DelinquencyCase.objects.get()
        for stage in ('notice1', 'notice2'):
            delinquency.set_stage(case, stage, self.staff.user)
            case.refresh_from_db()
            self.assertEqual(case.stage_since, timezone.localdate())                                    # entered today (the real day: set_stage stamps it)
            case.stage_since = self.today - timedelta(days=44)
            case.save()
            row = self.tab(stage)['rows'][0]
            self.assertEqual((row['days_in'], row['alert']), (44, ''))
            case.stage_since = self.today - timedelta(days=45)
            case.save()
            row = self.tab(stage)['rows'][0]
            self.assertIn('45 days in this step: time to move on', row['alert'])
            self.assertEqual(self.tab(stage)['alerts'], 1)

    def test_lien_or_foreclosure_has_no_move_on_alert_and_no_next_step(self):
        self.make_letter(mailed=self.today)
        case = DelinquencyCase.objects.get()
        delinquency.set_stage(case, 'lien', self.staff.user)
        case.stage_since = self.today - timedelta(days=200)
        case.save()
        row = self.tab('lien')['rows'][0]
        self.assertEqual((row['alert'], row['can_move']), ('', False))

    def test_a_case_whose_balance_is_paid_is_flagged_to_resolve(self):
        self.make_letter(mailed=self.today)
        charges.receive_payment(self.prop, self.late_unit, self.owner, self.today, D('615'), bank_account=self.bank)
        row = self.tab('letter')['rows'][0]
        self.assertTrue(row['paid'])
        self.assertIn('1 paid in full: resolve', owner_collections.attention(self.prop, self.today)['bits'])

    def test_resolved_cases_show_on_the_last_tab(self):
        self.make_letter()
        delinquency.resolve(DelinquencyCase.objects.get(), self.staff.user, 'Paid')
        self.assertEqual(self.tab('resolved')['count'], 1)


class MovingAnOwnerOnTests(CollectionsBase):
    def test_advance_walks_letter_notice_1_notice_2_lien_and_records_who_and_when(self):
        self.make_letter(mailed=self.today)
        case = DelinquencyCase.objects.get()
        before = case.next_review
        stages = [delinquency.advance(case, self.staff.user, note='Per the lawyer', attorney='Smith & Jones') for _ in range(3)]
        self.assertEqual([str(s) for s in stages], ['notice1', 'notice2', 'lien'])
        case.refresh_from_db()
        self.assertEqual((case.stage, case.attorney_name, case.stage_since), ('lien', 'Smith & Jones', timezone.localdate()))
        moves = list(case.events.filter(kind='attorney').order_by('pk'))
        self.assertEqual(len(moves), 3)
        self.assertIn('Lawyer: notice 1', moves[0].summary)
        self.assertIn('Per the lawyer', moves[0].summary)
        self.assertEqual(moves[0].by, self.staff.user)
        self.assertGreaterEqual(case.next_review, before)                                         # moving on counts as a review: it comes back in 30 days
        with self.assertRaises(ValueError):
            delinquency.advance(case, self.staff.user)

    def test_there_is_no_payment_plan_step(self):
        self.assertNotIn('plan', DelinquencyCase.Stage.values)
        self.assertEqual(DelinquencyCase.Stage.values, ['letter', 'notice1', 'notice2', 'lien', 'resolved'])
        self.make_letter()
        with self.assertRaises(ValueError):
            delinquency.set_stage(DelinquencyCase.objects.get(), 'plan')


@override_settings(STORAGES=STORAGES)
class TheOwnerCollectionsScreenTests(CollectionsBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.url = reverse('accounting:collections', args=[self.prop.pk])

    def get(self, url, **query):
        with mock.patch('django.utils.timezone.localdate', return_value=self.today):
            return self.client.get(url, query)

    def test_it_opens_on_the_first_step_that_needs_something_and_shows_the_tab_counts(self):
        html = self.get(self.url).content.decode()
        for label in ('Not in a case', 'Letter sent', 'Lawyer: notice 1', 'Lawyer: notice 2', 'Lien or foreclosure', 'Resolved'):
            self.assertIn(label, html)
        self.assertIn('Late Owner', html)
        self.assertIn('Write the late letter', html)
        self.assertIn(f'?tab=none', html)

    def test_the_letter_tab_highlights_who_can_go_to_the_lawyer(self):
        self.make_letter(mailed=self.today - timedelta(days=35))
        DelinquencyCase.objects.update(next_review=self.today + timedelta(days=20))
        html = self.get(self.url, tab='letter').content.decode()
        self.assertIn('Letter sent 35 days ago: can go to the lawyer', html)
        self.assertIn('Send to the lawyer', html)
        self.assertIn('oc-alert', html)

    def test_sending_an_owner_to_the_lawyer_from_the_screen(self):
        self.make_letter(mailed=self.today - timedelta(days=35))
        case = DelinquencyCase.objects.get()
        r = self.client.post(self.url, {'action': 'advance', 'case': case.pk, 'tab': 'letter', 'attorney': 'Smith & Jones', 'note': 'Sent the file'}, follow=True)
        self.assertContains(r, 'now at &quot;Lawyer: notice 1')
        case.refresh_from_db()
        self.assertEqual((case.stage, case.attorney_name), ('notice1', 'Smith & Jones'))
        self.assertEqual(r.redirect_chain[-1][0], self.url + '?tab=notice1')

    def test_a_case_of_another_association_cannot_be_moved(self):
        other = make_assoc('Other Condos')
        self.make_letter()
        case = DelinquencyCase.objects.get()
        r = self.client.post(reverse('accounting:collections', args=[other.pk]), {'action': 'advance', 'case': case.pk})
        self.assertEqual(r.status_code, 404)

    def test_making_late_letters_for_the_ticked_owners_opens_their_cases(self):
        r = self.client.post(self.url, {'action': 'make_letters', 'unit': [self.late_unit.pk], 'tab': 'none'}, follow=True)
        self.assertContains(r, 'Made 1 late letter')
        self.assertEqual(DelinquencyCase.objects.get().stage, 'letter')
        self.assertTrue(r.redirect_chain[-1][0].endswith('?tab=letter'))

    def test_the_case_page_goes_back_to_owner_collections_and_offers_only_the_new_steps(self):
        self.make_letter()
        case = DelinquencyCase.objects.get()
        page = self.client.get(reverse('accounting:case', args=[self.prop.pk, case.pk]))
        self.assertContains(page, 'Owner collections')
        self.assertContains(page, 'Lawyer: notice 2')
        self.assertNotContains(page, 'Payment plan')

    def test_the_list_of_associations_counts_who_needs_attention(self):
        with mock.patch('django.utils.timezone.localdate', return_value=self.today):
            r = self.client.get(reverse('accounting:collections_home'))
        self.assertContains(r, 'Test Condos')
        self.assertContains(r, '1 needs attention')
        self.assertContains(r, '1 need a late letter')

    def test_the_card_is_on_the_accounting_screen_and_under_financials_associations(self):
        hub = self.client.get(reverse('accounting:hub', args=[self.prop.pk])).content.decode()
        self.assertIn(self.url, hub)
        self.assertIn(reverse('accounting:late_fees', args=[self.prop.pk]), hub)
        self.assertIn('Post late fees', hub)
        fin = self.client.get(reverse('ws_financials')).content.decode()
        self.assertIn(reverse('accounting:collections_home'), fin)
        self.assertIn('Owner collections', fin)


@override_settings(STORAGES=STORAGES)
class PostLateFeesTests(CollectionsBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.url = reverse('accounting:late_fees', args=[self.prop.pk])

    def test_it_lists_what_is_ready_and_nothing_is_charged_until_a_button_is_pressed(self):
        with mock.patch('django.utils.timezone.localdate', return_value=self.today):
            html = self.client.get(self.url).content.decode()
        self.assertIn('Late Owner', html)
        self.assertIn('$30.75', html)
        self.assertIn('Charge late fees', html)
        self.assertIn('Next: Owner collections', html)
        self.assertFalse(Charge.objects.filter(kind='late_fee').exists())

    def test_charging_the_late_fee_logs_it_on_the_owners_case(self):
        self.make_letter()
        with mock.patch('django.utils.timezone.localdate', return_value=self.today):
            r = self.client.post(self.url, {'action': 'charge_late', 'unit': [self.late_unit.pk]}, follow=True)
        self.assertContains(r, 'Charged 1 late fee')
        self.assertEqual(Charge.objects.get(kind='late_fee').amount, D('30.75'))
        self.assertTrue(DelinquencyCase.objects.get().events.filter(kind='fee').exists())

    def test_with_nobody_ticked_everyone_listed_is_charged(self):
        with mock.patch('django.utils.timezone.localdate', return_value=self.today):
            r = self.client.post(self.url, {'action': 'charge_late'}, follow=True)
        self.assertContains(r, 'Charged 1 late fee')

    def test_when_the_rules_are_not_set_up_it_says_so(self):
        profile = ledger.profile_for(self.prop)
        profile.late_fee_confirmed = profile.interest_confirmed = False
        profile.save()
        self.assertContains(self.client.get(self.url), 'not set up yet')


class TheOldStepsMapToTheNewOnesTests(CollectionsBase):
    """The data migration (accounting 0016): final notice -> letter sent, with the attorney -> lawyer notice 1, payment plan -> letter sent; the day they entered the step is the day of their last move."""

    def test_the_mapping_and_the_stage_since_date(self):
        migration = importlib.import_module('accounting.migrations.0016_collections_stages')
        made = {}
        for old, label in (('final', 'Final notice'), ('attorney', 'With the attorney'), ('plan', 'Payment plan'), ('letter', 'Letter sent'), ('resolved', 'Resolved')):
            from core.models import Unit
            unit = Unit.objects.create(property=self.prop, label=f'M-{old}')
            owner = Contact.objects.create(name=f'Owner {old}')
            case = DelinquencyCase.objects.create(association=self.prop, unit=unit, owner=owner, stage=old, opened_on=date(2026, 9, 1))
            made[old] = case
        CaseEvent.objects.create(case=made['attorney'], kind='attorney', date=date(2026, 10, 5), summary='Now: With the attorney.')
        CaseEvent.objects.create(case=made['attorney'], kind='note', date=date(2026, 10, 20), summary='A note is not a move.')
        CaseEvent.objects.create(case=made['plan'], kind='plan', date=date(2026, 10, 12), summary='Now: Payment plan.')
        migration.forwards(django_apps, None)
        for case in made.values():
            case.refresh_from_db()
        self.assertEqual({k: c.stage for k, c in made.items()}, {'final': 'letter', 'attorney': 'notice1', 'plan': 'letter', 'letter': 'letter', 'resolved': 'resolved'})
        self.assertEqual(made['attorney'].stage_since, date(2026, 10, 5))
        self.assertEqual(made['plan'].stage_since, date(2026, 10, 12))
        self.assertEqual(made['letter'].stage_since, date(2026, 9, 1))                          # no move since it opened: the day it opened
        migration.backwards(django_apps, None)
        made['attorney'].refresh_from_db()
        self.assertEqual(made['attorney'].stage, 'attorney')


@override_settings(STORAGES=STORAGES)
class TheAsOfDateAndTheAmountOnTheScreenTests(CollectionsBase):
    """Who was late as of the end of last month (not today: this month's payments are not entered yet), and the amount that puts an owner in scope, set on the screen itself."""

    def setUp(self):
        super().setUp()
        prof = ledger.profile_for(self.prop)
        prof.delinquency_threshold = D('100')
        prof.save()
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.url = reverse('accounting:collections', args=[self.prop.pk])
        self.oct31, self.nov5 = date(2026, 10, 31), date(2026, 11, 5)

    def get(self, **query):
        with mock.patch('django.utils.timezone.localdate', return_value=self.today):
            return self.client.get(self.url, query)

    def none_rows(self, asof=None, today=None):
        board = owner_collections.board(self.prop, today or self.today, asof)
        return next(t for t in board['tabs'] if t['key'] == 'none')['rows']

    def test_the_date_is_the_end_of_last_month_unless_another_is_chosen(self):
        self.assertEqual(owner_collections.default_asof(date(2026, 11, 12)), self.oct31)
        self.assertEqual(owner_collections.default_asof(date(2027, 1, 5)), date(2026, 12, 31))
        self.assertEqual(owner_collections.board(self.prop, self.today)['asof'], self.oct31)
        self.assertEqual(owner_collections.board(self.prop, self.today, self.today)['asof'], self.today)
        self.assertEqual(owner_collections.board(self.prop, self.today, date(2027, 1, 1))['asof'], self.today)       # a date in the future means today

    def test_a_payment_received_after_the_date_does_not_count_for_that_date(self):
        charges.receive_payment(self.prop, self.late_unit, self.owner, self.nov5, D('615'), bank_account=self.bank)         # Late Owner pays on Nov 5
        rows = self.none_rows()                                                                                             # as of Oct 31: still behind
        self.assertEqual([r['owner'].name for r in rows], ['Late Owner'])
        self.assertEqual(rows[0]['b']['total'], D('615.00'))
        self.assertEqual(self.none_rows(asof=self.today), [])                                                               # as of today: paid

    def test_a_charge_that_fell_due_after_the_date_is_not_counted(self):
        charges.post_charge(self.prop, self.ok_unit, self.good, Charge.Kind.ASSESSMENT, 'Monthly assessment Nov', date(2026, 11, 1), D('615'))     # Good Owner has not paid November
        charges.post_charge(self.prop, self.late_unit, self.owner, Charge.Kind.ASSESSMENT, 'Monthly assessment Nov', date(2026, 11, 1), D('615'))
        self.assertEqual([(r['owner'].name, r['b']['total']) for r in self.none_rows()], [('Late Owner', D('615.00'))])         # as of Oct 31: only October's
        today_rows = {r['owner'].name: r['b']['total'] for r in self.none_rows(asof=self.today)}
        self.assertEqual(today_rows, {'Late Owner': D('1230.00'), 'Good Owner': D('615.00')})

    def test_how_late_is_counted_to_the_date_not_to_today(self):
        first, second = self.none_rows()[0], self.none_rows(asof=self.today)[0]
        self.assertEqual((first['days_late'], first['months']), (30, 1))
        self.assertEqual((second['days_late'], second['months']), (42, 1))

    def test_the_amount_decides_who_is_listed(self):
        prof = ledger.profile_for(self.prop)
        prof.delinquency_threshold = D('700')
        prof.save()
        self.assertEqual(self.none_rows(), [])
        prof.delinquency_threshold = D('600')
        prof.save()
        self.assertEqual([r['owner'].name for r in self.none_rows()], ['Late Owner'])

    def test_the_screen_says_which_date_and_which_amount_the_first_tab_is_for(self):
        html = self.get().content.decode()
        self.assertIn('Owners who owed more than $100 on Oct 31, 2026 and have no late letter yet', html)
        self.assertIn('Behind as of', html)
        self.assertIn('End of last month', html)
        self.assertIn('name="amount"', html)
        self.assertNotIn('Change the amount', html)                                      # no more trip to the Delinquencies screen
        self.assertNotIn(reverse('accounting:delinquencies', args=[self.prop.pk]), html)
        self.assertIn('Owed<span class="d-none d-md-inline"> Oct 31</span>', html)

    def test_a_chosen_date_is_kept_in_the_links_between_the_tabs_and_the_late_letter_link(self):
        html = self.get(asof='2026-11-05', tab='none').content.decode()
        self.assertIn('tab=letter&amp;asof=2026-11-05', html)
        self.assertIn('tab=resolved&amp;asof=2026-11-05', html)
        self.assertIn('balance_as_of=2026-11-05', html)
        self.assertIn('Owners who owed more than $100 on Nov 5, 2026', html)
        plain = self.get().content.decode()
        self.assertIn('balance_as_of=2026-10-31', plain)                                 # the default date goes to the letter too
        self.assertNotIn('asof=', plain.split('class="oc-tabs')[1].split('</div>')[0])   # and the plain address stays plain

    def test_a_date_in_the_future_is_taken_as_today(self):
        html = self.get(asof='2027-01-01').content.decode()
        self.assertIn('value="2026-11-12"', html)

    def test_saving_the_amount_stays_on_owner_collections_and_keeps_the_tab_and_date(self):
        r = self.client.post(self.url, {'action': 'threshold', 'amount': '$700.50', 'tab': 'none', 'asof': '2026-11-05'}, follow=True)
        self.assertEqual(ledger.profile_for(self.prop).delinquency_threshold, D('700.50'))
        self.assertEqual(r.redirect_chain[-1][0], self.url + '?tab=none&asof=2026-11-05')
        self.assertNotIn('delinquenc', r.redirect_chain[-1][0])
        self.assertContains(r, 'Saved: owners are listed once they owe more than $700.50.')
        r = self.client.post(self.url, {'action': 'threshold', 'amount': '650', 'tab': 'letter'})
        self.assertEqual(r['Location'], self.url + '?tab=letter')                       # no date chosen: none added
        self.assertEqual(ledger.profile_for(self.prop).delinquency_threshold, D('650.00'))

    def test_a_bad_amount_is_refused_and_nothing_changes(self):
        for bad in ('abc', '-5', ''):
            r = self.client.post(self.url, {'action': 'threshold', 'amount': bad, 'tab': 'none'}, follow=True)
            self.assertEqual(ledger.profile_for(self.prop).delinquency_threshold, D('100.00'), bad)
            self.assertTrue(any(m.level_tag == 'error' for m in r.context['messages']), bad)

    def test_the_letters_made_from_the_screen_are_for_the_date_chosen(self):
        from legalforms.models import GeneratedDocument
        self.client.post(self.url, {'action': 'make_letters', 'unit': [self.late_unit.pk], 'tab': 'none', 'asof': '2026-10-20'})
        doc = GeneratedDocument.objects.get()
        self.assertIn('2026-10-20', str(doc.values.get('balance_as_of')))

    def test_the_close_step_counts_who_was_behind_at_the_end_of_last_month_like_the_screen(self):
        charges.receive_payment(self.prop, self.late_unit, self.owner, self.nov5, D('615'), bank_account=self.bank)         # paid in November, after the date
        step = next(i for i in closing.close_sheet(self.prop, date(2026, 10, 1), today=self.today)['items'] if i['key'] == 'collections')
        self.assertIn('1 need a late letter', step['detail'])
        self.assertEqual(owner_collections.attention(self.prop, self.today)['count'], len(self.none_rows()))
        self.assertEqual(owner_collections.attention(self.prop, self.today, asof=self.today)['count'], 0)
