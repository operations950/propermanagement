"""Deleting a compliance rule. A rule that has only produced open items nobody has touched can go, taking those empty items with it; a rule with real history (finished items,
lines that were worked, projects started from them) cannot, because deleting it would erase that record. Turn such a rule off instead."""
from django.db import transaction

from ..models import Session, SessionLine


def open_items(template):
    return template.sessions.count()


def blockers(template):
    """Why this rule cannot be deleted: a list of short phrases, empty when it can."""
    from processes.models import ProcessRun
    out = []
    finished = template.sessions.filter(status=Session.Status.SUBMITTED).count()
    if finished:
        out.append(f'{finished} finished item{"s" if finished != 1 else ""}')
    worked = SessionLine.objects.filter(session__template=template).exclude(state=SessionLine.State.PENDING).count()
    if worked:
        out.append(f'{worked} line{"s" if worked != 1 else ""} already worked on')
    runs = ProcessRun.objects.filter(session_line__session__template=template).count()
    if runs:
        out.append(f'{runs} project or process run{"s" if runs != 1 else ""} started from it')
    return out


@transaction.atomic
def delete_rule(template):
    """Delete the rule and its untouched open items. Returns how many items went with it. Raises ValueError (naming why) when the rule has history."""
    why = blockers(template)
    if why:
        raise ValueError(f'"{template.name}" has {", ".join(why)}. Deleting it would erase that record, so turn the rule off instead (untick Active).')
    n = template.sessions.count()
    template.sessions.all().delete()          # the open, untouched items (their lines go with them); a rule's items are PROTECTed against a plain delete
    template.delete()
    return n
