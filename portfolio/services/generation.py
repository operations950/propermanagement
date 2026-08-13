"""Turns active BizRecurringRules into BizTask rows — the portfolio app's
equivalent of worksessions/services/generation.py, same catch-up-safe
cursor-walk shape: generate_for_rule walks the cursor forward one
occurrence at a time from rule.next_due_date up through today, so a missed
period always still gets its own task (never fast-forwarded past), and
running this twice in a row creates nothing extra the second time (no
uniqueness constraint needed the way Session has one — a duplicate run
would just create a same-day second task, which the day-guard in
generate_for_rule below prevents by stopping once cursor > today rather
than ever re-deriving an already-passed occurrence)."""
from datetime import date, timedelta

from dateutil.relativedelta import relativedelta
from django.db import transaction
from django.utils import timezone

from ..models import BizRecurringRule, BizTask, Frequency

STEP = {
    Frequency.WEEKLY: relativedelta(weeks=1),
    Frequency.BIWEEKLY: relativedelta(weeks=2),
    Frequency.QUARTERLY: relativedelta(months=3),
    Frequency.YEARLY: relativedelta(years=1),
}


def nth_business_day(year, month, n):
    """Same limitation as worksessions/services/generation.py's copy of
    this: weekends are skipped, holidays are not accounted for."""
    d = date(year, month, 1)
    count = 0
    while d.month == month:
        if d.weekday() < 5:
            count += 1
            if count == n:
                return d
        d += timedelta(days=1)
    return None


def _last_day_of_month(year, month):
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def next_month_day_occurrence(after_date, day_of_month):
    """The next month's `day_of_month` — clamped to that month's real last
    day when it's shorter (e.g. day_of_month=31 in April -> April 30),
    same clamp behavior a bill due "on the 31st" needs in a 30-day month."""
    month_cursor = after_date.replace(day=1) + relativedelta(months=1)
    last_day = _last_day_of_month(month_cursor.year, month_cursor.month)
    return month_cursor.replace(day=min(day_of_month, last_day.day))


def next_workday_occurrence(after_date, workday_of_month):
    month_cursor = after_date.replace(day=1) + relativedelta(months=1)
    for _ in range(24):
        due = nth_business_day(month_cursor.year, month_cursor.month, workday_of_month)
        if due:
            return due
        month_cursor += relativedelta(months=1)
    return after_date + relativedelta(months=1)  # pathological fallback, shouldn't happen


def advance(cursor, frequency, day_of_month, workday_of_month):
    if frequency == Frequency.MONTHLY_DAY:
        return next_month_day_occurrence(cursor, day_of_month)
    if frequency == Frequency.MONTHLY_WORKDAY:
        return next_workday_occurrence(cursor, workday_of_month)
    return cursor + STEP[frequency]


def generate_for_rule(rule, today=None):
    """Walks rule.next_due_date forward through today, creating one
    BizTask per occurrence along the way. Returns the number created."""
    today = today or timezone.localdate()
    cursor = rule.next_due_date
    created_count = 0

    while cursor <= today:
        with transaction.atomic():
            BizTask.objects.create(
                business=rule.business,
                category=rule.category,
                recurring_rule=rule,
                title=rule.title,
                notes=rule.notes,
                priority=rule.priority,
                amount=rule.amount,
                custom_field_value=rule.custom_field_value,
                due_date=cursor,
            )
            created_count += 1
        cursor = advance(cursor, rule.frequency, rule.day_of_month, rule.workday_of_month)

    if cursor != rule.next_due_date:
        rule.next_due_date = cursor
        rule.save(update_fields=['next_due_date'])

    return created_count


def generate_due_tasks(today=None):
    """Entry point for the scheduler job / management command — every
    active rule, in turn."""
    today = today or timezone.localdate()
    total = 0
    for rule in BizRecurringRule.objects.filter(is_active=True, business__is_active=True):
        total += generate_for_rule(rule, today=today)
    return total


def preview_next_occurrences(rule, count=3):
    """Resolved next N occurrence dates, pure computation, no DB writes —
    same purpose as worksessions' preview_next_occurrences, for a live
    preview on the add-rule form."""
    if not rule.frequency or not rule.next_due_date:
        return []
    cursor = rule.next_due_date
    out = []
    for _ in range(count):
        out.append(cursor)
        cursor = advance(cursor, rule.frequency, rule.day_of_month, rule.workday_of_month)
    return out
