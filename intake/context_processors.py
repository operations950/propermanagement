def mail_problem(request):
    """For admins only: the open email problem, shown as a banner at the top of every page (templates/base.html) until it is fixed."""
    user = getattr(request, 'user', None)
    if not user or not user.is_authenticated:
        return {}
    from core import permissions
    if not permissions.has_admin(user):
        return {}
    from . import mail_health
    return {'mail_problem': mail_health.open_problem()}
