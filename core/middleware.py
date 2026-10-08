from django.utils import timezone
from django.utils.cache import add_never_cache_headers


class NoStoreHtmlMiddleware:
    """Adds Cache-Control: no-cache, no-store (plus Expires) to every HTML
    response that doesn't already set its own Cache-Control. Intuit's
    QuickBooks security requirements: "Caching is disabled on all SSL pages
    and all pages that contain sensitive data by using value no-cache and
    no-store." Django adds none of this by default for a normal view (only
    a few built-ins like LoginView set it themselves), so without this a
    browser or shared proxy is free to keep a copy of a page full of
    ticket/contact/financial data.

    HTML only — static files never reach this (WhiteNoise answers them
    before the rest of the chain runs), and uploaded photos/PDFs served
    through the media route have their own non-HTML content types, so
    they keep normal caching."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if not response.has_header('Cache-Control') and response.get('Content-Type', '').startswith('text/html'):
            add_never_cache_headers(response)
        return response


class TimezoneMiddleware:
    """Activates the logged-in user's StaffProfile.timezone for this
    request's thread, overriding settings.TIME_ZONE for every
    timezone.localtime() call the request touches (ticket due dates,
    calendar events, message timestamps) — not just calendar rendering.
    A no-op for anonymous users or accounts with no StaffProfile yet."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tz = getattr(getattr(request.user, 'staff_profile', None), 'timezone', None)
        if tz:
            timezone.activate(tz)
        else:
            timezone.deactivate()
        return self.get_response(request)


class RequestMemoMiddleware:
    """Opens a core.memo scope around every GET/HEAD request (a page load never changes data, so what it reads once it can reuse).
    Anything else - a POST, which saves and redirects - runs with no memo at all."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method not in ('GET', 'HEAD'):
            return self.get_response(request)
        from . import memo
        memo.begin()
        try:
            return self.get_response(request)
        finally:
            memo.end()


class RequestTimingMiddleware:
    """Times every request and counts its database queries. A request slower than SLOW_REQUEST_MS (or with more than
    SLOW_REQUEST_QUERIES queries) is logged to the Railway log as one line - method, path, seconds, queries, database seconds,
    status, user - so which pages are slow, and why, is read straight from the log instead of guessed. Every response also
    carries a Server-Timing header (the browser's Network tab shows it under Timing). Static files never reach this."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        import logging
        import time

        from django.conf import settings
        from django.db import connection

        count = [0]
        db = [0.0]

        def wrapper(execute, sql, params, many, context):
            started = time.perf_counter()
            try:
                return execute(sql, params, many, context)
            finally:
                count[0] += 1
                db[0] += time.perf_counter() - started

        started = time.perf_counter()
        with connection.execute_wrapper(wrapper):
            response = self.get_response(request)
        total = time.perf_counter() - started
        response['Server-Timing'] = f'app;dur={total * 1000:.0f}, db;dur={db[0] * 1000:.0f};desc="{count[0]} queries"'
        if total * 1000 >= settings.SLOW_REQUEST_MS or count[0] >= settings.SLOW_REQUEST_QUERIES:
            user = getattr(getattr(request, 'user', None), 'username', '') or '-'
            logging.getLogger('proptasks.perf').warning(
                'SLOW %s %s %.2fs  %d queries (db %.2fs)  status=%s  user=%s', request.method, request.get_full_path()[:200], total, count[0], db[0], response.status_code, user)
        return response


class OwnerWallMiddleware:
    """An owner's login can open the owner portal and nothing else. Every staff page, the admin, the APIs: a signed-in owner is sent to the portal (or
    refused, for anything that is not a plain page view). It is a default-deny rule applied here to every request, not a check each page remembers to
    make - many staff pages only ask "is someone logged in?", so without this an owner could browse tickets, contacts and every property."""
    ALLOWED_PREFIXES = ('/owner/',)

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from . import owner_portal
        if owner_portal.is_owner_user(request.user) and not request.path.startswith(self.ALLOWED_PREFIXES):
            from django.http import HttpResponseForbidden
            from django.shortcuts import redirect
            if request.method in ('GET', 'HEAD'):
                return redirect('owner_home')
            return HttpResponseForbidden('This area is not available to owner logins.')
        return self.get_response(request)
