"""The phone's first screen: one big Home tile and six medium tiles, and everything else is one tap down from it.

The installed app opens here (the manifest's start_url points at /m/). A person taps a tile, works in that area, and the Menu button in the phone top bar (templates/_mobile_bar.html)
brings them straight back up. For a manager or an admin the tiles are, in the order of importance: Home (big, two columns wide), then Tickets, Compliance, Projects, On-Site, Properties and
Contacts. The time clock card stays on the phone's Home screen (the Home tile says where the clock stands), so clocking in and out never needs a tile of its own. A cleaner keeps her own
set: On-Site first, then the time clock for people on it (or "New ticket"), then the rest. The menu role limits what the desktop menu *shows*, but it never blocked an address.
"""
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone


def _first_name(user):
    return (user.first_name or user.get_full_name() or user.username or '').split(' ')[0]


def is_cleaner(profile):
    """The same test the installed app's start address uses: a cleaner by menu role on the new layout, by team on the original site."""
    if profile is None:
        return False
    return profile.role == 'cleaner'


def _plural(n, one, many=None):
    return f'{n} {one}' if n == 1 else f'{n} {many or one + "s"}'


def _today_counts(profile):
    """(things to do today, visits today) from the same list Home draws. Never allowed to take the menu down."""
    try:
        from workspace import services
        data = services.home(profile, 'today')
        visits = sum(len(k['rows']) for g in data['groups'] for k in g['kinds'] if k['kind'] == 'visit')
        return data['count'], visits
    except Exception:
        return None, None


def _open_tickets(profile):
    try:
        from tickets.models import Ticket
        done = [Ticket.Status.COMPLETED, Ticket.Status.VERIFIED, Ticket.Status.CANCELLED]
        return Ticket.objects.filter(assigned_staff=profile).exclude(status__in=done).count()
    except Exception:
        return None


def _clock_tile(user):
    """The time clock tile for someone on the clock: where they stand today in a few words. None when the clock does not apply to them."""
    try:
        from timeclock import services as tc
        clock = tc.profile_for(user)
        if clock is None:
            return None
        punch = tc.today_punch(clock)
        if punch and punch.clock_out:
            hint = 'Done for today'
        elif punch:
            t = timezone.localtime(punch.clock_in)
            hint = f'Clocked in {t.hour % 12 or 12}:{t.minute:02d} {"AM" if t.hour < 12 else "PM"}'
        else:
            hint = 'Not clocked in'
        return {'key': 'clock', 'label': 'Time clock', 'icon': 'clock', 'url': reverse('timeclock_home'), 'hint': hint,
                'state': 'done' if (punch and punch.clock_out) else 'in' if punch else 'out'}
    except Exception:
        return None


def _clock_hint(user):
    """Where the clock stands, in a few words, for people on the time clock (None for everyone else): it rides on the Home tile because the clock card lives on Home."""
    tile = _clock_tile(user)
    return tile['hint'] if tile else None


def tiles(user):
    """The tiles, in order, for this person: Home (big) and the six medium ones for a manager or admin; a cleaner's own six."""
    profile = getattr(user, 'staff_profile', None)
    new_face = bool(profile and profile.new_face)
    todo, visits = _today_counts(profile) if profile else (None, None)
    open_tickets = _open_tickets(profile) if profile else None
    home_url = reverse('ws_home') if new_face else reverse('dashboard')
    tickets_url = reverse('ticket_list') if new_face else reverse('ticket_list') + '?source=reactive'

    home = {'key': 'home', 'label': 'Home', 'icon': 'home', 'url': home_url,
            'hint': ('Nothing due today' if not todo else f'{todo} to do today') if todo is not None else 'Your day and calendar'}
    tickets = {'key': 'tickets', 'label': 'Tickets', 'icon': 'ticket', 'url': tickets_url,
               'hint': (f'{open_tickets} open for you' if open_tickets else 'Nothing open for you') if open_tickets is not None else 'See and filter tickets'}
    onsite = {'key': 'onsite', 'label': 'On-Site', 'icon': 'spray-can', 'url': reverse('onsite_dashboard'),
              'hint': (_plural(visits, 'visit') + ' today' if visits else 'No visits today') if visits is not None else 'Visits and cleanings'}
    properties = {'key': 'properties', 'label': 'Properties', 'icon': 'building-2', 'url': reverse('property_list'), 'hint': 'Look one up'}
    contacts = {'key': 'contacts', 'label': 'Contacts', 'icon': 'contact', 'url': reverse('contact_list'), 'hint': 'Call or email someone'}
    if is_cleaner(profile):                       # her work first: On-Site, then the clock (or New ticket), then the rest in their usual order: unchanged
        sixth = _clock_tile(user) or {'key': 'new_ticket', 'label': 'New ticket', 'icon': 'plus-circle', 'url': reverse('ticket_create'), 'hint': 'Report something'}
        first = [onsite, sixth]
        return first + [t for t in (home, tickets, properties, contacts) if t not in first]

    compliance_url = reverse('ws_compliance') if new_face else reverse('my_sessions')
    compliance = {'key': 'compliance', 'label': 'Compliance', 'icon': 'calendar-check', 'url': compliance_url, 'hint': 'Recurring work and rules'}
    projects = {'key': 'projects', 'label': 'Projects', 'icon': 'layers', 'url': reverse('ws_projects'), 'hint': 'Longer jobs with steps'}
    clock = _clock_hint(user)
    if clock:                                     # the clock card is on Home: the big tile says where the clock stands
        home['hint'] = f"{home['hint']} \u00b7 {clock}"
    home['big'] = True
    return [home, tickets, compliance, projects, onsite, properties, contacts]


# The phone's Quick add (a phone is for doing, not setting up: no new properties, project starts or templates here). The desktop Quick add (templates/base.html) has the fuller list.
QUICK_ADD = [
    ('ticket_create', 'New ticket', 'ticket'),
    ('session_template_create', 'New compliance rule', 'calendar-check'),
    ('onsite_visit_create', 'New on-site visit', 'spray-can'),
    ('contact_create', 'New contact', 'contact'),
]


def quick_add():
    return [{'url': reverse(name), 'label': label, 'icon': icon} for name, label, icon in QUICK_ADD]


@login_required
def menu(request):
    from . import owner_portal
    if owner_portal.is_owner_user(request.user):
        return redirect('owner_home')
    profile = getattr(request.user, 'staff_profile', None)
    new_face = bool(profile and profile.new_face)
    today = timezone.localdate()
    return render(request, 'core/mobile_menu.html', {
        'tiles': tiles(request.user), 'quick_add': quick_add(), 'first_name': _first_name(request.user), 'today_label': f'{today:%A, %B} {today.day}',
        'full_site_url': reverse('ws_home') if new_face else reverse('dashboard'),
        'mobile_top_level': True,
    })
