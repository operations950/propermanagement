"""What the property page shows first on a phone, by kind of property.

`{% property_phone as pp %}` (in core/property_detail.html) reads the page's own context (the property, its units, contacts, open tickets, trash schedule) and returns the sections of the phone
screen in the order that matters for that kind of property: access codes, main contacts, open tickets, units (and owners), trash. A kind of property that has nothing for a section simply has
no section. Everything else on the page stays available under "More". Nothing here saves or changes anything."""
from django import template
from django.urls import reverse
from django.utils import timezone

from core.models import Contact, Property
from core.templatetags.core_extras import tel_href

register = template.Library()

T = Contact.ContactType
# which people count as "main contacts", in the order to list them, and how many to show before "All contacts"
MAIN_CONTACTS = {
    Property.Type.SHORT_TERM_RENTAL: ([T.OWNER, T.ON_SITE_STAFF, T.STAFF_ADJACENT, T.TENANT, T.VENDOR, T.OTHER], 6),
    Property.Type.ASSOCIATION: ([T.ON_SITE_STAFF, T.BOARD_MEMBER, T.STAFF_ADJACENT, T.VENDOR], 8),      # owners are listed under Units and owners, not here
    Property.Type.LONG_TERM_RENTAL: ([T.TENANT, T.OWNER, T.ON_SITE_STAFF, T.STAFF_ADJACENT, T.VENDOR, T.OTHER], 6),
    Property.Type.COMMERCIAL: ([T.TENANT, T.OWNER, T.ON_SITE_STAFF, T.STAFF_ADJACENT, T.VENDOR, T.OTHER], 6),
}
MAIN_CONTACTS_DEFAULT = ([T.ON_SITE_STAFF, T.OWNER, T.TENANT, T.BOARD_MEMBER, T.STAFF_ADJACENT, T.VENDOR, T.OTHER], 6)
# the order of the sections for each kind
ORDER = {
    Property.Type.SHORT_TERM_RENTAL: ['access', 'contacts', 'tickets', 'units', 'trash'],
    Property.Type.ASSOCIATION: ['access', 'contacts', 'tickets', 'units', 'trash'],
    Property.Type.LONG_TERM_RENTAL: ['contacts', 'access', 'tickets', 'units', 'trash'],
    Property.Type.COMMERCIAL: ['contacts', 'access', 'tickets', 'units', 'trash'],
}
ORDER_DEFAULT = ['contacts', 'access', 'tickets', 'units', 'trash']
ICONS = {'access': 'key-round', 'contacts': 'contact', 'tickets': 'ticket', 'units': 'door-open', 'trash': 'trash-2'}
UNIT_ROWS = 10
OPEN_BY_DEFAULT = 2


def _quo(kind, phone):
    """The Quo app link for a number, or '' (see core/quo_links.py); never lets a missing or changed helper take the page down."""
    try:
        from core import quo_links
        return (quo_links.dial_url(phone) if kind == 'dial' else quo_links.message_url(phone)) or ''
    except Exception:
        return ''


@register.filter
def pp_quo_dial(phone):
    return _quo('dial', phone)


@register.filter
def pp_quo_text(phone):
    return _quo('text', phone)


def _row(label, value, copy=True):
    value = (value or '').strip() if isinstance(value, str) else value
    return {'label': label, 'value': value, 'copy': copy} if value else None


def _rows(items):
    return [r for r in (_row(*i) if len(i) == 2 else _row(i[0], i[1], i[2]) for i in items) if r]


def _access(prop, units, context):
    groups = []
    own = _rows([('Gate code', prop.gate_code), ('Door code', prop.door_code), ('Lockbox code', prop.lockbox_code), ('Alarm code', prop.alarm_code),
                 ('Wi-Fi network', prop.wifi_network), ('Wi-Fi password', prop.wifi_password)])
    shutoffs = [{'name': loc.system_name, 'where': loc.location + (f' ({loc.notes})' if loc.notes else '')} for loc in (context.get('system_locations') or [])]
    notes = (prop.access_notes or '').strip()
    if own or notes or shutoffs:
        groups.append({'title': 'Whole building' if units else '', 'rows': own, 'notes': notes, 'shutoffs': shutoffs})
    for u in units:
        rows = _rows([('Door code', u.access_code), ('Lockbox code', u.lockbox_code), ('Alarm code', u.alarm_code), ('Wi-Fi network', u.wifi_network), ('Wi-Fi password', u.wifi_password)])
        note = (u.access_notes or '').strip()
        if rows or note:
            groups.append({'title': u.label, 'rows': rows, 'notes': note, 'shutoffs': []})
    if not groups:
        return None
    if len(groups) == 1:
        groups[0]['title'] = ''                                           # "Whole building" only means something next to a unit's own codes
    n = sum(len(g['rows']) for g in groups)
    hint = f'{n} item{"" if n == 1 else "s"}' if n else ''
    return {'key': 'access', 'title': 'Access', 'groups': groups, 'hint': hint, 'edit': '#building'}


