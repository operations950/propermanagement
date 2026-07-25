import logging

from django.conf import settings

from core.models import Contact, Property

logger = logging.getLogger(__name__)

MODEL = 'claude-sonnet-5'

CLASSIFY_CONTACT_TOOL = {
    'name': 'classify_contact',
    'description': 'Classify a saved contact for a property management business, based on their actual message history.',
    'input_schema': {
        'type': 'object',
        'properties': {
            'contact_type': {
                'type': 'string',
                'enum': [c[0] for c in Contact.ContactType.choices],
                'description': (
                    'Best guess at what this person is to the business, based on how they talk and what '
                    'they discuss — owner (of a unit/association), board_member, association_member, '
                    'tenant, on_site_staff, vendor (an outside contractor/repair company), guest, or '
                    'other/staff_adjacent if genuinely unclear.'
                ),
            },
            'property_name': {
                'type': ['string', 'null'],
                'description': (
                    'Best-guess property name from the provided list, based on which property their '
                    'messages are actually about. Null if the messages never mention a specific property, '
                    'or if they clearly serve many properties rather than one — don\'t guess randomly.'
                ),
            },
            'trade': {
                'type': 'string',
                'description': (
                    'Only if contact_type is vendor: their trade (e.g. plumbing, HVAC, cleaning, '
                    'handyman, landscaping, pest control), inferred from what they discuss. Empty '
                    'string otherwise.'
                ),
            },
            'reasoning': {'type': 'string', 'description': 'One or two sentences explaining the verdict.'},
        },
        'required': ['contact_type', 'property_name', 'trade', 'reasoning'],
    },
}

CONTACT_PROMPT = """\
Here is the full available message history with one saved contact of a property management business. \
Read all of it, then classify who this person is and — if their messages point to one specific property \
— which one.

Known properties: {property_names}

Contact info on file: {contact_info}

--- Message history ---
{transcript}
--- End message history ---\
"""


class ContactVerdict:
    def __init__(self, contact_type, property_name, trade, reasoning):
        self.contact_type = contact_type
        self.property_name = property_name
        self.trade = trade
        self.reasoning = reasoning


def classify_contact(transcript, contact_info='', property_names=None):
    """Read a contact's Quo message history and guess their contact_type,
    likely property, and (if a vendor) trade — same tool-use pattern as
    thread_classifier.classify_thread, just classifying a PERSON instead of
    judging whether a conversation needs a ticket. Returns None (logged) on
    any failure — a bad guess is just a blank suggestion, never a reason to
    block the import review queue."""
    if not settings.ANTHROPIC_API_KEY:
        logger.info('ANTHROPIC_API_KEY not configured — skipping contact classification.')
        return None

    import anthropic

    if property_names is None:
        property_names = list(Property.objects.filter(is_active=True).values_list('name', flat=True))

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    try:
        message = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            tools=[CLASSIFY_CONTACT_TOOL],
            tool_choice={'type': 'tool', 'name': 'classify_contact'},
            messages=[{
                'role': 'user',
                'content': CONTACT_PROMPT.format(
                    property_names=', '.join(property_names) or '(none configured)',
                    contact_info=contact_info or '(none on file)',
                    transcript=transcript,
                ),
            }],
        )
    except Exception:
        # Broad on purpose: anthropic.APIError only covers errors the API itself returns — a
        # malformed request never even reaches the network (e.g. a UnicodeEncodeError building
        # headers from odd characters in imported data) and would otherwise propagate up and
        # kill the entire classify_pending_contacts batch instead of just skipping this one
        # contact. A bad guess is just a blank suggestion; a crashed batch loses every
        # candidate after the bad one.
        logger.exception('Claude API call failed during contact classification')
        return None

    tool_use = next((b for b in message.content if b.type == 'tool_use'), None)
    if tool_use is None:
        logger.warning('Claude did not return a tool_use block for contact classification.')
        return None

    data = tool_use.input
    try:
        return ContactVerdict(
            contact_type=data['contact_type'], property_name=data.get('property_name'),
            trade=data.get('trade', ''), reasoning=data.get('reasoning', ''),
        )
    except KeyError:
        logger.exception('Claude tool_use input missing expected fields: %r', data)
        return None
