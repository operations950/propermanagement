"""The installable web app: a manifest, a service worker and an offline page.

Staff (and owners) tap "Add to Home Screen" and the same site opens full screen from an icon. Nothing here caches a page: the service worker keeps only the site's
own static files and one small branded offline page, so what anyone sees is always live (and what an owner may see stays behind the same login as before).

The three addresses must answer for everyone, signed in or not, including an owner login (which is otherwise walled into /owner/) and an employee who has not signed
the time clock policy yet (who is otherwise sent to the policy). PwaFilesMiddleware therefore answers them before either wall.
"""
import json
import os

from django.conf import settings
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.templatetags.static import static
from django.urls import reverse
from django.views.decorators.http import require_safe

ACCENT = '#4e7690'            # the icon colour: the steel blue of the cleaner / property manager ramp in base.html
THEME = '#ffffff'             # the top bar is white, so the phone's status bar matches it
BACKGROUND = '#efedf3'        # the site's page colour, shown while the app opens
PATHS = ('/manifest.webmanifest', '/sw.js', '/offline/')
# One version for the cache and the worker file: a deploy changes it, so phones fetch the new worker and drop the old cache.
BUILD = (os.environ.get('RAILWAY_GIT_COMMIT_SHA') or os.environ.get('SOURCE_VERSION') or 'dev')[:8]
CACHE_NAME = f'proper-{BUILD}'

ICONS = [
    {'src': 'img/pwa-icon-192.png', 'sizes': '192x192', 'type': 'image/png', 'purpose': 'any'},
    {'src': 'img/pwa-icon-512.png', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any'},
    {'src': 'img/pwa-icon-maskable-512.png', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'maskable'},
]


def start_url(user, app=''):
    """Where the app opens: every staff member on the app menu (/m/: six big choices, the first screen of the phone app), an owner on the portal, anyone not signed in on
    the login page."""
    if app == 'owner':
        return reverse('owner_home')
    if not getattr(user, 'is_authenticated', False):
        return reverse('login')
    from . import owner_portal
    if owner_portal.is_owner_user(user):
        return reverse('owner_home')
    profile = getattr(user, 'staff_profile', None)
    if profile is None:
        return reverse('dashboard')
    return reverse('mobile_menu')


@require_safe
def manifest(request):
    app = 'owner' if request.GET.get('app') == 'owner' else ''
    owner = app == 'owner'
    body = {
        'id': '/owner/' if owner else '/',
        'name': 'Proper Realty Owner Portal' if owner else 'Proper Realty',
        'short_name': 'Owner' if owner else 'Proper',
        'description': 'Your rentals, statements and calendar.' if owner else 'Proper Realty: tickets, compliance, projects, on-site work, the time clock and the books.',
        'start_url': start_url(request.user, app),
        'scope': '/',
        'display': 'standalone',
        'display_override': ['standalone', 'minimal-ui'],
        'orientation': 'portrait-primary',
        'theme_color': THEME,
        'background_color': BACKGROUND,
        'lang': 'en-US',
        'categories': ['business', 'productivity'],
        'icons': [dict(i, src=static(i['src'])) for i in ICONS],
    }
    response = HttpResponse(json.dumps(body), content_type='application/manifest+json')
    response['Cache-Control'] = 'private, no-cache'
    response['Vary'] = 'Cookie'
    return response


SERVICE_WORKER = """/* Proper Realty service worker. __BUILD__
   It keeps the site's own static files (hashed, so they never go stale) and one offline page. It never stores a page, a form, an answer from the server or
   anything that depends on who is signed in: every page and every POST goes straight to the network. */
const CACHE = '__CACHE__';
const OFFLINE = '/offline/';
const LOCAL = ['localhost', '127.0.0.1', '[::1]'].indexOf(self.location.hostname) !== -1;

self.addEventListener('install', function (event) {
    event.waitUntil(caches.open(CACHE).then(function (cache) { return cache.add(new Request(OFFLINE, { cache: 'reload' })); }).then(function () { return self.skipWaiting(); }));
});

self.addEventListener('activate', function (event) {
    event.waitUntil(caches.keys().then(function (names) {
        return Promise.all(names.filter(function (n) { return n.indexOf('proper-') === 0 && n !== CACHE; }).map(function (n) { return caches.delete(n); }));
    }).then(function () { return self.clients.claim(); }));
});

self.addEventListener('fetch', function (event) {
    const request = event.request;
    if (request.method !== 'GET') return;                              // a form, a tick, a clock-in: always the network
    const url = new URL(request.url);
    if (url.origin !== self.location.origin) return;                   // fonts and scripts from other hosts are the browser's business

    if (request.mode === 'navigate') {                                 // a page: the network, or the offline page when there is none. Never cached.
        event.respondWith(fetch(request).catch(function () { return caches.match(OFFLINE); }));
        return;
    }
    if (!LOCAL && url.pathname.indexOf('/static/') === 0) {            // static files carry a hash of their content in their name: keep the first copy
        event.respondWith(caches.open(CACHE).then(function (cache) {
            return cache.match(request).then(function (hit) {
                if (hit) return hit;
                return fetch(request).then(function (response) {
                    if (response && response.status === 200 && response.type === 'basic') cache.put(request, response.clone());
                    return response;
                });
            });
        }));
    }
});
"""


@require_safe
def service_worker(request):
    body = SERVICE_WORKER.replace('__CACHE__', CACHE_NAME).replace('__BUILD__', f'build {BUILD}')
    response = HttpResponse(body, content_type='application/javascript; charset=utf-8')
    response['Service-Worker-Allowed'] = '/'
    response['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return response


@require_safe
def offline(request):
    """A small self-contained page (no menu, no login needed) shown when a page cannot be fetched because there is no connection."""
    response = HttpResponse(render_to_string('offline.html', {'accent': ACCENT}), content_type='text/html; charset=utf-8')
    response['Cache-Control'] = 'no-cache'
    return response


class PwaFilesMiddleware:
    """Answers the manifest, the worker and the offline page before the owner wall and the policy wall can redirect them (both walls send other requests away)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method in ('GET', 'HEAD') and request.path in PATHS:
            return {'/manifest.webmanifest': manifest, '/sw.js': service_worker, '/offline/': offline}[request.path](request)
        return self.get_response(request)
