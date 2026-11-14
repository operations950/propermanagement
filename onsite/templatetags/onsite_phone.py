"""Small text filters for the compact phone visit card (onsite/_phone_visit_card.html): a short name for the kind of visit and for who has it, so a 340px-wide card says more and cuts less."""
from django import template

register = template.Library()

_ENDINGS = (' Cleaning', ' Clean', ' Visit')


@register.filter
def short_type(value):
    """'Turnover Clean' -> 'Turnover', 'Normal Cleaning' -> 'Normal', 'Commercial Clean' -> 'Commercial'; a name that would be left empty (or has none of those endings) is unchanged."""
    text = str(value or '').strip()
    for ending in _ENDINGS:
        if text.endswith(ending) and len(text) > len(ending):
            return text[:-len(ending)]
    return text


@register.filter
def first_name(value):
    """'Charlotte Kim' -> 'Charlotte'; 'Unassigned' stays; 'ABC Cleaning (external)' -> 'ABC (external)'."""
    text = str(value or '').strip()
    if not text or text == 'Unassigned':
        return text
    external = text.endswith('(external)')
    if external:
        text = text[:-len('(external)')].strip()
    word = text.split()[0] if text.split() else text
    return f'{word} (external)' if external else word
