import logging

from django.conf import settings

logger = logging.getLogger(__name__)

MODEL = 'claude-sonnet-5'

# Below this many messages, a conversation is short enough that it's almost
# certainly all about the current issue anyway — not worth an API call.
MIN_MESSAGES = 12
MAX_MESSAGES = 200

RELEVANCE_TOOL = {
    'name': 'mark_relevant_messages',
    'description': (
        "Given a specific maintenance ticket and the full SMS history with the contact assigned to it, "
        "identify which messages actually discuss this ticket's issue — separating them from older/stale "
        "or concurrent, unrelated conversation with the same person (e.g. a handyman juggling several "
        "jobs for the same company at once)."
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'related_ids': {
                'type': 'array', 'items': {'type': 'integer'},
                'description': (
                    "IDs of every message substantively about this ticket's issue — the initial report, "
                    'clarifying questions, scheduling, updates, or completion. Exclude messages about a '
                    'different job, small talk, or an old already-resolved issue.'
                ),
            },
        },
        'required': ['related_ids'],
    },
}

PROMPT = """\
A property management company is viewing one specific maintenance ticket's conversation with a contact. \
The same phone conversation may also cover other, unrelated jobs or old resolved issues — mark which \
messages are actually about THIS ticket.

Ticket: {title}
Kind: {kind}
Description: {description}

--- Full message history with this contact ---
{transcript}
--- End message history ---\
"""


class RelevanceVerdict:
    def __init__(self, related_ids):
        self.related_ids = set(related_ids)


def classify_message_relevance(ticket, messages):
    """messages: a list of intake.models.QuoMessage instances (already
    ordered), each with a real .pk. Returns a RelevanceVerdict (whose
    related_ids is the subset judged on-topic for this ticket) or None on
    any failure/skip — callers must treat None as "show everything
    normally," never as "hide everything." Never run for a short thread
    (MIN_MESSAGES) — not enough history for staleness to be a real problem,
    and not worth the API call."""
    if len(messages) < MIN_MESSAGES:
        return None
    if not settings.ANTHROPIC_API_KEY:
        return None

    import anthropic

    windowed = messages[-MAX_MESSAGES:]
    lines = []
    if len(messages) > len(windowed):
        lines.append(f'[Showing the most recent {len(windowed)} of {len(messages)} total messages.]')
        lines.append('')
    for m in windowed:
        speaker = 'Staff (company line)' if m.direction == m.Direction.OUT else (m.from_number or 'Contact')
        timestamp = m.quo_created_at.isoformat() if m.quo_created_at else ''
        lines.append(f'[id={m.pk}] [{timestamp}] {speaker}: {m.body}')
    transcript = '\n'.join(lines)

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    try:
        message = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            tools=[RELEVANCE_TOOL],
            tool_choice={'type': 'tool', 'name': 'mark_relevant_messages'},
            messages=[{
                'role': 'user',
                'content': PROMPT.format(
                    title=ticket.title, kind=ticket.kind or '(none)',
                    description=ticket.description or '(none)', transcript=transcript,
                ),
            }],
        )
    except anthropic.APIError:
        logger.exception('Claude API call failed during message relevance classification')
        return None

    tool_use = next((b for b in message.content if b.type == 'tool_use'), None)
    if tool_use is None:
        logger.warning('Claude did not return a tool_use block for message relevance classification.')
        return None

    try:
        return RelevanceVerdict(related_ids=tool_use.input['related_ids'])
    except KeyError:
        logger.exception('Claude tool_use input missing expected fields: %r', tool_use.input)
        return None
