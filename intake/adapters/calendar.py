from django.conf import settings

from .base import IntakeAdapter, RawEvent


class GoogleCalendarAdapter(IntakeAdapter):
    """Reads the shared Google Calendar for events that should spawn tasks
    (e.g. a manually-added "deep clean unit 4" calendar entry).

    TODO once credentials exist: set GOOGLE_CALENDAR_CREDENTIALS_PATH in
    .env, then implement pull() with the Google Calendar API's
    events.list(updatedMin=<last poll>) on the shared calendar's id.
    """

    def pull(self) -> list[RawEvent]:
        if not settings.GOOGLE_CALENDAR_CREDENTIALS_PATH:
            return []
        raise NotImplementedError(
            'Calendar adapter not wired up yet — set GOOGLE_CALENDAR_CREDENTIALS_PATH and implement pull().'
        )
