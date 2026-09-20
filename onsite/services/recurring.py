"""Turns a VisitRule into Visits, on the dates the rule says.

A rule carries `next_due`: the date of the next visit it should produce,
chosen by a person when the rule is created (and editable later). A visit
is created LOOKAHEAD days ahead of its date, so it is already on the board,
assigned and on the calendar when the day comes — but it is always
scheduled ON its due date, never on the day the generator happened to run.
Creating a visit moves `next_due` forward by one interval.

A rule with no `next_due` produces nothing: it has no start date yet, and
the rules screen flags it. (It used to mean "generate immediately", which
gave a brand-new rule no say over when its first visit landed.)

If the generator was down past a due date, the visit is created for today
and the schedule continues from there — it doesn't try to backfill the
missed ones."""
from datetime import timedelta

from dateutil.relativedelta import relativedelta
from django.utils import timezone

from ..models import Visit
from .checklist import create_visit
from .notify import notify_assignee

MAX_LOOKAHEAD_DAYS = 7


def advance(rule, from_date):
    """One interval after from_date."""
    if rule.interval_days:
        return from_date + timedelta(days=rule.interval_days)
    return from_date + relativedelta(months=rule.interval_months)


def lookahead_days(rule):
    """How many days before its date a visit is created. Always shorter than
    the interval, or a weekly rule would create next week's visit the moment
    it created this week's, then the week after's, and so on."""
    interval_days = rule.interval_days or rule.interval_months * 28
    return max(0, min(MAX_LOOKAHEAD_DAYS, interval_days - 1))


def generate_for_rule(rule, today=None):
    """Creates the rule's next visit if it's within the lookahead window.
    Returns the Visit, or None when nothing was due (or the rule has no
    start date / is paused / points at a general placeholder)."""
    today = today or timezone.localdate()
    if not rule.is_active or rule.next_due is None or rule.property.is_general:
        return None
    if rule.next_due > today + timedelta(days=lookahead_days(rule)):
        return None
    scheduled = max(rule.next_due, today)
    visit = create_visit(
        rule.property, rule.visit_type, unit=rule.unit,
        scheduled_date=scheduled,
        assigned_staff=rule.default_assignee,
        status=Visit.Status.SCHEDULED if rule.default_assignee else Visit.Status.UNASSIGNED,
        created_from_rule=rule,
    )
    rule.last_generated_at = scheduled
    rule.next_due = advance(rule, scheduled)
    rule.save(update_fields=['last_generated_at', 'next_due'])
    # A rule's default assignee is assigned at creation, and nothing else
    # would ever tell them — the visit screen only notifies on a manual
    # assignment. Best-effort (never raises).
    if visit.assigned_staff_id or visit.assigned_contact_id:
        notify_assignee(visit)
    return visit
