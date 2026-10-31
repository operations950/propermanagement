from . import menu


def face(request):
    """Tells base.html whether to draw the new layout (only for people with StaffProfile.new_face on) and gives it the menu. Everyone else gets {'new_face': False} and the original menu, untouched."""
    user = getattr(request, 'user', None)
    if not user or not user.is_authenticated:
        return {'new_face': False}
    profile = getattr(user, 'staff_profile', None)
    if not profile or not profile.new_face:
        return {'new_face': False}
    try:
        return {'new_face': True, 'ws_nav': menu.build(request, profile), 'ws_lines': menu.lines(request, profile), 'ws_role': menu.role_for(profile)}
    except Exception:       # a menu problem must never take a page down; fall back to the original layout
        return {'new_face': False}
