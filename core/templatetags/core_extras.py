from django import template

register = template.Library()

# Lucide icon names (see templates/base.html's lucide script) — one per
# Property.Type, shown to the left of the name on the properties list so
# the 6 kinds of property are visually distinguishable at a glance.
PROPERTY_TYPE_ICONS = {
    'general': 'circle-dot',
    'association': 'users',
    'str': 'bed',
    'ltr': 'key',
    'snowbird': 'eye',
    'commercial': 'store',
}


@register.filter
def property_type_icon(property_type):
    return PROPERTY_TYPE_ICONS.get(property_type, 'circle-dot')


@register.filter
def money(value):
    """A dollar amount with its sign in front of the dollar sign: $1,234.56 or −$1,234.56 (blank for none)."""
    from decimal import Decimal, InvalidOperation
    if value is None or value == "":
        return ""
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return ""
    return ("−" if amount < 0 else "") + f"${abs(amount):,.2f}"


@register.filter
def dictget(mapping, key):
    """mapping[key] in a template (an empty list when it is not there)."""
    try:
        return mapping.get(key, [])
    except AttributeError:
        return []
