from . import menu, services


def face(request):
    """Tells base.html whether to draw the new layout (only for people with StaffProfile.new_face on) and gives it the menu. Everyone else gets {'new_face': False} and the original menu, untouched."""
    user = getattr(request, 'user', None)
    if not user or not user.is_authenticated:
        return {'new_face': False}
    profile = getattr(user, 'staff_profile', None)
    if not profile or not profile.new_face:
        return {'new_face': False}
    try:
        ctx = {'new_face': True, 'ws_nav': menu.build(request, profile), 'ws_lines': menu.lines(request, profile), 'ws_role': menu.role_for(profile)}
    except Exception:       # a menu problem must never take a page down; fall back to the original layout
        return {'new_face': False}
    if request.path.startswith('/onsite/'):
        # Guest requests waiting for an answer: a count on the Requests tab for everyone, and a card asking about the next one for a cleaner
        # (or for anyone who follows the count, ?review=1). Never allowed to take a page down.
        try:
            want = ctx['ws_role'] == 'cleaner' or request.GET.get('review') == '1'
            ctx['ws_req_count'], ctx['ws_req_prompt'] = services.request_state(request.session, want)
        except Exception:
            pass
    return ctx
