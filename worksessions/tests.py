from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import StaffProfile
from worksessions.models import Frequency, Session, SessionTemplate
from worksessions.services import generation


class TwiceAYearTests(TestCase):
    """A compliance rule can open twice a year: every six months from the first date."""

    def setUp(self):
        user = get_user_model().objects.create_user('pm', 'pm@example.com', 'pw12345678')
        self.staff = StaffProfile.objects.get_or_create(user=user)[0]

    def rule(self, first, **kw):
        return SessionTemplate.objects.create(name='Fire alarm inspection', frequency=Frequency.SEMIANNUAL, next_open_date=first, owner=self.staff, **kw)

    def test_it_is_in_the_list_and_reads_twice_a_year(self):
        self.assertIn(('semiannual', 'Twice a year'), Frequency.choices)
        self.assertEqual(self.rule(date(2026, 1, 15)).get_frequency_display(), 'Twice a year')

    def test_it_comes_round_every_six_months(self):
        self.assertEqual(generation.advance(date(2026, 1, 15), Frequency.SEMIANNUAL, None), date(2026, 7, 15))
        self.assertEqual(generation.advance(date(2026, 7, 15), Frequency.SEMIANNUAL, None), date(2027, 1, 15))
        self.assertEqual(generation.advance(date(2026, 8, 31), Frequency.SEMIANNUAL, None), date(2027, 2, 28))
        self.assertEqual(generation.period_label_for(date(2026, 7, 15), Frequency.SEMIANNUAL), 'July 2026')

    def test_the_preview_and_the_generator_use_it(self):
        t = self.rule(date(2026, 1, 15))
        self.assertEqual([p['date'] for p in generation.preview_next_occurrences(t, 3)], [date(2026, 1, 15), date(2026, 7, 15), date(2027, 1, 15)])
        self.assertEqual(generation.generate_for_template(t, today=date(2026, 8, 1)), 2)       # January and July have both opened
        t.refresh_from_db()
        self.assertEqual((t.next_open_date, sorted(Session.objects.values_list('period_label', flat=True))), (date(2027, 1, 15), ['January 2026', 'July 2026']))
        self.assertEqual(generation.generate_for_template(t, today=date(2026, 8, 2)), 0)       # nothing opens again until January

    def test_it_sorts_between_yearly_and_quarterly_and_filters_on_the_compliance_tab(self):
        from workspace import services
        order = list(services.FREQ_ORDER)
        self.assertEqual(order.index(Frequency.SEMIANNUAL), order.index(Frequency.YEARLY) + 1)
        self.assertLess(order.index(Frequency.SEMIANNUAL), order.index(Frequency.QUARTERLY))
        t = self.rule(date(2026, 1, 15))
        generation.generate_for_template(t, today=date(2026, 1, 15))
        items, sel = services.compliance_items({'frequency': 'semiannual'})
        self.assertEqual((sel['frequency'], items.count()), ('semiannual', 1))
