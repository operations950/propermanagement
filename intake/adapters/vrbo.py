from django.conf import settings

from .base import IntakeAdapter, RawEvent


class VrboAdapter(IntakeAdapter):
    """Reads booking/cancellation/guest-message activity from VRBO.

    TODO once credentials exist: set VRBO_API_KEY in .env, then implement
    pull() the same way as AirbnbAdapter — 'booking'/'cancellation' events
    keyed by VRBO's stable reservation id, plus 'maintenance'/'shortage'/
    'generic' events for guest messages.
    """

    def pull(self) -> list[RawEvent]:
        if not settings.VRBO_API_KEY:
            return []
        raise NotImplementedError('VRBO adapter not wired up yet — set VRBO_API_KEY and implement pull().')
