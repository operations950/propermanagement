from django.utils import timezone


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
