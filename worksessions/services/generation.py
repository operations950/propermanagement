"""Turns active SessionTemplates into Session rows — the sessions
equivalent of tickets/management/commands/generate_recurring_tickets.py,
deliberately simpler since there's no per-property override cursor
branching to support (targeting lives in one place, see matching_properties
below) and no skip_missed fast-forward path at all.

Catch-up-safe and idempotent by construction: generate_for_template walks
the cursor forward one period at a time from template.next_open_date up
through today, and _get_or_create_session is keyed on the real
UniqueConstraint (template, period_key) — so a missed period always still
gets its own Session (never fast-forwarded past), running this twice in a
row creates nothing the second time, and a process restart mid-run can't
double-create a period that already exists. See sessions/models.py's
Session.Meta.constraints comment for why this is a real, always-non-null
constraint rather than the old system's nullable-property gap.
"""
from datetime import timedelta

from dateutil.relativedelta import relativedelta
from django.db import transaction
from django.utils import timezone

from core.models import Property

from ..models import Frequency, Session, SessionLine, SessionTemplate

STEP = {
    Frequency.DAILY: relativedelta(days=1),
    Frequency.WEEKLY: relativedelta(weeks=1),
    Frequency.BIWEEKLY: relativedelta(weeks=2),
    Frequency.MONTHLY: relativedelta(months=1),
    Frequency.QUARTERLY: relativedelta(months=3),
    Frequency.YEARLY: relativedelta(years=1),
}


def nth_business_day(year, month, n):
    """Same limitation as tickets/management/commands/generate_recurring_tickets.py's
    copy of this function: weekends are skipped, holidays are not currently
    accounted for (no holiday calendar configured)."""
    from datetime import date
    d = date(year, month, 1)
    count = 0
    while d.month == month:
        if d.weekday() < 5:
            count += 1
            if count == n:
                return d
        d += timedelta(days=1)
    return None


def next_workday_occurrence(after_date, workday_of_month):
    month_cursor = after_date.replace(day=1) + relativedelta(months=1)
    for _ in range(24):
        due = nth_business_day(month_cursor.year, month_cursor.month, workday_of_month)
        if due:
            return due
        month_cursor += relativedelta(months=1)
    return after_date + relativedelta(months=1)  # pathological fallback, shouldn't happen


def advance(cursor, frequency, workday_of_month):
    if frequency == Frequency.MONTHLY_WORKDAY:
        return next_workday_occurrence(cursor, workday_of_month)
    return cursor + STEP[frequency]


_MONTH_GRANULAR = {Frequency.MONTHLY, Frequency.MONTHLY_WORKDAY, Frequency.QUARTERLY, Frequency.YEARLY}


def period_label_for(cursor, frequency):
    """"August 2026" for month-or-coarser cadences, "Tue 4 Aug" for
    day/week cadences — confirms cadence by example rather than by
    description (see Phase 3's live preview). Built without strftime's
    platform-dependent no-leading-zero flags (%-d is POSIX-only, absent on
    Windows) so this behaves identically in local dev and production."""
    if frequency in _MONTH_GRANULAR:
        return cursor.strftime('%B %Y')
    return f'{cursor.strftime("%a")} {cursor.day} {cursor.strftime("%b")}'


def matching_properties(template):
    """Query-driven line targeting — the only targeting mechanism a
    SessionTemplate has, deliberately with no group-level override chain
    layered on top (unlike the old TaskGroup.property_types, which could
    silently override a TicketTemplate's own target_type). One query: a
    property must be active, match one of property_types (if any are set),
    and carry every required_attributes tag (if any are set)."""
    qs = Property.objects.filter(is_active=True)
    if template.property_types:
        qs = qs.filter(property_type__in=template.property_types)
    for attr_id in template.required_attributes.values_list('id', flat=True):
        qs = qs.filter(attribute_assignments__attribute_id=attr_id)
    return list(qs.distinct().order_by('name'))


