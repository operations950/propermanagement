"""The design review: error pages, the login page, and the small phone-friendliness changes on the close screens (sizes, scrolling boxes, labelled fields, long text behind a bubble)."""
from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.test import Client, RequestFactory, TestCase, override_settings
from django.views.defaults import permission_denied

from .models import StaffProfile

STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}


@override_settings(STORAGES=STORAGES)
class ErrorPagesTests(TestCase):
    def test_a_missing_page_says_so_plainly_and_offers_the_way_home_and_back(self):
        user = get_user_model().objects.create_user('walker', 'w@example.com', 'pw12345678')
        StaffProfile.objects.create(user=user)
        self.client.force_login(user)
        response = self.client.get('/there-is-no-such-page/')
        self.assertEqual(response.status_code, 404)
        html = response.content.decode()
        self.assertIn("We can't find that page", html)
        self.assertIn('Go to the home screen', html)
        self.assertIn('Go back', html)

    def test_a_missing_page_looks_the_same_to_someone_not_signed_in(self):
        response = Client().get('/there-is-no-such-page/')
        self.assertEqual(response.status_code, 404)
        self.assertIn("We can't find that page", response.content.decode())

    def test_no_access_is_a_clear_page_not_a_bare_error(self):
        request = RequestFactory().get('/admin-tools/')
        user = get_user_model().objects.create_user('nosy', 'n@example.com', 'pw12345678')
        StaffProfile.objects.create(user=user)
        request.user = user
        request.session = {}
        response = permission_denied(request, Exception('no'))
        self.assertEqual(response.status_code, 403)
        self.assertIn("You don't have access to this page", response.content.decode())

    def test_a_timed_out_form_gets_a_friendly_page(self):
        response = Client(enforce_csrf_checks=True).post('/login/', {'username': 'a', 'password': 'b'})
        self.assertEqual(response.status_code, 403)
        self.assertIn('That page timed out', response.content.decode())

    def test_the_server_error_page_stands_on_its_own(self):
        html = render_to_string('500.html')
        self.assertIn('Something went wrong on our side', html)
        self.assertNotIn('{%', html)                                   # nothing in it needs the site's own templates or context to work
        self.assertIn('Try this page again', html)


@override_settings(STORAGES=STORAGES)
class LoginPageTests(TestCase):
    def test_the_login_page_is_built_for_a_thumb(self):
        html = Client().get('/login/').content.decode()
        self.assertIn('btn btn-primary btn-lg w-100', html)           # a big button
        self.assertIn('Show password', html)                          # a way to check what was typed
        self.assertIn('autocapitalize="none"', html)                  # a phone must not capitalise an email address


@override_settings(STORAGES=STORAGES)
class CloseScreensOnAPhoneTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))

    def get(self, name, **kw):
        from django.urls import reverse
        response = self.client.get(reverse(name) + '?month=2026-10')
        self.assertEqual(response.status_code, 200, name)
        return response.content.decode()

    def test_the_dense_screens_carry_the_phone_sizes_and_nothing_else_changes_on_a_desktop(self):
        for name in ('close_home', 'close_overview', 'close_sales_tax', 'tourist_tax', 'close_payouts', 'admin_tools'):
            html = self.get(name)
            self.assertIn('@media (max-width: 768px)', html, name)    # the sizes are inside the phone media query only
            self.assertIn('.ck-step', html, name)

    def test_the_checklist_shows_each_steps_one_line_detail_on_a_phone(self):
        html = self.get('close_home')
        self.assertIn('d-md-none', html)                              # the line that used to be a hover tooltip nobody could see on a phone

    def test_the_overview_keeps_long_text_behind_a_bubble_and_folds_the_danger_zone_away(self):
        html = self.get('close_overview')
        self.assertIn('How these figures work', html)
        self.assertIn('<details class="mt-3">', html)
        self.assertIn('Reopen everything', html)                      # still there, one tap away
        self.assertNotIn('font-size: 0.72rem;">Income less commission', html)

    def test_the_sales_tax_table_scrolls_and_its_fields_are_named(self):
        html = self.get('close_sales_tax')
        self.assertIn('table-responsive', html)
        for label in ('Name of the return', 'How often it is filed', 'Day of the month it is due'):
            self.assertIn(f'aria-label="{label}"', html)
