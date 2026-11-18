"""Dark mode: the Light / Dark / Automatic switch and where it is kept, the first-paint script, the dark grey palette (never black, readable), the printed and PDF pages that must stay light, and
the guarantee that converting the templates' literal colours to named ones changed nothing in light mode."""
import glob
import os
import re

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import StaffProfile

User = get_user_model()
PW = 'pw12345678'
STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}
BASE = settings.BASE_DIR


def person(email, **kw):
    user = User.objects.create_user(username=email, email=email, password=PW)
    return StaffProfile.objects.create(user=user, **kw)


def read(*parts):
    with open(os.path.join(BASE, *parts), encoding='utf8') as f:
        return f.read()


def hex_to_rgb(h):
    h = h.lstrip('#')
    if len(h) == 3:
        h = ''.join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def luminance(h):
    def f(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = hex_to_rgb(h)
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def contrast(a, b):
    la, lb = luminance(a), luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def dark_vars():
    css = read('static', 'css', 'dark.css')
    block = css.split('html[data-theme="dark"] {', 1)[1].split('}', 1)[0]
    return dict(re.findall(r'(--[\w-]+):\s*([^;]+);', block))


def admin(email, **kw):
    kw.setdefault('is_company_admin', True)
    return person(email, **kw)


class WhoGetsDarkModeTests(TestCase):
    """For now only people with the Admin box (superusers count). Everyone else, and anyone signed out, is always light. One helper decides, so widening it is a one-line change."""

    def test_the_helper_says_yes_only_to_admins(self):
        from django.contrib.auth.models import AnonymousUser
        from core.permissions import can_use_dark_mode
        self.assertTrue(can_use_dark_mode(admin('a1@x.com').user))
        self.assertTrue(can_use_dark_mode(User.objects.create_superuser('boss', 'boss@x.com', PW)))
        self.assertFalse(can_use_dark_mode(person('a2@x.com').user))
        self.assertFalse(can_use_dark_mode(person('a3@x.com', can_financial=True, can_time_admin=True).user))            # the other switches do not count
        gone = admin('a4@x.com')
        gone.user.is_active = False
        gone.user.save()
        self.assertFalse(can_use_dark_mode(gone.user))
        self.assertFalse(can_use_dark_mode(AnonymousUser()))
        self.assertFalse(can_use_dark_mode(None))


class TheSwitchTests(TestCase):
    def test_a_new_profile_is_automatic(self):
        self.assertEqual(person('a@x.com').theme, 'auto')

    def test_the_choice_needs_a_login_and_a_post(self):
        self.assertEqual(self.client.post(reverse('theme_set'), {'theme': 'dark'}).status_code, 302)
        p = admin('b@x.com')
        self.client.force_login(p.user)
        self.assertEqual(self.client.get(reverse('theme_set')).status_code, 405)

    def test_each_choice_is_saved_on_an_admins_profile(self):
        p = admin('c@x.com')
        self.client.force_login(p.user)
        for choice in ('dark', 'light', 'auto'):
            r = self.client.post(reverse('theme_set'), {'theme': choice})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json(), {'ok': True, 'saved': True, 'theme': choice})
            p.refresh_from_db()
            self.assertEqual(p.theme, choice)

    def test_a_bad_value_is_refused_and_nothing_changes(self):
        p = admin('d@x.com', theme='dark')
        self.client.force_login(p.user)
        for bad in ('', 'purple', 'DARK', 'black'):
            r = self.client.post(reverse('theme_set'), {'theme': bad})
            self.assertEqual(r.status_code, 400)
            self.assertFalse(r.json()['ok'])
        p.refresh_from_db()
        self.assertEqual(p.theme, 'dark')

    def test_a_non_admin_is_refused_and_nothing_is_saved(self):
        for flags in ({}, {'can_financial': True}, {'can_time_admin': True}):
            p = person(f'n{len(flags)}{"".join(flags)}@x.com', **flags)
            self.client.force_login(p.user)
            r = self.client.post(reverse('theme_set'), {'theme': 'dark'})
            self.assertEqual(r.status_code, 403)
            self.assertFalse(r.json()['ok'])
            p.refresh_from_db()
            self.assertEqual(p.theme, 'auto')

    def test_a_superuser_without_a_staff_profile_is_answered_but_only_this_browser_remembers(self):
        user = User.objects.create_superuser('boss', 'boss@x.com', PW)
        self.client.force_login(user)
        r = self.client.post(reverse('theme_set'), {'theme': 'dark'})
        self.assertEqual(r.json(), {'ok': True, 'saved': False, 'theme': 'dark'})

    def test_one_persons_choice_does_not_touch_another(self):
        a, b = admin('e@x.com'), admin('f@x.com')
        self.client.force_login(a.user)
        self.client.post(reverse('theme_set'), {'theme': 'dark'})
        b.refresh_from_db()
        self.assertEqual(b.theme, 'auto')


