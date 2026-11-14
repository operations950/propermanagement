"""The installable web app: the manifest (and where it opens for each kind of person), the icons, the service worker, the offline page, and the tags in the pages."""
import json
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from timeclock.models import TimeProfile

from . import owner_portal, pwa
from .models import StaffProfile

NO_MANIFEST = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
               'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}
IMG = Path(settings.BASE_DIR) / 'static' / 'img'


def _staff(username, role='', new_face=False, menu_role=''):
    user = get_user_model().objects.create_user(username, f'{username}@example.com', 'pw12345678')
    profile = StaffProfile.objects.create(user=user, role=role, new_face=new_face, menu_role=menu_role)
    return user, profile


def _manifest(client, query=''):
    response = client.get('/manifest.webmanifest' + query)
    assert response.status_code == 200, response.status_code
    return json.loads(response.content)


@override_settings(STORAGES=NO_MANIFEST)
class ManifestTests(TestCase):
    def test_it_is_valid_json_with_what_a_phone_needs_to_install_the_app(self):
        response = self.client.get('/manifest.webmanifest')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/manifest+json')
        self.assertIn('no-cache', response['Cache-Control'])
        body = json.loads(response.content)
        self.assertEqual((body['name'], body['short_name'], body['display'], body['scope'], body['id']), ('Proper Realty', 'Proper', 'standalone', '/', '/'))
        self.assertEqual(body['orientation'], 'portrait-primary')
        self.assertIn('standalone', body['display_override'])
        self.assertTrue(body['theme_color'].startswith('#') and body['background_color'].startswith('#'))
        sizes = {(i['sizes'], i['purpose']) for i in body['icons']}
        self.assertTrue({('192x192', 'any'), ('512x512', 'any'), ('512x512', 'maskable')} <= sizes)       # the three an installable app needs

    def test_every_icon_in_it_is_a_real_png_of_the_size_it_says(self):
        for icon in _manifest(self.client)['icons']:
            name = icon['src'].split('/static/', 1)[1]
            image = Image.open(Path(settings.BASE_DIR) / 'static' / name)
            self.assertEqual((image.format, f'{image.width}x{image.height}'), ('PNG', icon['sizes']), icon['src'])
            self.assertEqual(icon['type'], 'image/png')

    def test_the_other_icons_exist_too(self):
        for name, size in [('apple-touch-icon.png', (180, 180)), ('favicon-32.png', (32, 32))]:
            image = Image.open(IMG / name)
            self.assertEqual((image.format, image.size), ('PNG', size), name)
        self.assertEqual(Image.open(IMG / 'apple-touch-icon.png').mode, 'RGB')              # iPhones want it opaque


@override_settings(STORAGES=NO_MANIFEST)
class WhereTheAppOpensTests(TestCase):
    def test_not_signed_in_it_opens_on_the_login_page(self):
        self.assertEqual(_manifest(self.client)['start_url'], '/login/')

    def test_every_staff_member_opens_on_the_app_menu(self):
        for name, kw in (('amy', dict(role='property_manager')), ('gia', dict(role='property_manager', new_face=True, menu_role='property_manager')),
                         ('charlotte', dict(role='cleaner', new_face=True, menu_role='cleaner')), ('cora', dict(role='cleaner'))):
            user, _ = _staff(name, **kw)
            self.client.force_login(user)
            self.assertEqual(_manifest(self.client)['start_url'], '/m/', name)
            self.client.logout()

    def test_a_cleaner_on_the_time_clock_also_opens_on_the_app_menu_where_her_clock_tile_is(self):
        user, profile = _staff('charlotte', role='cleaner', new_face=True, menu_role='cleaner')
        TimeProfile.objects.create(staff=profile, applies=True)
        self.client.force_login(user)
        self.assertEqual(_manifest(self.client)['start_url'], '/m/')

    def test_a_user_with_no_staff_profile_opens_on_the_dashboard(self):
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.assertEqual(_manifest(self.client)['start_url'], '/')

    def test_an_owner_login_opens_on_the_portal(self):
        user = get_user_model().objects.create_user(f'{owner_portal.USERNAME_PREFIX}abc', '', 'pw12345678')
        self.client.force_login(user)
        self.assertEqual(_manifest(self.client)['start_url'], '/owner/')

    def test_the_owner_portals_own_manifest_is_a_separate_app_that_opens_on_the_portal(self):
        body = _manifest(self.client, '?app=owner')
        self.assertEqual((body['start_url'], body['id'], body['short_name']), ('/owner/', '/owner/', 'Owner'))
        self.assertNotEqual(body['id'], _manifest(self.client)['id'])            # the staff app and the owner app can both be installed


