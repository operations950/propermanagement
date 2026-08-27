from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.core.exceptions import PermissionDenied
from django.db.models import Q

from vendorportal.models import AccessAttempt


def _client_ip(request):
    """Same shape as vendorportal/intake's own _client_ip helpers — the
    app runs behind Railway's reverse proxy, so REMOTE_ADDR alone would
    just be the proxy's own address."""
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '0.0.0.0') if request else '0.0.0.0'


class EmailOrUsernameModelBackend(ModelBackend):
    """Accepts either an email address or a plain username in the login
    form's single "identifier" field. New accounts are expected to have
    User.email set and log in with that; a handful of existing staff
    accounts predate this and have no email on file yet, so they keep
    working with their original username rather than being locked out.

    Also the one place brute-force protection lives for every credentialed
    login surface in the app (StaffLoginView AND Django's own /admin/ login
    both go through django.contrib.auth.authenticate(), which tries every
    AUTHENTICATION_BACKENDS entry in order — this one runs first). Reuses
    the same AccessAttempt per-IP throttle (and default 30-per-5-minutes
    threshold) the vendor completion link and Quo webhook already rely on
    for the identical reason. Raising PermissionDenied here — rather than
    just returning None like a normal failed check — is what actually
    matters: django.contrib.auth.authenticate() specifically catches
    PermissionDenied and stops trying further backends immediately, so a
    rate-limited attempt can't fall through to the plain
    django.contrib.auth.backends.ModelBackend listed after this one and
    get a normal, unthrottled password check anyway."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        if request is not None and AccessAttempt.is_rate_limited(_client_ip(request)):
            raise PermissionDenied('Too many login attempts.')
        identifier = username or kwargs.get(get_user_model().USERNAME_FIELD)
        if identifier is None or password is None:
            return None
        User = get_user_model()
        try:
            user = User.objects.get(Q(email__iexact=identifier) | Q(username__iexact=identifier))
        except (User.DoesNotExist, User.MultipleObjectsReturned):
            return None
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
