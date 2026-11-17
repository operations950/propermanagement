"""The Post late fees and interest screen judged as of a day: who was late that day, the owes-at-least filter, the day the charges are posted on, and the closed month."""
from datetime import date
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import Unit

from . import charges, closing, ledger
from .models import Charge
from .tests import D, bank_for, make_assoc, owner_of
from .views_collections import late_fee_data

STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}
OCT1, OCT31 = date(2026, 10, 1), date(2026, 10, 31)


class AsOfBase(TestCase):
    """Four owners, all billed 615 due October 1 (a 10-day grace period, a late fee of the greater of $25 or 5%, 18% interest):
    Anna pays nothing; Ben pays on October 20; Cora pays on November 5 (after October 31); Dan owes only $15."""
    def setUp(self):
        self.prop = make_assoc()
        self.bank = bank_for(self.prop)
        prof = ledger.profile_for(self.prop)
        prof.late_fee_kind, prof.late_fee_flat, prof.late_fee_percent, prof.late_fee_grace_days, prof.late_fee_confirmed = 'greater', D('25'), D('5'), 10, True
        prof.interest_rate, prof.interest_confirmed = D('18'), True
        prof.save()
        units = list(Unit.objects.filter(property=self.prop).order_by('label'))
        self.anna_u, self.ben_u, self.cora_u, self.dan_u = units[:4]
        self.anna, self.ben, self.cora, self.dan = (owner_of(self.prop, u, n) for u, n in zip(units[:4], ('Anna', 'Ben', 'Cora', 'Dan')))
        for u, o in ((self.anna_u, self.anna), (self.ben_u, self.ben), (self.cora_u, self.cora)):
            charges.post_charge(self.prop, u, o, Charge.Kind.ASSESSMENT, 'Monthly assessment', OCT1, D('615'))
        charges.post_charge(self.prop, self.dan_u, self.dan, Charge.Kind.ASSESSMENT, 'Monthly assessment', OCT1, D('15'))
        charges.receive_payment(self.prop, self.ben_u, self.ben, date(2026, 10, 20), D('615'), bank_account=self.bank)
        charges.receive_payment(self.prop, self.cora_u, self.cora, date(2026, 11, 5), D('615'), bank_account=self.bank)

    def names(self, rows):
        return sorted(r['owner'].name for r in rows)


class WhoIsLateOnTheDayTests(AsOfBase):
    def test_someone_who_paid_before_the_day_is_not_late_and_someone_who_paid_after_it_is(self):
        rows = late_fee_data(self.prop, OCT31)
        self.assertEqual(self.names(rows), ['Anna', 'Cora', 'Dan'])               # Ben paid on the 20th; Cora paid only on November 5

    def test_the_same_owner_is_late_on_an_earlier_day(self):
        self.assertEqual(self.names(late_fee_data(self.prop, date(2026, 10, 15))), ['Anna', 'Ben', 'Cora', 'Dan'])      # Ben had not paid yet on the 15th

    def test_the_grace_period_counts_to_the_day_chosen(self):
        self.assertEqual(late_fee_data(self.prop, date(2026, 10, 11)), [])               # due the 1st, 10 days of grace: not late until after the 11th
        self.assertEqual(len(late_fee_data(self.prop, date(2026, 10, 12))), 4)
        self.assertEqual(charges._late_fee_rows(self.prop, date(2026, 10, 31), as_of=date(2026, 10, 11)), [])

    def test_the_old_way_still_judges_by_today(self):
        rows = charges._late_fee_rows(self.prop, date(2026, 11, 12))
        self.assertEqual(sorted(c.owner.name for c, fee, due in rows), ['Anna', 'Dan'])      # Cora has paid by now, so today's reading does not charge her

    def test_the_row_shows_what_was_owed_that_day(self):
        row = {r['owner'].name: r for r in late_fee_data(self.prop, OCT31)}['Cora']
        self.assertEqual((row['total'], row['principal'], row['late_fees'], row['interest']), (D('615.00'), D('615.00'), D('0.00'), D('0.00')))
        self.assertEqual(row['fee_ready'], D('30.75'))
        self.assertEqual(row['interest_ready'], D('9.10'))                       # October, charged for the month that ended on the day
        self.assertEqual(len(row['lines']), 1)

    def test_interest_is_only_for_a_charge_unpaid_on_the_day(self):
        rows = charges._interest_rows(self.prop, OCT31, as_of=OCT31)
        self.assertEqual(sorted(c.owner.name for c, mf, me, amt in rows), ['Anna', 'Cora', 'Dan'])      # Ben paid on the 20th: not late on the 31st
        self.assertEqual({c.owner.name: amt for c, mf, me, amt in rows}['Anna'], D('9.10'))

    def test_a_month_that_has_not_ended_has_no_interest_yet(self):
        self.assertEqual(charges._interest_rows(self.prop, date(2026, 10, 20), as_of=date(2026, 10, 20)), [])


