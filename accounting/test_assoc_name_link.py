from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import StaffProfile

from .tests import make_assoc


class TheAssociationNameInAHeadingTests(TestCase):
    """Where a screen's heading already prints the association's name, the name is the link to its property page. No screen gets a line of text it did not have: there is no name line
    above the Up link, and a screen that never printed the name still does not."""

    def setUp(self):
        self.boss = get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678')
        StaffProfile.objects.create(user=self.boss, role='admin', is_company_admin=True)
        self.client.force_login(self.boss)
        self.prop = make_assoc('Name Link Condos')
        self.link = f'<a href="{reverse("property_detail", args=[self.prop.pk])}" class="ac-assoc-link" title="Open Name Link Condos">Name Link Condos</a>'

    def html(self, name, *args):
        r = self.client.get(reverse(name, args=list(args)))
        self.assertEqual(r.status_code, 200, name)
        return r.content.decode()

    def test_no_screen_has_a_name_line_of_its_own_above_the_up_link(self):
        for name in ('accounting:units', 'accounting:charges', 'accounting:bills', 'accounting:reports', 'accounting:settings', 'accounting:recurring', 'accounting:journal', 'accounting:hub'):
            self.assertNotIn('class="ac-assoc"', self.html(name, self.prop.pk), name)

    def test_the_name_already_in_a_heading_is_the_link(self):
        for name in ('accounting:bills', 'accounting:settings', 'accounting:recurring', 'accounting:journal'):
            self.assertIn(self.link, self.html(name, self.prop.pk), name)

    def test_the_name_is_printed_once_in_the_heading_not_twice(self):
        self.assertEqual(self.html('accounting:settings', self.prop.pk).count('>Name Link Condos</a>'), 1)

    def test_a_heading_or_sentence_that_only_contains_the_name_among_other_words_is_not_made_a_link(self):
        self.assertNotIn('class="ac-assoc-link"', self.html('accounting:reports', self.prop.pk))
        self.assertNotIn('class="ac-assoc-link"', self.html('accounting:budget', self.prop.pk))

    def test_a_screen_that_never_printed_the_name_has_no_name_link(self):
        for name in ('accounting:units', 'accounting:charges'):
            self.assertNotIn('class="ac-assoc-link"', self.html(name, self.prop.pk), name)

    def test_the_close_review_names_the_association_once_and_as_a_link(self):
        html = self.html('accounting:close_review', self.prop.pk, '2026-10')
        self.assertIn(self.link, html)
        self.assertNotIn('class="ac-assoc"', html)

    def test_the_link_is_not_underlined_on_paper(self):
        self.assertIn('@media print { a.ac-assoc-link', self.html('accounting:settings', self.prop.pk))