@override_settings(STORAGES=STORAGES)
class ThePageTests(TestCase):
    """What an admin gets."""

    def page(self, profile, url='property_list'):
        self.client.force_login(profile.user)
        return self.client.get(reverse(url)).content.decode()

    def test_every_preference_is_on_the_html_tag_for_the_first_paint_script(self):
        for pref in ('auto', 'light', 'dark'):
            html = self.page(admin(f'{pref}@x.com', theme=pref))
            self.assertIn(f'data-theme-pref="{pref}"', html)
            self.assertIn(f'data-theme-url="{reverse("theme_set")}"', html)

    def test_the_script_that_decides_before_the_first_paint_comes_before_the_stylesheets(self):
        html = self.page(admin('g@x.com'))
        head = html.split('</head>')[0]
        self.assertIn('window.__themeApply', head)
        self.assertLess(head.index('window.__themeApply'), head.index('bootstrap.min.css'))
        for needle in ("prefers-color-scheme: dark", "setAttribute('data-theme'", "setAttribute('data-bs-theme'", "localStorage"):
            self.assertIn(needle, head)

    def test_the_dark_stylesheets_and_the_switch_script_are_loaded(self):
        html = self.page(admin('h@x.com'))
        for needle in ('css/dark-literals.css', 'css/dark.css', 'js/theme.js'):
            self.assertIn(needle, html)
        self.assertLess(html.index('css/dark.css'), html.index('</head>'))

    def test_the_menu_offers_light_dark_and_automatic(self):
        html = self.page(admin('i@x.com'))
        for choice in ('light', 'dark', 'auto'):
            self.assertIn(f'data-theme-choice="{choice}"', html)
        self.assertIn('data-theme-label', html)

    def test_the_phone_menu_has_an_appearance_row(self):
        html = self.page(admin('j@x.com'), 'mobile_menu')
        self.assertIn('>Appearance<', html)
        self.assertEqual(html.count('data-theme-choice="dark"'), 2)                    # the top bar's menu and the phone's own row

    def test_the_switch_script_posts_to_the_address_on_the_page_and_never_when_there_is_none(self):
        js = read('static', 'js', 'theme.js')
        self.assertIn("getAttribute('data-theme-url')", js)
        self.assertIn('if (!url) return;', js)
        self.assertIn('X-CSRFToken', js)

    def test_the_choice_is_saved_even_though_the_csrf_cookie_cannot_be_read_by_scripts(self):
        # on the live site CSRF_COOKIE_HTTPONLY is on, so the switch cannot read the cookie: the token is printed on the page and the save goes through with it
        from django.test import Client
        profile = admin('csrf@x.com', theme='auto')
        client = Client(enforce_csrf_checks=True)
        client.force_login(profile.user)
        html = client.get(reverse('property_list')).content.decode()
        token = re.search(r'<html[^>]*data-csrf="([^"]+)"', html).group(1)
        self.assertTrue(token)
        self.assertIn("getAttribute('data-csrf')", read('static', 'js', 'theme.js'))
        self.assertEqual(client.post(reverse('theme_set'), {'theme': 'dark'}).status_code, 403)                 # without the token: refused (what used to happen)
        r = client.post(reverse('theme_set'), {'theme': 'dark'}, HTTP_X_CSRFTOKEN=token)
        self.assertEqual(r.json(), {'ok': True, 'saved': True, 'theme': 'dark'})
        profile.refresh_from_db()
        self.assertEqual(profile.theme, 'dark')
        self.assertIn('data-theme-pref="dark"', client.get(reverse('property_list')).content.decode())       # the next screen opens dark

    def test_a_choice_the_server_has_not_confirmed_yet_wins_over_the_saved_one_on_the_next_screen(self):
        head = self.page(admin('pend@x.com', theme='auto')).split('</head>')[0]
        self.assertIn("localStorage.getItem('theme-pending')", head)
        js = read('static', 'js', 'theme.js')
        for needle in ("setItem('theme-pending'", "removeItem('theme-pending')", 'keepalive: true'):
            self.assertIn(needle, js)


