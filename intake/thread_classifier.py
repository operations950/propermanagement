import logging

from django.conf import settings

from core.models import Property, StaffProfile

logger = logging.getLogger(__name__)

MODEL = 'claude-sonnet-5'

CLASSIFY_TOOL = {
    'name': 'classify_thread',
    'description': 'Classify a conversation thread (SMS or email) for property-management task tracking.',
    'input_schema': {
        'type': 'object',
        'properties': {
            'actionable': {
                'type': 'boolean',
                'description': (
                    'True only if, as of the LAST message in the thread, staff genuinely still '
                    'need to do something (a maintenance issue, a supply shortage, etc). A problem '
                    'mentioned earlier that was resolved, clarified, or retracted later in the same '
                    'thread is NOT actionable.'
                ),
            },
            'already_resolved': {
                'type': 'boolean',
                'description': (
                    'True if the thread itself shows the issue was resolved, was a false alarm, or '
                    'the reporter said never mind, by the end of the conversation.'
                ),
            },
            'is_supply_request': {
                'type': 'boolean',
                'description': (
                    'True ONLY if this is purely a supply/inventory reorder (e.g. "we\'re out of '
                    'towels", "need more coffee pods") — these are routed to a supply order list, '
                    'not the ticket queue. False for everything else, including maintenance, vendor '
                    'quotes, billing, cleaning, and admin work.'
                ),
            },
            'priority': {'type': 'string', 'enum': ['low', 'medium', 'high', 'urgent']},
            'role': {
                'type': 'string',
                'enum': [c[0] for c in StaffProfile.Role.choices],
                'description': (
                    'Which department should own this — the primary classification for every ticket: '
                    'property_manager for tenant/board/admin/leasing matters, maintenance for repairs, '
                    'cleaner for cleaning requests, contractor for work needing an outside vendor, '
                    'accounting for billing/payments/dues, admin for internal account/system '
                    'administration. Best guess even if uncertain — staff can reassign.'
                ),
            },
            'property_name': {
                'type': ['string', 'null'],
                'description': (
                    'Best-guess property name from the provided list, based on context in the '
                    'conversation (an address, a nickname, prior mentions). Null if it genuinely '
                    "can't be determined from this thread — don't guess randomly."
                ),
            },
            'title': {
                'type': 'string',
                'description': (
                    'A SHORT, scannable headline — 4 to 8 words, NOT a full sentence, no trailing '
                    'period. Think newspaper headline, not a summary. Examples: "AC repair — Marielys '
                    'unit", "Reserve transfer to City National", "Bee removal quote — Grand/GLEN". '
                    'Include a specific identifying detail (unit, vendor, amount) when the thread has '
                    'one, so two tickets in the same department are still distinguishable at a glance.'
                ),
            },
            'summary': {
                'type': 'string',
                'description': (
                    'ONE short sentence (under 20 words) with the specific action needed. This is the '
                    'only context staff sees without opening the ticket, so be concrete, not vague — '
                    '"Send AC repair invoice to owner Carolina Schultz for payment" not "Follow up on '
                    'an invoice."'
                ),
            },
            'reasoning': {
                'type': 'string',
                'description': 'One or two sentences explaining the verdict, especially why something was or was not marked actionable.',
            },
        },
        'required': [
            'actionable', 'already_resolved', 'is_supply_request', 'priority', 'role', 'property_name', 'title',
            'summary', 'reasoning',
        ],
    },
}

THREAD_PROMPT = """\
Here is a full {source_label}, chronological, for a property management business. Read the ENTIRE \
thread before deciding anything — a problem mentioned early in the conversation may be resolved, \
clarified, or retracted later in the same thread. Only flag it as actionable if, as of the last \
message, staff genuinely still need to do something.

Staff scan a dashboard with dozens of these at once, so the title and summary must be short and \
concrete — no filler words, no restating "follow up on" for everything.

Known properties: {property_names}

--- Thread transcript ---
{transcript}
--- End transcript ---\
"""


class ThreadVerdict:
    def __init__(
        self, actionable, already_resolved, is_supply_request, priority, role, property_name, title, summary,
        reasoning,
    ):
        self.actionable = actionable
        self.already_resolved = already_resolved
        self.is_supply_request = is_supply_request
        self.priority = priority
        self.role = role
        self.property_name = property_name
        self.title = title
        self.summary = summary
        self.reasoning = reasoning


def classify_thread(
    transcript: str, source_label: str = 'phone-line conversation thread (SMS messages)',
) -> ThreadVerdict | None:
    """Read a full conversation thread (Quo SMS, Gmail email, ...) and
    decide whether it contains an actionable, still-open task — as opposed
    to e.g. a problem that got mentioned but was resolved or dismissed
    later in the same thread. `source_label` only affects the wording of
    the prompt (e.g. "email thread"); the classification schema is the
    same regardless of source.

    Returns None (safe no-op, logged) if ANTHROPIC_API_KEY isn't configured
    yet, or if Claude's response couldn't be parsed.
    """
    if not settings.ANTHROPIC_API_KEY:
        logger.info('ANTHROPIC_API_KEY not configured — skipping thread classification.')
        return None

    import anthropic  # imported lazily so the package is only required once a key is configured

    property_names = list(Property.objects.filter(is_active=True).values_list('name', flat=True))

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    try:
        message = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            tools=[CLASSIFY_TOOL],
            tool_choice={'type': 'tool', 'name': 'classify_thread'},
            messages=[{
                'role': 'user',
                'content': THREAD_PROMPT.format(
                    source_label=source_label,
                    property_names=', '.join(property_names) or '(none configured)',
                    transcript=transcript,
                ),
            }],
        )
    except anthropic.APIError:
        logger.exception('Claude API call failed during thread classification')
        return None

    tool_use = next((b for b in message.content if b.type == 'tool_use'), None)
    if tool_use is None:
        logger.warning('Claude did not return a tool_use block for thread classification.')
        return None

    data = tool_use.input
    try:
        return ThreadVerdict(
            actionable=data['actionable'],
            already_resolved=data['already_resolved'],
            is_supply_request=data['is_supply_request'],
            priority=data['priority'],
            role=data['role'],
            property_name=data.get('property_name'),
            title=data['title'],
            summary=data['summary'],
            reasoning=data.get('reasoning', ''),
        )
    except KeyError:
        logger.exception('Claude tool_use input missing expected fields: %r', data)
        return None
