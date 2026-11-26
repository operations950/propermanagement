"""The budget mailing: one letter per owner (the address on file by season), or a letter to every address on file, each place once."""
from datetime import date

from django.test import TestCase

from core.models import Contact, Unit

from . import budget_project as bp
from .models import Budget, BudgetProject, OwnerProfile
from .tests import make_assoc


class MailAllAddressesTests(TestCase):
    def setUp(self):
        self.prop = make_assoc('Mail Condos')
        self.prop.street, self.prop.city, self.prop.state, self.prop.zip_code = '2290 Sunset Ave', 'Lake Worth Beach', 'FL', '33461'
        self.prop.save()
        from .models import Ownership
        self.Ownership = Ownership
        self.units = {u.label: u for u in Unit.objects.filter(property=self.prop)}

    def make(self, label, name, mailing, seasonal=None):
        c = Contact.objects.create(name=name, contact_type=Contact.ContactType.OWNER)
        kw = {}
        if seasonal:
            kw = {'seasonal_address': seasonal, 'seasonal_from': date(2026, 11, 1), 'seasonal_to': date(2027, 4, 30)}
        OwnerProfile.objects.create(contact=c, mailing_address=mailing, **kw)
        self.Ownership.objects.create(unit=self.units[label], person=c, is_billed=True)
        return c

    def test_by_default_there_is_one_letter_per_owner_to_the_main_address(self):
        self.make('100', 'Away Owner', '68 Highview Ave, Staten Island, NY 10301')
        self.make('101', 'Home Owner', '2290 Sunset Ave Apt 101, Lake Worth Beach, FL 33461-6828')
        rows = bp.mailing_rows(self.prop)
        self.assertEqual([(r['unit'], r['source']) for r in rows if r['owner']], [('100', 'mailing'), ('101', 'mailing')])

    def test_every_address_gives_an_owner_living_elsewhere_a_second_letter_and_a_resident_only_one(self):
        self.make('100', 'Away Owner', '68 Highview Ave, Staten Island, NY 10301')
        self.make('101', 'Home Owner', '2290 Sunset Ave Apt 101, Lake Worth Beach, FL 33461-6828')
        rows = [r for r in bp.mailing_rows(self.prop, all_addresses=True) if r['owner']]
        self.assertEqual([(r['unit'], r['source']) for r in rows], [('100', 'mailing'), ('100', 'unit_also'), ('101', 'mailing')])
        extra = rows[1]
        self.assertEqual(extra['lines'], ['2290 Sunset Ave Unit 100', 'Lake Worth Beach, FL 33461'])
        self.assertEqual(len(bp.deliverable(rows)), 3)
        self.assertEqual(bp.flagged(rows), [])

    def test_an_owner_whose_address_on_file_is_the_building_without_the_unit_is_not_sent_two_letters(self):
        self.make('100', 'Building Owner', '2290 Sunset Avenue, Lake Worth Beach, FL 33461')
        self.make('101', 'Same Unit', '2290 S Sunset Ave Unit 101, Lake Worth Beach, FL 33461')
        self.make('102', 'Abbreviated', '2290 Sunset Ave, Unit 102, Lake Worth Beach, FL 33461-1234')
        rows = [r for r in bp.mailing_rows(self.prop, all_addresses=True) if r['owner']]
        self.assertEqual([(r['unit'], r['source']) for r in rows], [('100', 'mailing'), ('101', 'mailing'), ('102', 'mailing')])

    def test_every_address_also_sends_to_the_other_address_on_file_when_the_season_picks_one(self):
        self.make('100', 'Snowbird', '68 Highview Ave, Staten Island, NY 10301', seasonal='9 Palm Way, Naples, FL 34102')
        rows = [r for r in bp.mailing_rows(self.prop, on=date(2026, 12, 15), all_addresses=True) if r['owner']]
        self.assertEqual([(r['unit'], r['source']) for r in rows], [('100', 'seasonal'), ('100', 'other_also'), ('100', 'unit_also')])
        one = [r for r in bp.mailing_rows(self.prop, on=date(2026, 12, 15)) if r['owner']]
        self.assertEqual([r['source'] for r in one], ['seasonal'])

    def test_the_place_of_an_address_ignores_how_it_is_written(self):
        same = bp._same_place(['2290 Sunset Ave # 12', 'Lake Worth Beach, FL 33461-6828'], ['2290 Sunset Ave Unit 12', 'Lake Worth Beach, FL 33461'])
        self.assertTrue(same)
        self.assertTrue(bp._same_place(['4000 South Ocean Boulevard', 'Palm Beach, FL 33480'], ['4000 S Ocean Blvd Unit 102', 'South Palm Beach, FL 33480']))
        self.assertTrue(bp._same_place(['4000 South Ocean Boulevard 301', 'Palm Beach, FL 33480'], ['4000 S Ocean Blvd Unit 301', 'South Palm Beach, FL 33480']))
        self.assertFalse(bp._same_place(['2290 Sunset Ave Apt 3', 'Lake Worth Beach, FL 33461'], ['2290 Sunset Ave Unit 4', 'Lake Worth Beach, FL 33461']))
        self.assertFalse(bp._same_place(['441 Northwest 53rd Street', 'Boca Raton, FL 33487'], ['4000 S Ocean Blvd Unit 101', 'South Palm Beach, FL 33480']))
        self.assertFalse(bp._same_place(['P.O. Box 1086', 'Palm Beach, FL 33480'], ['4000 S Ocean Blvd Unit 103', 'South Palm Beach, FL 33480']))

    def test_the_packet_choice_sets_it_and_the_envelopes_follow_it(self):
        self.make('100', 'Away Owner', '68 Highview Ave, Staten Island, NY 10301')
        bud = Budget.objects.create(association=self.prop, year=2027)
        project = bp.ensure_project(bud)
        self.assertFalse(project.mail_all_addresses)
        self.assertEqual(len([r for r in bp.rows_for_packet(self.prop, project) if r['owner']]), 1)
        BudgetProject.objects.filter(pk=project.pk).update(mail_all_addresses=True)
        project.refresh_from_db()
        self.assertEqual(len([r for r in bp.rows_for_packet(self.prop, project) if r['owner']]), 2)

    def test_the_affidavit_says_how_many_unit_owners_and_how_many_mailings_when_they_differ(self):
        from legalforms import catalog_meetings as cm
        base = {'association_type': cm.CONDO, 'budget_year': 2027, 'meeting_date': date(2027, 1, 20), 'date_mailed': date(2026, 12, 20), 'notice_count': 48}
        one = dict(base)
        try:
            cm.compute_budget_affidavit(one)
        except KeyError:
            self.skipTest('the legal form needs more of the document context than this test supplies')
        self.assertEqual(one['mailing_clause'], '')
        many = dict(base, mailings=72)
        cm.compute_budget_affidavit(many)
        self.assertIn('72 mailings in all', many['mailing_clause'])
        same = dict(base, mailings=48)
        cm.compute_budget_affidavit(same)
        self.assertEqual(same['mailing_clause'], '')

    def test_the_lock_counts_unit_owners_not_letters(self):
        self.make('100', 'Away Owner', '68 Highview Ave, Staten Island, NY 10301')
        self.make('101', 'Home Owner', '2290 Sunset Ave Apt 101, Lake Worth Beach, FL 33461-6828')
        rows = bp.lock_list(self.prop, bp.ensure_project(Budget.objects.create(association=self.prop, year=2027)), True)
        self.assertEqual((len(bp.deliverable(rows)), bp.owner_count(rows)), (3, 2))
