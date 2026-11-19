"""An owner's printed address never carries the property's city, state and zip a second time (an old import added it to every address)."""
from django.test import TestCase

from core.models import Property

from . import budget_project as bp


class TheAddedEnding(TestCase):
    def setUp(self):
        self.prop = Property.objects.create(name='Lakeside Test', property_type=Property.Type.ASSOCIATION, city='Lake Worth Beach', state='FL', zip_code='33461')

    def lines(self, text):
        return bp.address_lines(text, self.prop)

    def test_an_address_with_its_own_city_state_and_zip_loses_the_added_ending(self):
        self.assertEqual(self.lines('17950 Pond Rd, Ashton, MD 20861, Lake Worth Beach, FL 33461'), ['17950 Pond Rd', 'Ashton, MD 20861'])

    def test_a_full_state_name_and_a_zip_plus_four_count_as_an_ending_of_its_own(self):
        self.assertEqual(self.lines('67-30 Eliot Ave, Middle Village, New York 11379-1129, Lake Worth Beach, FL 33461'), ['67-30 Eliot Ave', 'Middle Village, New York 11379-1129'])

    def test_a_canadian_address_loses_the_added_ending(self):
        self.assertEqual(self.lines('17 Sandy Brook Way, Kingsville, ON N9Y 4A6, Lake Worth Beach, FL 33461'), ['17 Sandy Brook Way', 'Kingsville, ON N9Y 4A6'])

    def test_a_street_only_address_keeps_the_property_city_and_zip(self):
        self.assertEqual(self.lines('2004 Lake Osborne Dr #1, Lake Worth Beach, FL 33461'), ['2004 Lake Osborne Dr #1', 'Lake Worth Beach, FL 33461'])
        self.assertEqual(self.lines('2004 Lake Osborne Dr #1, Unit A1, Lake Worth Beach, FL 33461'), ['2004 Lake Osborne Dr #1, Unit A1', 'Lake Worth Beach, FL 33461'])

    def test_the_same_ending_twice_is_one(self):
        self.assertEqual(self.lines('1406 High Ridge Road, Lake Worth Beach, FL 33461, Lake Worth Beach, FL 33461'), ['1406 High Ridge Road', 'Lake Worth Beach, FL 33461'])

    def test_without_the_property_nothing_changes(self):
        self.assertEqual(bp.address_lines('17950 Pond Rd, Ashton, MD 20861, Lake Worth Beach, FL 33461'), ['17950 Pond Rd, Ashton, MD 20861', 'Lake Worth Beach, FL 33461'])