@override_settings(STORAGES=NO_MANIFEST)
class OpenToEveryoneTests(TestCase):
    """The owner wall sends an owner login to the portal and the policy wall sends an employee to sign the policy; neither may swallow the app's own files."""

    def test_an_owner_login_gets_all_three_instead_of_being_sent_to_the_portal(self):
        self.client.force_login(get_user_model().objects.create_user(f'{owner_portal.USERNAME_PREFIX}abc', '', 'pw12345678'))
        self.assertEqual(self.client.get('/dashboard/').status_code, 302)           # (the wall is on: an ordinary staff page is refused)
        for path in ('/manifest.webmanifest', '/sw.js', '/offline/'):
            self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_an_employee_who_has_not_signed_the_policy_gets_all_three(self):
        user, profile = _staff('alexis', role='property_manager')
        TimeProfile.objects.create(staff=profile, applies=True)
        self.client.force_login(user)
        self.assertEqual(self.client.get('/').status_code, 302)                      # (sent to sign the policy)
        for path in ('/manifest.webmanifest', '/sw.js', '/offline/'):
            self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_the_names_still_resolve(self):
        self.assertEqual((reverse('pwa_manifest'), reverse('pwa_service_worker'), reverse('pwa_offline')), ('/manifest.webmanifest', '/sw.js', '/offline/'))

    def test_only_a_get_is_answered_here(self):
        self.assertNotEqual(self.client.post('/sw.js').status_code, 200)


@override_settings(STORAGES=NO_MANIFEST)
class ServiceWorkerTests(TestCase):
    def setUp(self):
        self.response = self.client.get('/sw.js')
        self.body = self.response.content.decode()

    def test_it_is_served_as_a_script_from_the_root_and_never_cached_by_the_browser(self):
        self.assertEqual(self.response.status_code, 200)
        self.assertTrue(self.response['Content-Type'].startswith('application/javascript'))
        self.assertEqual(self.response['Service-Worker-Allowed'], '/')
        self.assertIn('no-cache', self.response['Cache-Control'])

    def test_the_cache_carries_the_build_so_a_deploy_replaces_it(self):
        self.assertIn(f"const CACHE = '{pwa.CACHE_NAME}';", self.body)
        self.assertTrue(pwa.CACHE_NAME.startswith('proper-'))
        self.assertIn("n.indexOf('proper-') === 0 && n !== CACHE", self.body)            # old caches are deleted when it takes over

    def test_it_never_touches_anything_but_a_get_and_never_stores_a_page(self):
        self.assertIn("if (request.method !== 'GET') return;", self.body)
        self.assertEqual(self.body.count('cache.put('), 1)                                  # the one place anything is stored: ...
        before_put = self.body.split('cache.put(')[0]
        self.assertIn("url.pathname.indexOf('/static/') === 0", before_put)                 # ... a static file
        navigate = self.body.split("request.mode === 'navigate'")[1].split('return;')[0]
        self.assertNotIn('cache.put', navigate)                                              # a page goes to the network, and to the offline page when there is none
        self.assertIn('caches.match(OFFLINE)', navigate)

    def test_it_ignores_other_hosts(self):
        self.assertIn('url.origin !== self.location.origin', self.body)


@override_settings(STORAGES=NO_MANIFEST)
class OfflinePageTests(TestCase):
    def test_it_is_a_small_page_that_needs_no_login_and_no_other_files(self):
        response = self.client.get('/offline/')
        self.assertEqual(response.status_code, 200)
        text = response.content.decode()
        self.assertIn('You are offline', text)
        self.assertIn('Reconnect and try again.', text)
        self.assertNotIn('<script src', text)
        self.assertNotIn('<link', text)


@override_settings(STORAGES=NO_MANIFEST)
class PagesCarryTheAppTests(TestCase):
    def test_the_login_page_links_the_manifest_the_icons_and_the_iphone_settings(self):
        html = self.client.get('/login/').content.decode()
        self.assertIn('<link rel="manifest" href="/manifest.webmanifest" crossorigin="use-credentials">', html)
        self.assertIn('rel="apple-touch-icon"', html)
        self.assertIn('name="theme-color"', html)
        self.assertIn('name="apple-mobile-web-app-capable" content="yes"', html)
        self.assertIn('js/pwa.js', html)
        self.assertIn('id="pwaTip"', html)

    def test_a_staff_page_has_the_menu_entry_that_shows_how_to_install(self):
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        html = self.client.get('/').content.decode()
        self.assertIn('data-pwa-install', html)
        self.assertIn('Install the app', html)
        self.assertIn('rel="manifest"', html)

    def test_the_owner_portal_links_its_own_manifest(self):
        html = self.client.get('/owner/login/').content.decode()
        self.assertIn('href="/manifest.webmanifest?app=owner"', html)
        self.assertIn('rel="apple-touch-icon"', html)
        self.assertIn('js/pwa.js', html)
        self.assertIn('id="pwaTip"', html)
