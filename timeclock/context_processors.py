from . import services

HOME_PATHS = ('/', '/w/', '/w/home/')          # the dashboards and the new Home screen: where the big clock card goes


def nav(request):
    """Two small things every page needs: whether to show the time links in the menu, and (on the dashboard only) the one-line time card.
    Cleaners and people with no set schedule clock in from a phone, so on the home screens they get the big clock card at the very top instead (tc_clock)."""
    user = getattr(request, 'user', None)
    if not user or not user.is_authenticated:
        return {}
    from core.views import _is_admin
    profile = services.profile_for(user)
    manager = _is_admin(user)
    out = {'tc_employee': bool(profile), 'tc_manager': manager}
    if request.path in HOME_PATHS and profile and wants_clock_card(profile):
        out['tc_clock'] = _clock(request, profile)
    if request.path == '/' and (profile or manager):
        card = _card(profile, manager)
        if out.get('tc_clock'):                 # the big card already has the clock buttons
            card.pop('me', None)
        if card:
            out['tc_card'] = card
    return out


def wants_clock_card(profile):
    """True for people who clock in on a phone: a cleaner (by team or by menu) or anyone with no set schedule."""
    from core.models import StaffProfile
    staff = profile.staff
    return bool(profile.variable_hours or staff.role == StaffProfile.Role.CLEANER or staff.menu_role == StaffProfile.MenuRole.CLEANER)


def _clock(request, profile):
    from django.utils import timezone

    from core.auth_backends import _client_ip

    from . import policy
    from .models import Punch
    today = timezone.localdate()
    punch = services.today_punch(profile, today)
    start, end = policy.pay_period(today)
    minutes = sum(p.minutes or 0 for p in Punch.objects.filter(profile=profile, day__range=(start, end), clock_out__isnull=False))
    office_set = services.office_configured()
    return {'punch': punch, 'in': bool(punch and not punch.clock_out), 'done': bool(punch and punch.clock_out), 'profile': profile,
            'remote_now': not services.at_office(_client_ip(request)), 'office_set': office_set, 'show_where': office_set or bool(punch and punch.in_remote),
            'lunch_choices': (0, 15, 30, 45, 60, 75, 90), 'period_min': minutes, 'next': request.path}


def _card(profile, manager):
    from django.utils import timezone
    card = {}
    if profile:
        punch = services.today_punch(profile)
        card['me'] = {'punch': punch, 'in': bool(punch and not punch.clock_out), 'done': bool(punch and punch.clock_out), 'profile': profile}
    if manager:
        profiles = services.tracked_profiles()
        card['board'] = services.today_board(profiles)
        card['waiting'] = len(services.collect_items(timezone.localdate())) if profiles else 0
        card['has_people'] = bool(profiles)
    return card
