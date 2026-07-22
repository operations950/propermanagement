from django.conf import settings

from .base import IntakeAdapter, RawEvent


class AirbnbAdapter(IntakeAdapter):
    """Reads booking/cancellation/guest-message activity from Airbnb.

    TODO once credentials exist: set AIRBNB_API_KEY in .env, then implement
    pull() against Airbnb's reservations/messaging API. Emit a 'booking'
    RawEvent per new confirmed reservation (external_id = Airbnb's
    confirmation code — this becomes Reservation.external_reservation_id,
    the natural key the classifier uses), a 'cancellation' RawEvent when a
    reservation is cancelled (same external_id, so the classifier can find
    and soft-cancel the linked cleaning ticket), and 'maintenance'/
    'shortage'/'generic' events for guest messages, same as the other
    adapters.
    """

    def pull(self) -> list[RawEvent]:
        if not settings.AIRBNB_API_KEY:
            return []
        raise NotImplementedError('Airbnb adapter not wired up yet — set AIRBNB_API_KEY and implement pull().')
