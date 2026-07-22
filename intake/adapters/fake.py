from .base import IntakeAdapter, RawEvent


class FakeAdapter(IntakeAdapter):
    """Simulates inbound events from every reactive source so the full
    pipeline (event -> ticket/reservation/supply request -> assignment ->
    follow-up) can be demonstrated end-to-end before real credentials for
    Gmail/Quo/Calendar/Airbnb/VRBO exist.

    Deliberately returns the same fixed events every call (stable
    external_id per event) so repeated polling is idempotent — see
    intake/classifier.py's get_or_create usage — rather than manufacturing
    a new random event each run.
    """

    def pull(self) -> list[RawEvent]:
        return [
            RawEvent(
                event_type='booking',
                source='fake',
                external_id='FAKE-RES-2001',
                property_name='Sunset Villa',
                reporter_name='Jamie Guest',
                reporter_email='jamie.guest@example.com',
                check_in='2026-08-01',
                check_out='2026-08-05',
            ),
            RawEvent(
                event_type='booking',
                source='fake',
                external_id='FAKE-RES-2002',
                property_name='Lakeside Cabin',
                reporter_name='Morgan Guest',
                reporter_email='morgan.guest@example.com',
                check_in='2026-07-10',
                check_out='2026-07-14',
            ),
            RawEvent(
                event_type='cancellation',
                source='fake',
                external_id='FAKE-RES-2002',
                property_name='Lakeside Cabin',
            ),
            RawEvent(
                event_type='maintenance',
                source='fake',
                external_id='FAKE-MSG-3001',
                property_name='Sunset Villa',
                title='Leaking kitchen faucet',
                body="The kitchen faucet has been dripping constantly since we checked in.",
                reporter_name='Jamie Guest',
                reporter_email='jamie.guest@example.com',
                reporter_phone='555-0101',
            ),
            RawEvent(
                event_type='shortage',
                source='fake',
                external_id='FAKE-MSG-3002',
                property_name='Sunset Villa',
                title='Supply request',
                body="Hey, we're out of toilet paper and running low on paper towels, could someone bring more?",
            ),
        ]
