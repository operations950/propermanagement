"""The phone's first screen: six big tiles, and everything else is one tap down from it.

The installed app opens here (the manifest's start_url points at /m/). A person taps a tile, works in that area, and the Menu button in the phone top bar (templates/_mobile_bar.html)
brings them straight back up. The six tiles are the same for everyone: the menu role limits what the desktop menu *shows*, but it never blocked an address, and on a phone
everybody needs to find a property or a contact. A cleaner gets On-Site first. The sixth tile is the time clock for people on it, and "New ticket" for everyone else.
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


def tiles(user):
    """The six tiles, in order, for this person."""
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
    sixth = _clock_tile(user) or {'key': 'new_ticket', 'label': 'New ticket', 'icon': 'plus-circle', 'url': reverse('ticket_create'), 'hint': 'Report something'}

    out = [home, tickets, onsite, properties, contacts, sixth]
    if is_cleaner(profile):                       # her work first: On-Site, then the clock, then the rest in their usual order
        first = [onsite, sixth]
        out = first + [t for t in (home, tickets, properties, contacts) if t not in first]
    return out


@login_required
def menu(request):
    from . import owner_portal
    if owner_portal.is_owner_user(request.user):
        return redirect('owner_home')
    profile = getattr(request.user, 'staff_profile', None)
    new_face = bool(profile and profile.new_face)
    today = timezone.localdate()
    return render(request, 'core/mobile_menu.html', {
        'tiles': tiles(request.user), 'first_name': _first_name(request.user), 'today_label': f'{today:%A, %B} {today.day}',
        'full_site_url': reverse('ws_home') if new_face else reverse('dashboard'),
        'mobile_top_level': True,
    })
