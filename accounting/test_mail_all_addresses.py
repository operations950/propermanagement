"""The budget mailing: one letter to each owner's main address, or (when asked) also one at the unit for the owners who live elsewhere."""
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

    def make(self, label, name, mailing):
        c = Contact.objects.create(name=name, contact_type=Contact.ContactType.OWNER)
        OwnerProfile.objects.create(contact=c, mailing_address=mailing)
        self.Ownership.objects.create(unit=self.units[label], person=c, is_billed=True)
        return c

    def test_by_default_there_is_one_letter_per_owner_to_the_main_address(self):
        self.make('100', 'Away Owner', '68 Highview Ave, Staten Island, NY 10301')
        self.make('101', 'Home Owner', '2290 Sunset Ave Apt 101, Lake Worth Beach, FL 33461-6828')
        rows = bp.mailing_rows(self.prop)
        self.assertEqual([(r['unit'], r['source']) for r in rows if r['owner']], [('100', 'mailing'), ('101', 'mailing')])

    def test_with_all_addresses_an_owner_living_elsewhere_also_gets_the_unit_and_a_resident_does_not(self):
        self.make('100', 'Away Owner', '68 Highview Ave, Staten Island, NY 10301')
        self.make('101', 'Home Owner', '2290 Sunset Ave Apt 101, Lake Worth Beach, FL 33461-6828')
        rows = [r for r in bp.mailing_rows(self.prop, all_addresses=True) if r['owner']]
        self.assertEqual([(r['unit'], r['source']) for r in rows], [('100', 'mailing'), ('100', 'unit_also'), ('101', 'mailing')])
        extra = rows[1]
        self.assertEqual(extra['lines'], ['2290 Sunset Ave Unit 100', 'Lake Worth Beach, FL 33461'])
        self.assertEqual(extra['owner'], 'Away Owner')
        self.assertEqual(len(bp.deliverable(rows)), 3)
        self.assertEqual(bp.flagged(rows), [])                           # the extra copy is not a problem to look at

    def test_the_place_of_an_address_ignores_how_it_is_written(self):
        same = bp._same_place(['2290 Sunset Ave # 12', 'Lake Worth Beach, FL 33461-6828'], ['2290 Sunset Ave Unit 12', 'Lake Worth Beach, FL 33461'])
        self.assertTrue(same)
        self.assertFalse(bp._same_place(['2290 Sunset Ave Apt 3', 'Lake Worth Beach, FL 33461'], ['2290 Sunset Ave Unit 4', 'Lake Worth Beach, FL 33461']))
        self.assertFalse(bp._same_place(['1 Estero Blvd', 'Fort Myers Beach, FL 33931'], ['1 Estero Blvd Apt 2', 'Fort Myers Beach, FL 33931']))

    def test_the_packet_checkbox_sets_the_choice_and_the_envelopes_follow_it(self):
        self.make('100', 'Away Owner', '68 Highview Ave, Staten Island, NY 10301')
        bud = Budget.objects.create(association=self.prop, year=2027)
        project = bp.ensure_project(bud)
        self.assertFalse(project.mail_all_addresses)
        self.assertEqual(len([r for r in bp.rows_for_packet(self.prop, project) if r['owner']]), 1)
        BudgetProject.objects.filter(pk=project.pk).update(mail_all_addresses=True)
        project.refresh_from_db()
        self.assertEqual(len([r for r in bp.rows_for_packet(self.prop, project) if r['owner']]), 2)
