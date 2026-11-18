from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from . import ledger
from .tests import make_assoc
from .views_setup import settings_rows


class LateFeeOnThePropertyPageTests(TestCase):
    """A late fee that is only a percent (no flat amount) or only a flat amount (no percent) used to crash the whole property page."""

    def setUp(self):
        self.prop = make_assoc('Fee Condos')
        self.profile = ledger.profile_for(self.prop)

    def set_fee(self, kind, flat, pct, grace=10):
        p = self.profile
        p.late_fee_kind, p.late_fee_flat, p.late_fee_percent, p.late_fee_grace_days, p.late_fee_confirmed = kind, flat, pct, grace, True
        p.save()

    def words(self):
        return next(r['value'] for r in settings_rows(self.prop) if r['label'] == 'Late fee and interest')

    def test_a_percent_only_fee_reads_as_a_percent(self):
        self.set_fee('percent', None, D('5'))
        self.assertTrue(self.words().startswith('5%, 10 days after the due date'))

    def test_a_flat_only_fee_reads_as_dollars(self):
        self.set_fee('flat', D('25'), None)
        self.assertTrue(self.words().startswith('$25.00, 10 days'))

    def test_a_greater_of_fee_reads_both(self):
        self.set_fee('greater', D('25'), D('5'))
        self.assertTrue(self.words().startswith('$25.00 or 5%, whichever is greater'))

    def test_a_fee_with_nothing_typed_still_reads_without_crashing(self):
        for kind in ('flat', 'percent', 'greater'):
            self.set_fee(kind, None, None)
            self.assertIn('days after the due date', self.words(), kind)

    def test_the_property_page_opens_for_every_kind_of_fee(self):
        boss = get_user_model().objects.create_superuser('boss', 'b@x.com', 'pw12345678')
        self.client.force_login(boss)
        for kind, flat, pct in (('percent', None, D('5')), ('flat', D('25'), None), ('greater', D('25'), D('5')), ('percent', None, None), ('none', None, None)):
            self.set_fee(kind, flat, pct)
            r = self.client.get(reverse('property_detail', args=[self.prop.pk]))
            self.assertEqual(r.status_code, 200, (kind, flat, pct))
