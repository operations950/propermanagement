"""The Tickets screen's newspaper boxes: every ticket in exactly ONE box, by when it is due.

Order of the boxes: Urgent (a ticket marked Urgent, whatever its date), Overdue, Today, Tomorrow, This week (after tomorrow, through SATURDAY), Next week (the following Sunday through
Saturday), then In 2 weeks, In 3 weeks, In 4 weeks and so on (each one a whole Sunday-to-Saturday week), and No due date last. Tickets that are finished (completed, cancelled ...) are not "due":
a list that shows them puts them in one box for each month they were closed in, after the open ones. Only the boxes that have tickets are made.
"""
from datetime import date, timedelta

from django.utils import timezone

from core.newspaper import box

from .models import Priority

URGENT_ORDER, OVERDUE_ORDER, TODAY_ORDER, TOMORROW_ORDER, WEEK_ORDER, NO_DATE_ORDER, DONE_ORDER = 0, 1, 2, 3, 4, 10000, 20000
PRIORITY_RANK = {Priority.URGENT: 0, Priority.HIGH: 1, Priority.MEDIUM: 2, Priority.LOW: 3}


def week_start(day):
    """The Sunday on or before `day` (a week here is Sunday to Saturday)."""
    return day - timedelta(days=(day.weekday() + 1) % 7)


def _local_day(moment):
    if moment is None:
        return None
    if isinstance(moment, date) and not hasattr(moment, 'hour'):
        return moment
    return timezone.localtime(moment).date() if timezone.is_aware(moment) else moment.date()


def _range_text(start, end):
    return f'{start:%b} {start.day} – {end:%b} {end.day}'


def _day_text(day):
    return f'{day:%a}, {day:%b} {day.day}'


def _week_box(k, sunday):
    start = sunday + timedelta(days=7 * k)
    end = start + timedelta(days=6)
    title = 'This week' if k == 0 else 'Next week' if k == 1 else f'In {k} weeks'
    return f'week-{k}', WEEK_ORDER + k, title, _range_text(start, end), ''


def bucket_of(ticket, today, done_statuses):
    """(key, order, title, hint, tone) of the one box this ticket belongs in."""
    if ticket.status in done_statuses:
        closed = _local_day(getattr(ticket, 'completed_at', None)) or _local_day(ticket.created_at) or today
        return f'done-{closed:%Y%m}', DONE_ORDER + (999999 - (closed.year * 12 + closed.month)), f'Done · {closed:%B %Y}', '', ''
    if ticket.priority == Priority.URGENT:
        return 'urgent', URGENT_ORDER, 'Urgent', '', 'urgent'
    due = _local_day(ticket.due_date)
    if due is None:
        return 'no-date', NO_DATE_ORDER, 'No due date', '', ''
    if due < today:
        return 'overdue', OVERDUE_ORDER, 'Overdue', '', 'late'
    if due == today:
        return 'today', TODAY_ORDER, 'Today', _day_text(today), ''
    if due == today + timedelta(days=1):
        return 'tomorrow', TOMORROW_ORDER, 'Tomorrow', _day_text(due), ''
    return _week_box((due - week_start(today)).days // 7, week_start(today))


def _row_sort(ticket):
    due = _local_day(ticket.due_date)
    return (due or date.max, PRIORITY_RANK.get(ticket.priority, 9), (ticket.title or '').lower())


def ticket_boxes(tickets, today=None, done_statuses=(), keep_order=False):
    """The boxes for these tickets (a list, in the order they should be read). Inside a box the tickets run by due date, then priority, then title, unless `keep_order` (the person chose a
    column to sort by: then the order they came in is kept)."""
    today = today or timezone.localdate()
    found = {}
    for ticket in tickets:
        key, order, title, hint, tone = bucket_of(ticket, today, done_statuses)
        found.setdefault(key, {'order': order, 'title': title, 'hint': hint, 'tone': tone, 'rows': []})['rows'].append(ticket)
    boxes = []
    for key, one in sorted(found.items(), key=lambda kv: kv[1]['order']):
        rows = one['rows'] if keep_order else sorted(one['rows'], key=_row_sort)
        boxes.append(box(one['title'], rows, hint=one['hint'], key=key, tone=one['tone']))
    return boxes
