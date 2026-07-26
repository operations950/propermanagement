import logging

from django.conf import settings

from core.models import Property

logger = logging.getLogger(__name__)

MODEL = 'claude-sonnet-5'

EXTRACT_BOOKING_TOOL = {
    'name': 'extract_airbnb_booking',
    'description': 'Extract structured reservation details from an Airbnb automated email.',
    'input_schema': {
        'type': 'object',
        'properties': {
            'is_booking_confirmation': {
                'type': 'boolean',
                'description': (
                    'True ONLY if this genuinely is a NEW reservation confirmation — a guest just '
                    "booked a stay. False for everything else Airbnb sends automatically: cancellations, "
                    'payout/payment notices, review requests, itinerary reminders, messages from a guest '
                    'relayed through Airbnb, or anything not actually confirming a brand-new booking.'
                ),
            },
            'confirmation_code': {
                'type': ['string', 'null'],
                'description': "Airbnb's reservation/confirmation code (e.g. HMXXXXXXXX). Null if not found.",
            },
            'check_in': {
                'type': ['string', 'null'],
                'description': 'Check-in date in YYYY-MM-DD format. Null if not found or not a real date.',
            },
            'check_out': {
                'type': ['string', 'null'],
                'description': 'Check-out date in YYYY-MM-DD format. Null if not found or not a real date.',
            },
            'property_name': {
                'type': ['string', 'null'],
                'description': (
                    'Best-guess match from the provided list of known properties, based on the listing '
                    "name/nickname/address mentioned in the email. Null if it can't be confidently "
                    "matched to one of the listed properties — don't guess randomly."
                ),
            },
            'guest_name': {
                'type': ['string', 'null'],
                'description': "The guest's name as it appears in the email. Null if not found.",
            },
        },
        'required': [
            'is_booking_confirmation', 'confirmation_code', 'check_in', 'check_out', 'property_name', 'guest_name',
        ],
    },
}

BOOKING_PROMPT = """\
Here is an automated email from Airbnb, sent to a property management business's shared inbox. \
Determine whether it's a brand-new reservation confirmation (as opposed to a cancellation, payout \
notice, review request, or anything else Airbnb sends automatically), and if so extract the \
reservation details.

Known properties: {property_names}

--- Email ---
{email_text}
--- End email ---\
"""


class AirbnbBookingExtract:
    def __init__(self, is_booking_confirmation, confirmation_code, check_in, check_out, property_name, guest_name):
        self.is_booking_confirmation = is_booking_confirmation
        self.confirmation_code = confirmation_code
        self.check_in = check_in
        self.check_out = check_out
        self.property_name = property_name
        self.guest_name = guest_name


def extract_airbnb_booking(email_text: str) -> AirbnbBookingExtract | None:
    """Read one Airbnb automated email and decide whether it's a new
    booking confirmation, extracting the reservation details if so.
    Returns None (safe no-op, logged) if ANTHROPIC_API_KEY isn't configured
    yet, or if Claude's response couldn't be parsed — the caller treats
    that the same as thread_classifier.classify_thread returning None
    (retry next poll, not a false negative)."""
    if not settings.ANTHROPIC_API_KEY:
        logger.info('ANTHROPIC_API_KEY not configured — skipping Airbnb booking extraction.')
        return None

    import anthropic  # imported lazily so the package is only required once a key is configured

    from core.app_settings import sanitized_setting

    property_names = list(Property.objects.filter(is_active=True).values_list('name', flat=True))

    client = anthropic.Anthropic(api_key=sanitized_setting('ANTHROPIC_API_KEY'))
    try:
        message = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            tools=[EXTRACT_BOOKING_TOOL],
            tool_choice={'type': 'tool', 'name': 'extract_airbnb_booking'},
            messages=[{
                'role': 'user',
                'content': BOOKING_PROMPT.format(
                    property_names=', '.join(property_names) or '(none configured)',
                    email_text=email_text,
                ),
            }],
        )
    except Exception:
        # Broad on purpose — see intake/contact_classifier.py's identical comment. A malformed
        # request never reaches the network and isn't an anthropic.APIError, so narrowly
        # catching that alone lets it propagate and crash the caller.
        logger.exception('Claude API call failed during Airbnb booking extraction')
        return None

    tool_use = next((b for b in message.content if b.type == 'tool_use'), None)
    if tool_use is None:
        logger.warning('Claude did not return a tool_use block for Airbnb booking extraction.')
        return None

    data = tool_use.input
    try:
        return AirbnbBookingExtract(
            is_booking_confirmation=data['is_booking_confirmation'],
            confirmation_code=data.get('confirmation_code'),
            check_in=data.get('check_in'),
            check_out=data.get('check_out'),
            property_name=data.get('property_name'),
            guest_name=data.get('guest_name'),
        )
    except KeyError:
        logger.exception('Claude tool_use input missing expected fields: %r', data)
        return None
