from . import nudges


def alerts(request):
    """The numbers beside the menu items (On-Site 4, Contacts 1): how many reminders each part has. Only for company admins, and never allowed to take a page down."""
    user = getattr(request, 'user', None)
    if not user or not user.is_authenticated:
        return {}
    try:
        return {'nav_alerts': nudges.nav_counts(user)}
    except Exception:
        return {}
