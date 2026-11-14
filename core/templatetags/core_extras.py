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
def same_month_settle(totals):
    """The settlement figures for a rental paid the month it is collected (see ledger.same_month_settlement)."""
    from core import ledger
    return ledger.same_month_settlement(totals)


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


@register.filter
def mdate(value):
    """A date as "Aug 8" — from a date, or from the ISO string a closed month's frozen reconciliation keeps."""
    from datetime import date as _date
    if isinstance(value, str):
        try:
            value = _date.fromisoformat(value)
        except ValueError:
            return value
    if not value:
        return ''
    return f'{value:%b} {value.day}'


@register.filter
def unaccepted(items):
    """The reconciliation items nobody has explained yet."""
    return [i for i in items if not i.get('accepted')]


@register.filter
def accepted_any(items):
    return any(i.get('accepted') for i in items)


@register.filter
def tel_href(phone):
    """A tel:/sms: friendly number from however it was typed: digits only, keeping a leading +."""
    raw = str(phone or '').strip()
    digits = ''.join(ch for ch in raw if ch.isdigit())
    if not digits:
        return ''
    return ('+' if raw.startswith('+') else '') + digits


@register.filter
def maps_href(address):
    """A link that opens an address in the phone's maps app (or Google Maps in a browser)."""
    from urllib.parse import quote_plus
    address = str(address or '').strip()
    return f'https://www.google.com/maps/search/?api=1&query={quote_plus(address)}' if address else ''


# who to offer first when a property page has a Call button: on-site people, then the board, then owners, then anyone else with a phone number
CALL_ORDER = ['on_site_staff', 'board_member', 'association_member', 'owner', 'tenant', 'staff_adjacent', 'vendor', 'lead', 'guest', 'other']


@register.simple_tag
def property_call_contact(prop):
    """The one contact of this property to offer as a Call button (the first with a phone number, by the order above), or None."""
    rank = {k: i for i, k in enumerate(CALL_ORDER)}
    people = [c for c in prop.contacts.all() if (c.phone or '').strip()]
    if not people:
        return None
    return sorted(people, key=lambda c: (rank.get(c.contact_type, 99), c.name.lower()))[0]
