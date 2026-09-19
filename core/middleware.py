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
