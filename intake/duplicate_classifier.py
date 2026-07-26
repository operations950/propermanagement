import logging

from django.conf import settings

logger = logging.getLogger(__name__)

MODEL = 'claude-sonnet-5'

# How many of the most recent still-open tickets at a property to show
# Claude as candidates — bounded so a busy property's prompt doesn't grow
# unbounded and so token cost stays predictable per intake event.
MAX_CANDIDATES = 15

FIND_DUPLICATE_TOOL = {
    'name': 'find_duplicate_ticket',
    'description': (
        'Decide whether a brand-new ticket candidate is almost certainly reporting the exact same '
        'real-world issue as one of the already-open tickets at the same property, as opposed to a '
        'coincidentally similar but genuinely separate issue.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'is_duplicate': {
                'type': 'boolean',
                'description': (
                    'True ONLY if you are confident the new ticket and one specific candidate are the '
                    'same real-world incident or request — e.g. the same guest\'s same lost item still '
                    'being discussed in a fresh conversation thread, or the same repair request '
                    'reported again before anyone has acted on it. Two different guests independently '
                    'reporting a similar-sounding problem (e.g. two different lost AirPods, two '
                    'different AC complaints on different dates) is NOT a duplicate — property '
                    'managers handle many similar but distinct requests. When genuinely unsure, prefer '
                    'False — a human reviews every ticket anyway, but a wrong True wastes their time '
                    'more than a missed match does.'
                ),
            },
            'duplicate_ticket_id': {
                'type': ['integer', 'null'],
                'description': 'The id of the one matching candidate ticket, or null if is_duplicate is false.',
            },
            'reasoning': {
                'type': 'string',
                'description': 'One or two sentences a staff member will read to quickly confirm or reject this match.',
            },
        },
        'required': ['is_duplicate', 'duplicate_ticket_id', 'reasoning'],
    },
}

DUPLICATE_PROMPT = """\
A property management system just received a new ticket candidate. Before it's created as a real \
ticket, check whether it's actually the same real-world issue as one already sitting open at the \
same property — as opposed to a different issue that just happens to sound similar.

Property: {property_name}

--- New ticket candidate ---
Title: {new_title}
Summary: {new_summary}
--- End new ticket candidate ---

--- Already-open tickets at this property ---
{candidates}
--- End already-open tickets ---\
"""


class DuplicateVerdict:
    def __init__(self, is_duplicate, duplicate_ticket_id, reasoning):
        self.is_duplicate = is_duplicate
        self.duplicate_ticket_id = duplicate_ticket_id
        self.reasoning = reasoning


def _format_candidates(candidates):
    lines = []
    for c in candidates:
        lines.append(f'[id={c.pk}] {c.title} — {c.description or "(no description)"}')
    return '\n'.join(lines)


def find_duplicate_ticket(property_obj, candidates, new_title, new_summary) -> DuplicateVerdict | None:
    """Ask Claude whether a new ticket candidate (not yet saved) is the same
    real-world issue as one of `candidates` — already-open tickets at the
    same property. Returns None (safe no-op, logged) if ANTHROPIC_API_KEY
    isn't configured, `candidates` is empty, or the response can't be
    parsed — callers should treat None exactly like "not a duplicate"."""
    if not candidates:
        return None
    if not settings.ANTHROPIC_API_KEY:
        logger.info('ANTHROPIC_API_KEY not configured — skipping duplicate-ticket check.')
        return None

    import anthropic  # imported lazily so the package is only required once a key is configured

    from core.app_settings import sanitized_setting

    client = anthropic.Anthropic(api_key=sanitized_setting('ANTHROPIC_API_KEY'))
    try:
        message = client.messages.create(
            model=MODEL,
            max_tokens=512,
            tools=[FIND_DUPLICATE_TOOL],
            tool_choice={'type': 'tool', 'name': 'find_duplicate_ticket'},
            messages=[{
                'role': 'user',
                'content': DUPLICATE_PROMPT.format(
                    property_name=property_obj.name if property_obj else '(unknown)',
                    new_title=new_title,
                    new_summary=new_summary or '(no summary)',
                    candidates=_format_candidates(candidates),
                ),
            }],
        )
    except Exception:
        # Broad on purpose — see intake/thread_classifier.py's identical comment.
        logger.exception('Claude API call failed during duplicate-ticket check')
        return None

    tool_use = next((b for b in message.content if b.type == 'tool_use'), None)
    if tool_use is None:
        logger.warning('Claude did not return a tool_use block for duplicate-ticket check.')
        return None

    data = tool_use.input
    try:
        return DuplicateVerdict(
            is_duplicate=data['is_duplicate'],
            duplicate_ticket_id=data.get('duplicate_ticket_id'),
            reasoning=data.get('reasoning', ''),
        )
    except KeyError:
        logger.exception('Claude tool_use input missing expected fields: %r', data)
        return None
