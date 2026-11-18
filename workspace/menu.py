"""The menu: the items the whole team sees, plus Financials, Dashboard, Portfolio and Admin for the people whose switches on People and roles allow them.

The menu only shows or hides a link. The screens themselves check the same switches (core/permissions.py), so a hidden link is also a closed door."""
from django.urls import reverse

# key -> (label, lucide icon)
ITEMS = {
    'home': ('Home', 'home'),
    'dashboard': ('Dashboard', 'layout-dashboard'),
    'tickets': ('Tickets', 'ticket'),
    'compliance': ('Compliance', 'calendar-check'),
    'projects': ('Projects', 'layers'),
    'onsite': ('On-Site', 'spray-can'),
    'properties': ('Properties', 'building-2'),
    'financials': ('Financials', 'landmark'),
    'contacts': ('Contacts', 'contact'),
    'time': ('Time', 'clock'),
    'legal': ('Legal forms', 'scale'),
    'portfolio': ('Portfolio', 'briefcase'),
    'admin': ('Admin', 'shield'),
}

# Everyone on the team gets these, in the order of importance: Home, Tickets, Compliance, Projects, On-Site, Properties, Contacts, (Financials), Time, (Portfolio), (Admin). Financials, the statistics
# dashboard, Portfolio and Admin are added by the person's switches on People and roles (core/permissions.py).
BASE_MENU = ['home', 'tickets', 'compliance', 'projects', 'onsite', 'properties', 'contacts', 'time']       # Legal forms is a card on the Associations page, not a menu item

LINES = [
    ('associations', 'Associations'),
    ('short-term-rentals', 'Short-term rentals'),
    ('long-term-rentals', 'Long-term rentals'),
    ('commercial', 'Commercial'),
]

# Which addresses light up which item.
PREFIXES = {
    'home': ['/w/home'],
    'tickets': ['/tickets'],
    'compliance': ['/w/compliance', '/compliance', '/recurring'],
    'projects': ['/w/projects', '/processes'],
    'onsite': ['/onsite', '/w/onsite'],
    'properties': ['/properties', '/w/lines', '/legal', '/documents'],       # Legal forms is reached from the Associations page
    'financials': ['/w/financials', '/close', '/accounting'],
    'contacts': ['/contacts'],
    'time': ['/clock'],
    'portfolio': ['/portfolio'],
    'legal': ['/legal', '/documents'],
    'admin': ['/w/admin', '/admin-tools', '/faq-review'],
}


def role_for(profile):
    """Only used to tell a cleaner (the phone layout and the guest-request prompt) from everyone else: the person's department decides, not a menu."""
    if profile.role == 'cleaner':
        return 'cleaner'
    return 'owner' if profile.is_company_admin else 'manager'


def build(request, profile):
    """[{key, label, icon, url, active}] for the menu, in order: the team's items, plus what this person's switches allow."""
    from core import permissions
    from timeclock import services as tc
    user = request.user
    admin = permissions.has_admin(user)
    path = request.path
    keys = list(BASE_MENU)
    if permissions.has_financial(user):
        keys.insert(keys.index('contacts') + 1, 'financials')           # the order of importance: ... Properties, Contacts, Financials, then Time
    if admin:
        keys.insert(1, 'dashboard')
    if permissions.has_portfolio(user):
        keys.append('portfolio')
    if admin:
        keys.append('admin')
    from core import nudges
    counts = nudges.nav_counts(user)
    out = []
    for key in keys:
        label, icon = ITEMS[key]
        if key == 'dashboard':
            url, active = reverse('dashboard') + '?stats=1', path == '/'
        elif key == 'home':
            url, active = reverse('ws_home'), path == '/w/' or path.startswith('/w/home')
        elif key == 'tickets':
            url, active = reverse('ticket_list'), any(path.startswith(p) for p in PREFIXES[key])
        elif key == 'compliance':
            url, active = reverse('ws_compliance'), any(path.startswith(p) for p in PREFIXES[key])
        elif key == 'projects':
            url, active = reverse('ws_projects'), any(path.startswith(p) for p in PREFIXES[key])
        elif key == 'onsite':
            url, active = reverse('onsite_dashboard'), any(path.startswith(p) for p in PREFIXES[key])
        elif key == 'properties':
            url, active = reverse('property_list'), any(path.startswith(p) for p in PREFIXES[key])
        elif key == 'financials':
            url, active = reverse('ws_financials'), any(path.startswith(p) for p in PREFIXES[key])
        elif key == 'contacts':
            url, active = reverse('contact_list'), any(path.startswith(p) for p in PREFIXES[key])
        elif key == 'time':
            has_profile = bool(tc.profile_for(user))
            url = reverse('timeclock_home') if has_profile or not permissions.has_time_admin(user) else reverse('timeclock_manage')
            active = any(path.startswith(p) for p in PREFIXES[key])
        elif key == 'legal':
            url, active = reverse('legalforms:list'), any(path.startswith(p) for p in PREFIXES[key])
        elif key == 'portfolio':
            url, active = reverse('portfolio_dashboard'), any(path.startswith(p) for p in PREFIXES[key])
        else:
            url, active = reverse('ws_admin'), any(path.startswith(p) for p in PREFIXES[key])
        out.append({'key': key, 'label': label, 'icon': icon, 'url': url, 'active': active, 'badge': counts.get(key, 0)})
    return out


LINE_ICONS = {'associations': 'landmark', 'short-term-rentals': 'home', 'long-term-rentals': 'key-round', 'commercial': 'store'}
LINE_COUNTS_SECONDS = 120


def line_counts():
    """{line slug: how many properties it has} for the strip under the main bar: active properties of that kind, not the placeholders ("Commercial (general)", "No specific property"). One query,
    kept for a couple of minutes so the strip does not cost anything on every page; a problem here must never take a page down."""
    from django.core.cache import cache
    cached = cache.get('ws:line_counts')
    if cached is not None:
        return cached
    counts = {}
    try:
        from core.models import Property
        from core.views import _is_placeholder
        from . import services
        by_type = {ptype: slug for slug, (ptype, _label) in services.LINE_TYPES.items()}
        for p in Property.objects.filter(is_active=True).only('name', 'property_type', 'is_general'):
            if p.property_type in by_type and not _is_placeholder(p):
                counts[by_type[p.property_type]] = counts.get(by_type[p.property_type], 0) + 1
    except Exception:
        return {}
    cache.set('ws:line_counts', counts, LINE_COUNTS_SECONDS)
    return counts


def lines(request, profile):
    counts = line_counts()
    return [{'slug': slug, 'label': label, 'url': reverse('ws_line', args=[slug]), 'active': request.path.startswith(f'/w/lines/{slug}'), 'icon': LINE_ICONS.get(slug, 'building-2'), 'count': counts.get(slug, 0)}
            for slug, label in LINES]
