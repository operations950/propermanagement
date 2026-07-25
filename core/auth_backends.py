from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.db.models import Q


class EmailOrUsernameModelBackend(ModelBackend):
    """Accepts either an email address or a plain username in the login
    form's single "identifier" field. New accounts are expected to have
    User.email set and log in with that; a handful of existing staff
    accounts predate this and have no email on file yet, so they keep
    working with their original username rather than being locked out."""

    def authenticate(self, request, username=None, password=None, **kwargs):
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
