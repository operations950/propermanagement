from . import services


def nav(request):
    """Two small things every page needs: whether to show the time links in the menu, and (on the dashboard only) the one-line time card."""
    user = getattr(request, 'user', None)
    if not user or not user.is_authenticated:
        return {}
    from core.views import _is_admin
    profile = services.profile_for(user)
    manager = _is_admin(user)
    out = {'tc_employee': bool(profile), 'tc_manager': manager}
    if request.path == '/' and (profile or manager):
        out['tc_card'] = _card(profile, manager)
    return out


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