@override_settings(STORAGES=STORAGES)
class EveryoneElseIsAlwaysLightTests(TestCase):
    """A non-admin and anyone signed out never see the switch, and nothing can make the page dark: not their system setting, not a value stored on their profile, not one stored in the browser."""

    def check_light(self, html):
        tag = html.split('<html', 1)[1].split('>', 1)[0]
        self.assertNotIn('data-theme', tag)
        self.assertNotIn('data-bs-theme', tag)
        for needle in ('window.__themeApply', 'prefers-color-scheme', 'data-theme-choice="', 'css/dark.css', 'css/dark-literals.css', 'js/theme.js', 'theme-pref'):
            self.assertNotIn(needle, html, needle)

    def test_a_non_admin_gets_no_switch_and_no_dark_even_with_dark_saved_on_their_profile(self):
        for flags in ({}, {'can_financial': True}, {'can_time_admin': True}, {'role': 'cleaner'}):
            p = person(f'm{"".join(flags)}@x.com', theme='dark', **flags)
            self.client.force_login(p.user)
            for url in ('property_list', 'mobile_menu', 'ticket_list'):
                self.check_light(self.client.get(reverse(url)).content.decode())

    def test_the_phone_menu_has_no_appearance_row_for_a_non_admin(self):
        self.client.force_login(person('p@x.com', theme='dark').user)
        self.assertNotIn('Appearance', self.client.get(reverse('mobile_menu')).content.decode())

    def test_a_signed_out_page_is_light_only(self):
        self.check_light(self.client.get(reverse('login')).content.decode())

    def test_the_owner_portal_and_vendor_pages_never_load_the_dark_files(self):
        for parts in (('core', 'templates', 'core', 'owner', 'base.html'), ('vendorportal', 'templates', 'vendorportal', 'base.html')):
            html = read(*parts)
            for needle in ('dark.css', 'theme.js', 'data-theme', '__themeApply'):
                self.assertNotIn(needle, html, (parts, needle))

    def test_demoting_an_admin_makes_the_page_light_again_whatever_was_saved(self):
        p = admin('q@x.com', theme='dark')
        self.client.force_login(p.user)
        self.assertIn('data-theme-pref="dark"', self.client.get(reverse('property_list')).content.decode())
        p.is_company_admin = False
        p.save()
        self.check_light(self.client.get(reverse('property_list')).content.decode())


class ThePaletteTests(TestCase):
    def test_the_dark_page_is_a_dark_grey_never_black(self):
        v = dark_vars()
        page = v['--surface-page'].strip()
        r, g, b = hex_to_rgb(page)
        self.assertEqual(page.lower(), '#22262d')
        self.assertTrue(min(r, g, b) >= 0x18, 'a dark grey, not black')
        self.assertTrue(max(r, g, b) <= 0x40, 'still dark')
        card = v['--surface-card'].strip()
        self.assertGreater(luminance(card), luminance(page))                        # the cards sit a step lighter than the page

    def test_the_text_and_accent_colours_read_on_the_dark_card(self):
        v = dark_vars()
        card = v['--surface-card'].strip()
        for name in ('--ink-primary', '--ink-secondary', '--ink-muted', '--brand-primary', '--brand-slate', '--status-critical', '--status-warning', '--status-good', '--priority-urgent',
                     '--accent-contrast'):
            self.assertGreaterEqual(contrast(v[name].strip(), card), 4.5, name)

    def test_the_dark_words_on_the_light_accents_read_too(self):
        v = dark_vars()
        for accent in ('--brand-primary', '--brand-slate', '--status-good', '--status-warning', '--priority-urgent'):
            self.assertGreaterEqual(contrast(v['--on-accent'].strip(), v[accent].strip()), 4.5, accent)

    def test_the_light_muted_text_is_now_readable_on_white(self):
        base = read('templates', 'base.html')
        m = re.search(r'--ink-muted:\s*(#[0-9a-fA-F]{6})', base)
        self.assertEqual(m.group(1).lower(), '#6f6d69')
        self.assertGreaterEqual(contrast(m.group(1), '#ffffff'), 4.5)

    def test_everything_dark_is_for_the_screen_only_so_a_printout_stays_light(self):
        for name in ('dark.css', 'dark-literals.css'):
            css = re.sub(r'/\*.*?\*/', '', read('static', 'css', name), flags=re.S)
            depth, top = 0, []
            buf = ''
            for ch in css:
                if ch == '{':
                    if depth == 0:
                        top.append(buf.strip())
                        buf = ''
                    depth += 1
                elif ch == '}':
                    depth -= 1
                elif depth == 0:
                    buf += ch
            self.assertTrue(top and all(t == '@media screen' for t in top), (name, top))

    def test_the_printed_and_pdf_pages_never_load_the_dark_stylesheets(self):
        for parts in (('legalforms', 'templates', 'legalforms', 'print.html'), ('accounting', 'templates', 'accounting', 'check_print.html'),
                      ('timeclock', 'templates', 'timeclock', 'policy_copy.html'), ('vendorportal', 'templates', 'vendorportal', 'base.html'), ('core', 'templates', 'core', 'owner', 'base.html')):
            html = read(*parts)
            self.assertNotIn('dark.css', html, parts)
            self.assertNotIn('data-theme', html, parts)

    def test_the_signature_pad_stays_white_so_ink_can_be_seen(self):
        self.assertIn('[data-signature-canvas] { background: #ffffff; }', read('static', 'css', 'dark.css'))


