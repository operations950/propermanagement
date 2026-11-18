"""The primary action of every main screen is one button style (btn-action), and Quick add lists every create action of those screens."""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import StaffProfile

STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}


@override_settings(STORAGES=STORAGES)
class ActionButtonTests(TestCase):
    def setUp(self):
        self.boss = get_user_model().objects.create_superuser('boss5', 'b5@example.com', 'pw12345678')
        StaffProfile.objects.create(user=self.boss, role='admin', is_company_admin=True)
        self.client.force_login(self.boss)

    def page(self, name, *args):
        r = self.client.get(reverse(name, args=list(args)))
        self.assertEqual(r.status_code, 200, name)
        return r.content.decode()

    def test_the_style_is_defined_once_for_the_whole_site(self):
        html = self.page('property_list')
        self.assertIn('.btn-action {', html)
        self.assertIn('.btn-action-outline', html)

    def test_quick_add_has_every_create_action(self):
        html = self.page('property_list')
        menu = html.split('title="Quick add"')[1].split('</ul>')[0]
        for url_name, label in (('ticket_create', 'New ticket'), ('session_template_create', 'New compliance rule'), ('process_template_create', 'New project template'),
                                ('onsite_visit_create', 'New on-site visit'), ('property_create', 'New property'), ('contact_create', 'New contact')):
            self.assertIn(f'href="{reverse(url_name)}"', menu, label)
            self.assertIn(label, menu)
        self.assertIn(f'href="{reverse("ws_projects")}?start=1"', menu)
        self.assertIn('Start project', menu)

    def test_the_phone_menu_lists_the_same_create_actions(self):
        html = self.page('mobile_menu')
        for label in ('New ticket', 'New compliance rule', 'Start project', 'New project template', 'New on-site visit', 'New property', 'New contact'):
            self.assertIn(label, html)

    def test_properties_and_on_site_use_the_action_button(self):
        self.assertIn(f'href="{reverse("property_create")}" class="btn-action pl-new"', self.page('property_list'))
        for name in ('onsite_dashboard', 'onsite_turnovers', 'onsite_rentals_calendar', 'onsite_performance'):
            html = self.page(name)
            self.assertIn(f'href="{reverse("onsite_visit_create")}" class="btn-action"', html, name)
