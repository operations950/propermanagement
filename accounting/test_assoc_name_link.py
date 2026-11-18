from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import StaffProfile

from .tests import make_assoc


class TheAssociationNameAtTheTopTests(TestCase):
    """On an individual association's screens the association's name is at the very top, and it opens that association's property page."""

    def setUp(self):
        self.boss = get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678')
        StaffProfile.objects.create(user=self.boss, role='admin', is_company_admin=True)
        self.client.force_login(self.boss)
        self.prop = make_assoc('Name Link Condos')
        self.link = f'<a href="{reverse("property_detail", args=[self.prop.pk])}" class="ac-assoc ac-noprint" title="Open Name Link Condos">Name Link Condos</a>'

    def html(self, name, *args):
        r = self.client.get(reverse(name, args=list(args)))
        self.assertEqual(r.status_code, 200, name)
        return r.content.decode()

    def test_the_name_is_a_link_to_the_property_on_the_screens_of_one_association(self):
        for name in ('accounting:units', 'accounting:charges', 'accounting:bills', 'accounting:reports', 'accounting:settings', 'accounting:recurring', 'accounting:journal'):
            self.assertIn(self.link, self.html(name, self.prop.pk), name)

    def test_it_sits_above_the_up_link_at_the_very_top(self):
        html = self.html('accounting:units', self.prop.pk)
        self.assertLess(html.index('class="ac-assoc'), html.index('data-up-link'))

    def test_the_accounting_hub_already_says_it_in_its_up_link_so_it_is_not_repeated(self):
        html = self.html('accounting:hub', self.prop.pk)
        self.assertNotIn('class="ac-assoc', html)
        self.assertIn(reverse('property_detail', args=[self.prop.pk]), html)

    def test_a_screen_that_is_not_about_one_association_has_no_name_line(self):
        self.assertNotIn('class="ac-assoc', self.html('accounting:budgets'))

    def test_the_name_is_left_out_of_a_printout(self):
        self.assertIn('ac-noprint', self.link)