def matching_targets(template):
    """Query-driven line targeting, expanded to units when
    template.query_by_unit is set — a list of (label, property, unit)
    tuples. A matching property with active Units produces one tuple per
    unit instead of one for the whole property; a matching property with
    no units (or query_by_unit off, the default) produces exactly the same
    single property-level tuple this always has — existing rules keep
    their exact current behavior unless this flag is deliberately turned
    on."""
    properties = matching_properties(template)
    if not template.query_by_unit:
        return [(str(prop), prop, None) for prop in properties]

    targets = []
    for prop in properties:
        units = list(prop.units.filter(is_active=True).order_by('label'))
        if units:
            targets.extend((f'{prop} — {unit.label}', prop, unit) for unit in units)
        else:
            targets.append((str(prop), prop, None))
    return targets


def materialize_lines(session, template):
    """Snapshots the template's lines onto the new Session — the one place
    copying is correct, per this app's own build brief: once a Session
    exists its lines are frozen, and a template edited later only affects
    sessions opened after the edit."""
    if template.line_source == SessionTemplate.LineSource.STATIC:
        rows = [
            SessionLine(session=session, label=tl.label, display_order=tl.display_order)
            for tl in template.static_lines.all()
        ]
    else:
        rows = [
            SessionLine(session=session, label=label, property=prop, unit=unit, display_order=i)
            for i, (label, prop, unit) in enumerate(matching_targets(template))
        ]
    SessionLine.objects.bulk_create(rows)
    return rows


def _get_or_create_session(template, cursor):
    label = period_label_for(cursor, template.frequency)
    due = cursor + timedelta(days=template.due_offset_days) if template.due_offset_days else cursor
    session, created = Session.objects.get_or_create(
        template=template, period_key=cursor,
        defaults={
            'owner': template.owner,
            'department': template.department,
            'period_label': label,
            'opens_at': cursor,
            'due_at': due,
        },
    )
    if created:
        materialize_lines(session, template)
    return session, created


def generate_for_template(template, today=None):
    """Walks template.next_open_date forward through today, opening a
    Session for every period along the way — including ones inside a past
    active_from/active_until window, which are simply skipped (not
    fast-forwarded past: the cursor still advances one period at a time, a
    session for that period just never gets created). Returns the number of
    sessions actually created."""
    today = today or timezone.localdate()
    cursor = template.next_open_date
    created_count = 0

    while cursor <= today:
        if template.active_until and cursor > template.active_until:
            break
        in_window = not template.active_from or cursor >= template.active_from
        if in_window:
            with transaction.atomic():
                _session, created = _get_or_create_session(template, cursor)
                if created:
                    created_count += 1
        cursor = advance(cursor, template.frequency, template.workday_of_month)

    if cursor != template.next_open_date:
        template.next_open_date = cursor
        template.save(update_fields=['next_open_date'])

    return created_count


def generate_due_sessions(today=None):
    """Entry point for the scheduler job / management command — every
    active template, in turn."""
    today = today or timezone.localdate()
    total = 0
    for template in SessionTemplate.objects.filter(is_active=True):
        total += generate_for_template(template, today=today)
    return total


def preview_next_occurrences(template, count=3):
    """Resolved next N occurrence dates + labels for Phase 3's live preview
    — pure computation, no DB writes, safe to call against an unsaved or
    in-progress template edit."""
    if not template.frequency or not template.next_open_date:
        return []
    cursor = template.next_open_date
    out = []
    guard = 0
    while len(out) < count and guard < count + 50:
        guard += 1
        in_window = (
            (not template.active_from or cursor >= template.active_from)
            and (not template.active_until or cursor <= template.active_until)
        )
        if in_window:
            out.append({'date': cursor, 'label': period_label_for(cursor, template.frequency)})
        elif template.active_until and cursor > template.active_until:
            break
        cursor = advance(cursor, template.frequency, template.workday_of_month)
    return out