def _contacts(prop, contacts):
    types, cap = MAIN_CONTACTS.get(prop.property_type, MAIN_CONTACTS_DEFAULT)
    rank = {t: i for i, t in enumerate(types)}
    labels = dict(Contact.ContactType.choices)
    picked = sorted((c for c in contacts if c.contact_type in rank), key=lambda c: (rank[c.contact_type], c.name.lower()))
    rows = []
    for c in picked[:cap]:
        phone = (c.phone or '').strip()
        rows.append({'pk': c.pk, 'name': c.name, 'role': labels.get(c.contact_type, ''), 'trade': c.trade, 'phone': phone, 'tel': tel_href(phone) if phone else '',
                     'dial': _quo('dial', phone) if phone else '', 'text': _quo('text', phone) if phone else '', 'email': (c.email or '').strip()})
    if not rows:
        return None
    return {'key': 'contacts', 'title': 'Main contacts', 'rows': rows, 'more': max(0, len(contacts) - len(rows)), 'total': len(contacts), 'hint': str(len(rows)), 'edit': '#contacts'}


def _tickets(prop, open_tickets):
    tickets = list(open_tickets)
    if not tickets:
        return None
    now = timezone.now()
    rows = []
    for t in tickets[:5]:
        rows.append({'pk': t.pk, 'title': t.title, 'urgent': t.priority == 'urgent', 'due': t.due_date, 'late': bool(t.due_date and t.due_date < now)})
    return {'key': 'tickets', 'title': 'Open tickets', 'rows': rows, 'total': len(tickets), 'more': max(0, len(tickets) - len(rows)), 'hint': f'{len(tickets)} open',
            'all_url': reverse('ticket_list') + f'?property={prop.pk}'}


def _size(obj, fallback=None):
    """'2 bd · 2 ba · 1,100 sq ft' from whatever is filled in on a unit (or on the property when the unit leaves it blank)."""
    def pick(field):
        value = getattr(obj, field, None)
        return value if value is not None else (getattr(fallback, field, None) if fallback is not None else None)
    bits = []
    beds, baths, sqft = pick('bedroom_count'), pick('bathroom_count'), pick('square_footage')
    if beds is not None:
        bits.append(f'{beds} bd')
    if baths is not None:
        bits.append(f'{float(baths):g} ba')
    if sqft:
        bits.append(f'{sqft:,} sq ft')
    return ' · '.join(bits)


def _units(prop, units, context):
    ptype = prop.property_type
    units = [u for u in units if u.is_active]
    if not units:
        if ptype == Property.Type.SHORT_TERM_RENTAL:
            size = _size(prop)
            if size:
                return {'key': 'units', 'title': 'Size', 'rows': [{'label': prop.name, 'line': size}], 'more': 0, 'hint': size, 'edit': '#units'}
        return None
    owners_by_unit, tenants_by_unit = {}, {}
    if ptype == Property.Type.ASSOCIATION and context.get('ap'):          # who owns what is shown to the people who see the association's accounting card
        from accounting.models import Ownership
        for o in Ownership.objects.filter(unit__property=prop, end_date__isnull=True).select_related('person').order_by('-is_billed', 'person__name'):
            owners_by_unit.setdefault(o.unit_id, []).append(o.person.name)
    elif ptype in (Property.Type.LONG_TERM_RENTAL, Property.Type.COMMERCIAL):
        for c in Contact.objects.filter(properties=prop, contact_type=Contact.ContactType.TENANT).prefetch_related('units'):
            for u in c.units.all():
                tenants_by_unit.setdefault(u.pk, []).append(c.name)
    rows = []
    for u in units[:UNIT_ROWS]:
        if ptype == Property.Type.ASSOCIATION:
            line = ', '.join(owners_by_unit.get(u.pk, [])[:2])
        elif ptype in (Property.Type.LONG_TERM_RENTAL, Property.Type.COMMERCIAL):
            line = ', '.join(tenants_by_unit.get(u.pk, [])[:2]) or _size(u, prop)
        else:
            line = _size(u, prop)
        rows.append({'label': u.label, 'line': line})
    title = {Property.Type.ASSOCIATION: 'Units and owners', Property.Type.COMMERCIAL: 'Units and tenants', Property.Type.SHORT_TERM_RENTAL: 'Units'}.get(ptype, 'Units')
    return {'key': 'units', 'title': title, 'rows': rows, 'more': max(0, len(units) - len(rows)), 'total': len(units), 'hint': f'{len(units)} unit{"" if len(units) == 1 else "s"}',
            'edit': '#owners' if (ptype == Property.Type.ASSOCIATION and context.get('ap')) else '#units'}


def _trash(context):
    trash = context.get('trash') or {}
    if not trash.get('set'):
        return None
    rows = [{'label': r['name'], 'line': r['day_text']} for r in trash.get('rules', [])]
    first = (trash.get('by_day') or [{}])[0].get('day_text', '')
    return {'key': 'trash', 'title': 'Trash', 'rows': rows, 'hint': first, 'edit': '#trash' if 'trash' in (context.get('sections') or ()) else ''}


@register.simple_tag(takes_context=True)
def property_phone(context):
    prop = context['property']
    units = list(context.get('units') or [])
    contacts = [c for g in (context.get('contact_groups') or []) for c in g['contacts']]
    builders = {
        'access': lambda: _access(prop, units, context),
        'contacts': lambda: _contacts(prop, contacts),
        'tickets': lambda: _tickets(prop, context.get('open_tickets') or []),
        'units': lambda: _units(prop, units, context),
        'trash': lambda: _trash(context),
    }
    sections = []
    for key in ORDER.get(prop.property_type, ORDER_DEFAULT):
        built = builders[key]()
        if built:
            built['icon'] = ICONS[key]
            built['open'] = len(sections) < OPEN_BY_DEFAULT
            sections.append(built)
    return {'sections': sections, 'has_tickets': any(s['key'] == 'tickets' for s in sections)}
