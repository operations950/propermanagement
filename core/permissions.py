"""Who may do what. Four switches on a person's profile (set on People and roles) and nothing else decides it:

  Admin               StaffProfile.is_company_admin: Admin Tools (keys, QuickBooks, Gmail and Quo, creating staff), People and roles, the statistics dashboard.
  Financial           StaffProfile.can_financial: all of the books, commercial leases / invoices / CAM, and the whole month-end checklist.
  Time clock manager  StaffProfile.can_time_admin: everyone's hours, approvals, setup and the office connection.
  Portfolio           StaffProfile.is_portfolio_owner: the private portfolio dashboards (you only).

A Django superuser has every switch. Each switch stands alone: Admin does not include Financial, and the other way round. Every screen asks one of these functions, so the menu and the server agree.
"""


def _profile(user):
    return getattr(user, 'staff_profile', None) if getattr(user, 'is_authenticated', False) else None


def has_admin(user):
    if getattr(user, 'is_superuser', False):
        return True
    profile = _profile(user)
    return bool(profile and profile.is_company_admin and user.is_active)


def can_use_dark_mode(user):
    """Who is offered dark mode and ever sees it. For now only people with the Admin box (superusers count); everyone else, and anyone signed out, is always light whatever their system setting.
    Widening it later is a change to this one function."""
    return bool(getattr(user, 'is_authenticated', False) and has_admin(user))


def has_financial(user):
    if getattr(user, 'is_superuser', False):
        return True
    profile = _profile(user)
    return bool(profile and profile.can_financial and user.is_active)


def has_time_admin(user):
    if getattr(user, 'is_superuser', False):
        return True
    profile = _profile(user)
    return bool(profile and profile.can_time_admin and user.is_active)


def has_portfolio(user):
    profile = _profile(user)
    return bool(profile and profile.is_portfolio_owner and user.is_active)
