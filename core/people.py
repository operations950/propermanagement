"""Who can be picked for a job. A person who was removed (their login is deactivated; see People and roles) is not offered in any list that assigns work or filters by owner.

One helper so every picker uses the same rule. A person who is ALREADY on the thing being edited (the ticket's assignee, a rule's owner, the person a list is filtered to) is kept in the
list, so the screen still shows who it is: callers pass their ids in `also`, and the screens label such a person "(removed)" (StaffProfile.is_removed)."""
from django.db.models import Q

from .models import StaffProfile

DEFAULT_ORDER = ('user__first_name', 'user__last_name', 'user__username')


def _ids(values):
    out = []
    for v in values or ():
        s = str(v if v is not None else '').strip()
        if s.isdigit():
            out.append(int(s))
    return out


def active_staff(also=(), order=DEFAULT_ORDER):
    """Staff whose login is active, plus anyone whose id is in `also` (the people already on the thing being edited)."""
    cond = Q(user__is_active=True)
    ids = _ids(also)
    if ids:
        cond |= Q(pk__in=ids)
    return StaffProfile.objects.select_related('user').filter(cond).order_by(*order)


def default_admin():
    """The person automatic work (reviews, contract alerts, close steps) is handed to when nobody was chosen: the first ACTIVE company admin, else the first active person."""
    active = StaffProfile.objects.filter(user__is_active=True).order_by('pk')
    return active.filter(is_company_admin=True).first() or active.first()


def label(person):
    """A person's name for a picker; a removed person reads "Name (removed)"."""
    return f'{person} (removed)' if person.is_removed else str(person)
