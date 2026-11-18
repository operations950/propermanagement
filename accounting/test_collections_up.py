"""Owner collections is a step of the month-end close: its Up button is that association's close sheet, whatever was visited before it, and never Accounting."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import StaffProfile

from .tests import make_assoc


class OwnerCollectionsUpTests(TestCase):
    def setUp(self):
        self.prop = make_assoc('Up Condos')
        self.user = get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678')
        StaffProfile.objects.create(user=self.user, role='admin', is_company_admin=True)
        self.client.force_login(self.user)
        self.html = {'HTTP_ACCEPT': 'text/html'}
        self.url = reverse('accounting:collections', args=[self.prop.pk])

    def up(self, *visits):
        for v in visits:
            self.client.get(v, **self.html)
        r = self.client.get(self.url + '?month=2026-10', **self.html)
        self.assertEqual(r.status_code, 200)
        return r.context['acc_back'], r.context['acc_back_label']

    def test_opened_cold_it_goes_up_to_the_close_sheet_for_the_month(self):
        self.assertEqual(self.up(), (reverse('accounting:close', args=[self.prop.pk, '2026-10']), 'Month-end close'))

    def test_even_with_accounting_the_most_recent_screen_before_it_up_is_the_close_sheet(self):
        close = reverse('accounting:close', args=[self.prop.pk, '2026-10'])
        hub = reverse('accounting:hub', args=[self.prop.pk])
        self.assertEqual(self.up(close, hub), (close, 'Month-end close'))                # the close sheet first, then Accounting, then Owner collections
        self.assertEqual(self.up(hub), (close, 'Month-end close'))

    def test_without_a_month_it_is_the_close_sheet_for_last_month(self):
        r = self.client.get(self.url, **self.html)
        self.assertTrue(r.context['acc_back'].startswith(reverse('accounting:close', args=[self.prop.pk, '2000-01'])[:-8]))
        self.assertEqual(r.context['acc_back_label'], 'Month-end close')


class TheCloseSheetNameTests(TestCase):
    def setUp(self):
        self.prop = make_assoc('Title Link Condos')
        user = get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678')
        StaffProfile.objects.create(user=user, role='admin', is_company_admin=True)
        self.client.force_login(user)

    def test_the_association_name_beside_the_icon_is_the_only_link_to_the_property_and_nothing_is_added_above_it(self):
        html = self.client.get(reverse('accounting:close', args=[self.prop.pk, '2026-10'])).content.decode()
        self.assertNotIn('class="ac-assoc"', html)                                       # no extra line of text above the title
        prop_url = reverse('property_detail', args=[self.prop.pk])
        self.assertEqual(html.count(f'href="{prop_url}"'), 1)
        self.assertIn(f'<a href="{prop_url}" class="text-reset text-decoration-none" title="Open Title Link Condos">Title Link Condos</a></h2>', html)
