from django.shortcuts import redirect

from . import policy
from .models import TimeProfile


class PolicyWallMiddleware:
    """Staff the time clock covers see nothing else until they have read and signed the current policy: every page sends them to it (a default-deny rule
    applied here to every request, like the owner wall, not a check each page remembers to make). The policy page itself, signing out and static
    files stay open. When the policy text changes (POLICY_VERSION goes up) they are asked again."""
    OPEN_PREFIXES = ('/clock/policy/', '/logout/', '/static/', '/media/')

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if user.is_authenticated and not request.path.startswith(self.OPEN_PREFIXES):
            unsigned = TimeProfile.objects.filter(staff__user=user, applies=True).exclude(signatures__version=policy.POLICY_VERSION).exists()
            if unsigned:
                return redirect('timeclock_policy')
        return self.get_response(request)