class LightModeIsUnchangedTests(TestCase):
    """Literal colours in templates became var(--b-xxxxxx, #xxxxxx): the dark theme defines the variable, light mode never does, so the fallback (the original colour) is what shows."""

    def files(self):
        out = []
        for pat in ('**/*.html', 'static/css/*.css'):
            for f in glob.glob(os.path.join(BASE, pat), recursive=True):
                if any(s in f for s in ('venv', 'staticfiles', os.sep + 'vendor' + os.sep, 'node_modules')):
                    continue
                out.append(f)
        return out

    def test_every_converted_colour_falls_back_to_the_colour_it_was(self):
        bad = []
        for f in self.files():
            text = open(f, encoding='utf8', errors='ignore').read()
            for name, fallback in re.findall(r'var\((--[btl]-[0-9a-f]{3,6}),\s*([^()]*?)\)', text):
                if fallback.strip().lower().lstrip('#') != name.split('-', 3)[3]:
                    bad.append((os.path.relpath(f, BASE), name, fallback))
        self.assertEqual(bad, [])

    def test_every_converted_colour_has_a_dark_value(self):
        defined = set(re.findall(r'(--[btl]-[0-9a-f]{3,6}):', read('static', 'css', 'dark-literals.css')))
        used = set()
        for f in self.files():
            if f.endswith('dark-literals.css'):
                continue
            used |= set(re.findall(r'var\((--[btl]-[0-9a-f]{3,6}),', open(f, encoding='utf8', errors='ignore').read()))
        self.assertTrue(used)
        self.assertEqual(sorted(used - defined), [])

    def test_the_conversion_never_nests_a_fallback_inside_a_fallback(self):
        for f in self.files():
            text = open(f, encoding='utf8', errors='ignore').read()
            self.assertNotRegex(text, r'var\(--[btl]-[0-9a-f]+, var\(--[btl]-', os.path.relpath(f, BASE))
            self.assertNotIn('var(--mix-black, var(--mix-black', text, os.path.relpath(f, BASE))

    def test_the_light_values_of_the_main_variables_are_what_they_were(self):
        base = read('templates', 'base.html')
        for name, value in (('--surface-card', '#ffffff'), ('--surface-page', 'var(--brand-surface)'), ('--brand-primary', '#6e95b2'), ('--brand-slate', '#64768a'), ('--ink-primary', 'var(--brand-ink)'),
                            ('--status-critical', '#d03b3b'), ('--status-warning', '#fab219'), ('--status-good', '#0ca30c'), ('--priority-urgent', '#f2790a'), ('--accent-contrast', '#8c4570')):
            self.assertRegex(base, re.escape(name) + r':\s*' + re.escape(value), name)

    def test_the_old_hardcoded_white_backgrounds_are_now_the_themes_card_colour(self):
        css = read('static', 'css', 'mobile-tickets.css') + read('workspace', 'templates', 'workspace', '_css.html')
        self.assertNotRegex(css, r'background(-color)?:\s*#fff\b')
        self.assertIn('var(--surface-card)', css)