class OwesAtLeastTests(AsOfBase):
    def test_owners_below_the_amount_are_not_listed(self):
        self.assertEqual(self.names(late_fee_data(self.prop, OCT31, min_balance=D('100'))), ['Anna', 'Cora'])
        self.assertEqual(self.names(late_fee_data(self.prop, OCT31, min_balance=D('15'))), ['Anna', 'Cora', 'Dan'])

    def test_total_owed_adds_late_fees_and_interest_already_charged(self):
        charges.assess_late_fees(self.prop, OCT31, units=[self.anna_u.pk], as_of=OCT31, post_day=OCT31)
        row = {r['owner'].name: r for r in late_fee_data(self.prop, OCT31, show_all=True)}['Anna']
        self.assertEqual((row['principal'], row['late_fees'], row['total']), (D('615.00'), D('30.75'), D('645.75')))
        self.assertEqual(row['last_charged'], OCT31)
        self.assertEqual(row['fee_ready'], D('0.00'))


@override_settings(STORAGES=STORAGES)
class PostingTests(AsOfBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.url = reverse('accounting:late_fees', args=[self.prop.pk])
        self.today = date(2026, 11, 12)

    def post(self, data):
        with mock.patch('django.utils.timezone.localdate', return_value=self.today):
            return self.client.post(self.url, data, follow=True)

    def owners_charged(self, kind='late_fee'):
        return sorted(Charge.objects.filter(kind=kind).values_list('owner__name', flat=True))

    def test_the_screen_opens_as_of_the_day_in_the_address_and_lists_who_was_late(self):
        with mock.patch('django.utils.timezone.localdate', return_value=self.today):
            r = self.client.get(self.url + '?as_of=2026-10-31')
        html = r.content.decode()
        self.assertEqual(self.names(r.context['rows']), ['Anna', 'Cora', 'Dan'])
        self.assertNotIn('What for', html)
        self.assertIn('Total owed', html)
        self.assertIn('Late fee last charged', html)
        self.assertIn('Interest last charged', html)

    def test_a_day_in_the_future_or_not_a_date_means_today(self):
        with mock.patch('django.utils.timezone.localdate', return_value=self.today):
            future = self.client.get(self.url + '?as_of=2027-01-01').content.decode()
            bad = self.client.get(self.url + '?as_of=soon').content.decode()
        self.assertIn('has not come yet', future)
        self.assertIn('2026-11-12', future)
        self.assertIn('not a date', bad)

    def test_charging_with_nobody_ticked_charges_everyone_listed_as_of_the_day_and_dates_it_that_day(self):
        r = self.post({'action': 'charge_late', 'as_of': '2026-10-31', 'post_on': '2026-10-31'})
        self.assertContains(r, 'Charged 3 late fees')
        self.assertEqual(self.owners_charged(), ['Anna', 'Cora', 'Dan'])
        self.assertEqual({c.due_date for c in Charge.objects.filter(kind='late_fee')}, {OCT31})
        self.assertEqual({c.entry.date for c in Charge.objects.filter(kind='late_fee')}, {OCT31})

    def test_post_on_can_be_another_day(self):
        self.post({'action': 'charge_late', 'as_of': '2026-10-31', 'post_on': '2026-11-02'})
        self.assertEqual({c.due_date for c in Charge.objects.filter(kind='late_fee')}, {date(2026, 11, 2)})

    def test_owners_under_the_minimum_are_never_charged_even_when_ticked(self):
        self.post({'action': 'charge_late', 'as_of': '2026-10-31', 'post_on': '2026-10-31', 'min': '100', 'unit': [self.dan_u.pk]})
        self.assertEqual(self.owners_charged(), [])                                      # Dan is hidden by the filter, so a ticked Dan alone leaves nobody listed
        r = self.post({'action': 'charge_late', 'as_of': '2026-10-31', 'post_on': '2026-10-31', 'min': '100'})
        self.assertContains(r, 'Charged 2 late fees')
        self.assertEqual(self.owners_charged(), ['Anna', 'Cora'])

    def test_only_the_ticked_owners_are_charged(self):
        self.post({'action': 'charge_late', 'as_of': '2026-10-31', 'post_on': '2026-10-31', 'unit': [self.anna_u.pk]})
        self.assertEqual(self.owners_charged(), ['Anna'])

    def test_an_owner_who_is_not_late_on_the_day_is_never_charged_even_if_ticked(self):
        self.post({'action': 'charge_late', 'as_of': '2026-10-31', 'post_on': '2026-10-31', 'unit': [self.ben_u.pk]})
        self.assertEqual(self.owners_charged(), [])

    def test_a_closed_month_says_so_and_charges_nothing(self):
        ledger.lock_month(self.prop, OCT1)
        r = self.post({'action': 'charge_late', 'as_of': '2026-10-31', 'post_on': '2026-10-31'})
        self.assertContains(r, 'October 2026 is closed')
        self.assertFalse(Charge.objects.filter(kind='late_fee').exists())
        r = self.post({'action': 'charge_late', 'as_of': '2026-10-31', 'post_on': '2026-11-02'})            # a day in the open month is fine
        self.assertContains(r, 'Charged 3 late fees')

    def test_interest_is_charged_as_of_the_day_and_not_twice(self):
        r = self.post({'action': 'charge_interest', 'as_of': '2026-10-31', 'post_on': '2026-10-31'})
        self.assertContains(r, 'Charged 3 interest charges')
        self.assertEqual(self.owners_charged('interest'), ['Anna', 'Cora', 'Dan'])
        r = self.post({'action': 'charge_interest', 'as_of': '2026-10-31', 'post_on': '2026-10-31'})
        self.assertContains(r, 'No interest charges were owed')

    def test_months_of_interest_posted_on_one_day_are_not_charged_again(self):
        old = charges.post_charge(self.prop, self.anna_u, self.anna, Charge.Kind.ASSESSMENT, 'Special assessment', date(2026, 8, 1), D('1000'))
        made = charges.assess_interest(self.prop, OCT31, units=[self.anna_u.pk], as_of=OCT31, post_day=OCT31)
        self.assertEqual(len([c for c in made if c.parent_id == old.pk]), 3)                     # August, September and October, all dated the day chosen
        self.assertEqual({c.due_date for c in made}, {OCT31})
        self.assertEqual(charges.assess_interest(self.prop, OCT31, units=[self.anna_u.pk], as_of=OCT31, post_day=OCT31), [])

    def test_owners_charged_stay_on_the_list_with_nothing_ready_and_the_checkbox_adds_owners_who_are_not_late(self):
        self.post({'action': 'charge_late', 'as_of': '2026-10-31', 'post_on': '2026-10-31'})
        self.post({'action': 'charge_interest', 'as_of': '2026-10-31', 'post_on': '2026-10-31'})
        rows = late_fee_data(self.prop, OCT31)
        self.assertEqual(self.names(rows), ['Anna', 'Cora', 'Dan'])                    # charged, and still listed
        self.assertTrue(all(r['fee_ready'] == 0 and r['interest_ready'] == 0 and not r['ready'] and r['charged_text'] for r in rows))
        self.assertEqual(self.names(late_fee_data(self.prop, OCT31, show_all=True)), ['Anna', 'Cora', 'Dan'])      # Ben paid before the 31st: he owes nothing, so there is nobody more to add


@override_settings(STORAGES=STORAGES)
class TheCloseSheetOpensItAsOfTheMonthEndTests(AsOfBase):
    def test_the_step_counts_who_was_late_on_the_last_day_and_links_with_that_day(self):
        sheet = closing.close_sheet(self.prop, OCT1, today=date(2026, 11, 12))
        step = {i['key']: i for i in sheet['items']}['late_fees']
        self.assertEqual(step['url'], reverse('accounting:late_fees', args=[self.prop.pk]) + '?as_of=2026-10-31&month=2026-10')
        self.assertIn('3 owners with late fees or interest ready to charge as of Oct 31', step['detail'])
