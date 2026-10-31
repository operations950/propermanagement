"""The menu of the new layout: which items a person sees, built from their menu role.

The menu role decides what is *shown*. It never blocks an address, and it has nothing to do with StaffProfile.role (the ticket queue)."""
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
    'portfolio': ('Portfolio', 'briefcase'),
    'admin': ('Admin', 'shield'),
}

ROLE_MENUS = {
    'owner': ['home', 'dashboard', 'tickets', 'compliance', 'projects', 'onsite', 'properties', 'financials', 'contacts', 'time', 'portfolio', 'admin'],
    'manager': ['home', 'tickets', 'compliance', 'projects', 'onsite', 'properties', 'financials', 'contacts', 'time'],
    'property_manager': ['home', 'tickets', 'compliance', 'projects', 'onsite', 'properties', 'contacts', 'time'],
    'accounting': ['home', 'tickets', 'compliance', 'projects', 'properties', 'financials', 'time'],
    'maintenance': ['home', 'tickets', 'projects', 'time'],
    'cleaner': ['home', 'onsite', 'time'],
}

# Roles that also get the row of business lines under the menu.
LINE_ROLES = {'owner', 'manager', 'property_manager', 'accounting'}

LINES = [
    ('associations', 'Associations'),
    ('short-term-rentals', 'Short-term rentals'),
    ('long-term-rentals', 'Long-term rentals'),
    ('commercial', 'Commercial'),
]

# Which addresses light up which item.
PREFIXES = {
    'home': ['/w/home'],
    'tickets': ['/tickets', '/templates', '/functions', '/task-'],
    'compliance': ['/w/compliance', '/recurring'],
    'projects': ['/w/projects', '/processes'],
    'onsite': ['/onsite', '/w/onsite'],
    'properties': ['/properties', '/w/lines'],
    'financials': ['/w/financials', '/close', '/accounting'],
    'contacts': ['/contacts'],
    'time': ['/clock'],
    'portfolio': ['/portfolio'],
    'admin': ['/w/admin', '/admin-tools', '/documents', '/faq-review', '/legal'],
}


def role_for(profile):
    """The menu role a person gets: the one set on their profile, else owner for company admins and manager for everyone else."""
    if profile.menu_role in ROLE_MENUS:
        return profile.menu_role
    return 'owner' if profile.is_company_admin else 'manager'


def build(request, profile):
    """[{key, label, icon, url, active}] for the new layout, in order."""
    from core.views import _is_admin
    from timeclock import services as tc
    user = request.user
    role = role_for(profile)
    admin = _is_admin(user)
    path = request.path
    keys = []
    for key in ROLE_MENUS[role]:
        if key == 'dashboard' and not profile.is_company_admin:
            continue          # the statistics dashboard is the owner's alone
        if key == 'admin' and not admin:
            continue
        if key == 'portfolio' and not profile.is_portfolio_owner:
            continue
        keys.append(key)
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
            url = reverse('timeclock_home') if has_profile or not admin else reverse('timeclock_manage')
            active = any(path.startswith(p) for p in PREFIXES[key])
        elif key == 'portfolio':
            url, active = reverse('portfolio_dashboard'), any(path.startswith(p) for p in PREFIXES[key])
        else:
            url, active = reverse('ws_admin'), any(path.startswith(p) for p in PREFIXES[key])
        out.append({'key': key, 'label': label, 'icon': icon, 'url': url, 'active': active})
    return out


def lines(request, profile):
    if role_for(profile) not in LINE_ROLES:
        return []
    return [{'slug': slug, 'label': label, 'url': reverse('ws_line', args=[slug]), 'active': request.path.startswith(f'/w/lines/{slug}')} for slug, label in LINES]
