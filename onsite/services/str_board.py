"""The short-term rental "Today" board: what is happening at every rental on
a given day, and — first — what needs a person to look.

Everything here is DERIVED each time it's asked for (nothing stored but the
hand-logged guest requests), from data the app already has: the bookings
(check-in / check-out, from the platform calendars or CSV imports), each
booking's turnover visit and its live state, and its checklist progress.

Per unit and day it works out: who checks out and when, who checks in and
when, the state of the cleaning between them, and then a list of "items" —
things to act on — ranked most urgent first:
  * no cleaning scheduled for a checkout, or nobody assigned to it
  * a cleaning that should be under way (or is running behind) for a
    same-day check-in
  * a turnover that is too tight for the cleaning it needs
  * every pending early check-in / late checkout request, already turned
    into an answer: can we say yes without colliding with the cleaning and
    the next guest?

Times: a booking's checkout/check-in come from the property's default times
(the platform calendars carry dates only), so they are a plan, not what the
guest told us. An APPROVED request moves that booking's time on the board.
Time-based warnings ("should have started") only apply to today; other days
show only the structural ones."""
from datetime import datetime, time, timedelta

from django.conf import settings
from django.db.models import Prefetch
from django.utils import timezone

from ..models import Booking, GuestRequest, Visit, VisitType
from .feeds import coverage_report, eligible_properties

# Item tones, most to least urgent, used to sort and to colour.
CRITICAL, WARNING, GOOD, INFO = 'critical', 'warning', 'good', 'info'
_RANK = {CRITICAL: 0, WARNING: 1, GOOD: 2, INFO: 3}

OK, TIGHT, CONFLICT, NOT_YET, MOOT, UNKNOWN = 'ok', 'tight', 'conflict', 'not_yet', 'moot', 'unknown'

FALLBACK_TURNOVER_MINUTES = 90


def _at(day, clock):
    return timezone.make_aware(datetime.combine(day, clock))


def _fmt(dt):
    """'2:00 PM' — no leading zero, portable across platforms."""
    local = timezone.localtime(dt)
    return f'{local.hour % 12 or 12}:{local:%M} {"AM" if local.hour < 12 else "PM"}'


def _minutes(delta):
    return int(delta.total_seconds() // 60)


def _duration(minutes):
    minutes = max(0, int(minutes))
    hours, rest = divmod(minutes, 60)
    if hours and rest:
        return f'{hours} hr {rest} min'
    return f'{hours} hr' if hours else f'{rest} min'


# --- the two answers a request needs -----------------------------------------

def late_checkout_verdict(requested_at, checkout_at, checkin_at, est_minutes, buffer_minutes, cleaning_begun):
    """(level, text) for a late-checkout request.
    checkin_at is the same-day arrival (None if nobody arrives that day)."""
    if requested_at <= checkout_at:
        return OK, 'That is no later than the normal checkout.'
    if cleaning_begun:
        return MOOT, 'The cleaning has already started or finished, so the guest has left.'
    if checkin_at is None:
        return OK, 'No one checks in that day.'
    left = _minutes(checkin_at - requested_at)
    if left >= est_minutes + buffer_minutes:
        return OK, f'Leaves {_duration(left)} to clean before check-in (cleaning takes about {_duration(est_minutes)}).'
    if left >= est_minutes:
        return TIGHT, f'Only {_duration(left)} to clean before check-in — the cleaning takes about {_duration(est_minutes)}, so almost no cushion.'
    return CONFLICT, f'Leaves {_duration(max(left, 0))} but the cleaning takes about {_duration(est_minutes)} — the next guest would arrive before it is done.'


def early_checkin_verdict(requested_at, checkin_at, ready_at, margin_minutes):
    """(level, text) for an early check-in request. ready_at is when the unit
    is expected to be clean and ready (None = we can't tell)."""
    if requested_at >= checkin_at:
        return OK, 'That is no earlier than the normal check-in.'
    if ready_at is None:
        return UNKNOWN, "No cleaning is scheduled for this unit's checkout, so we can't promise a ready time."
    if requested_at >= ready_at + timedelta(minutes=margin_minutes):
        return OK, f'The unit should be ready by about {_fmt(ready_at)}.'
    if requested_at >= ready_at:
        return TIGHT, f'The unit should be ready by about {_fmt(ready_at)} — barely in time.'
    return NOT_YET, f'The unit will not be ready until about {_fmt(ready_at)}.'


# --- building blocks -----------------------------------------------------------

def _active_visit(booking):
    live = [v for v in booking.visits.all() if v.status not in (Visit.Status.CANCELLED, Visit.Status.SKIPPED)]
    return max(live, key=lambda v: v.pk) if live else None


def _est_minutes(visit, default_minutes):
    minutes = visit.estimated_minutes() if visit is not None else 0
    return minutes or default_minutes


def _cleaning_state(visit):
    if visit is None:
        return {'code': 'none', 'label': 'No cleaning scheduled', 'tone': CRITICAL, 'visit': None}
    items = list(visit.checklist_items.all())
    done, total = sum(1 for i in items if i.is_completed), len(items)
    base = {'visit': visit, 'done': done, 'total': total}
    if visit.status == Visit.Status.VERIFIED:
        return {**base, 'code': 'verified', 'label': 'Clean, verified', 'tone': GOOD}
    if visit.status == Visit.Status.SUBMITTED:
        return {**base, 'code': 'submitted', 'label': 'Cleaned, awaiting review', 'tone': GOOD}
    who = visit.assignee_label()
    if visit.status == Visit.Status.IN_PROGRESS or visit.started_at:
        return {**base, 'code': 'in_progress', 'label': f'In progress · {who} · {done} of {total}', 'tone': INFO}
    if not (visit.assigned_staff_id or visit.assigned_contact_id):
        return {**base, 'code': 'unassigned', 'label': 'Unassigned', 'tone': WARNING}
    return {**base, 'code': 'assigned', 'label': f'Assigned · {who} · not started', 'tone': INFO}


def _requests_for(booking, kind):
    return [r for r in booking.guest_requests.all() if r.kind == kind and r.status != GuestRequest.Status.DECLINED]


def _effective_time(booking_dt, requests, day, later):
    """The booking's time on `day`, moved by an APPROVED request when it
    moves it the right way (later for a checkout, earlier for a check-in)."""
    best = booking_dt
    for r in requests:
        if r.status != GuestRequest.Status.APPROVED:
            continue
        candidate = _at(day, r.requested_time)
        if (later and candidate > best) or (not later and candidate < best):
            best = candidate
    return best


def _ready_at(row, now, is_today, est):
    """When the unit is expected to be clean and ready for a check-in on this
    row's day, or None when we can't say."""
    visit, state = row['visit'], row['cleaning']
    day_start = _at(row['day'], time.min)
    if row['checkout'] is None:
        # Nobody checked out today: the previous stay ended earlier. Ready
        # now unless its (older) cleaning is somehow still open — not tracked
        # here, so treat as ready.
        return max(now, day_start) if is_today else day_start
    if visit is None:
        return None
    if state['code'] in ('submitted', 'verified'):
        return visit.submitted_at or now
    if state['code'] == 'in_progress' and visit.started_at:
        return visit.started_at + timedelta(minutes=est)
    begin = row['checkout_at']
    if is_today and now > begin:
        begin = now
    return begin + timedelta(minutes=est)


def _label(prop, unit):
    return f'{prop.name} — {unit.label}' if unit else prop.name


# --- the board -----------------------------------------------------------------

def build_board(day=None, now=None, include_tomorrow=True):
    """Everything the Today board shows for `day` (default today) as of `now`.
    Returns a dict: day, is_today, rows, in_house, vacant, items, counts,
    tomorrow (a smaller board for the next day, only when day is today),
    calendar_gaps (Airbnb/VRBO calendar lines that aren't connected)."""
    now = now or timezone.now()
    local_now = timezone.localtime(now)
    today = local_now.date()
    day = day or today
    is_today = day == today
    buffer = settings.STR_BOARD_BUFFER_MINUTES
    margin = settings.STR_BOARD_EARLY_READY_MARGIN_MINUTES

    turnover = VisitType.objects.filter(slug='turnover').first()
    default_minutes = turnover.default_duration_minutes if turnover else FALLBACK_TURNOVER_MINUTES
    properties = list(eligible_properties())
    day_start = _at(day, time.min)
    day_end = day_start + timedelta(days=1)

    bookings = list(
        Booking.objects.filter(
            status=Booking.Status.ACTIVE, property_id__in=[p.pk for p in properties],
            check_out__gte=day_start, check_in__lt=day_end,
        ).select_related('property', 'unit').prefetch_related(
            Prefetch('visits', queryset=Visit.objects.select_related('assigned_staff__user', 'assigned_contact').prefetch_related('checklist_items')),
            'guest_requests',
        )
    )

    by_unit = {}
    for b in bookings:
        slot = by_unit.setdefault((b.property_id, b.unit_id), {'property': b.property, 'unit': b.unit, 'outs': [], 'ins': [], 'stays': []})
        if day_start <= b.check_out < day_end:
            slot['outs'].append(b)
        if day_start <= b.check_in < day_end:
            slot['ins'].append(b)
        if b.check_in < day_start and b.check_out >= day_end:
            slot['stays'].append(b)

    rows, in_house, items = [], [], []

    def add_item(tone, title, text, row, request=None, verdict=None):
        items.append({'tone': tone, 'rank': _RANK[tone], 'title': title, 'text': text, 'row': row,
                      'visit': row['visit'], 'request': request, 'verdict': verdict})

    for (_pid, _uid), slot in by_unit.items():
        co = min(slot['outs'], key=lambda b: b.check_out) if slot['outs'] else None
        ci = min(slot['ins'], key=lambda b: b.check_in) if slot['ins'] else None
        stay = slot['stays'][0] if slot['stays'] else None
        label = _label(slot['property'], slot['unit'])
        if co is None and ci is None:
            if stay:
                in_house.append({'label': label, 'property': slot['property'], 'unit': slot['unit'], 'booking': stay})
            continue

        visit = _active_visit(co) if co else None
        state = _cleaning_state(visit) if co else None
        est = _est_minutes(visit, default_minutes)
        co_at = timezone.localtime(co.check_out) if co else None
        ci_at = timezone.localtime(ci.check_in) if ci else None
        late_reqs = _requests_for(co, GuestRequest.Kind.LATE_CHECKOUT) if co else []
        early_reqs = _requests_for(ci, GuestRequest.Kind.EARLY_CHECKIN) if ci else []
        eff_out = _effective_time(co_at, late_reqs, day, later=True) if co else None
        eff_in = _effective_time(ci_at, early_reqs, day, later=False) if ci else None

        row = {
            'key': f'{slot["property"].pk}-{slot["unit"].pk if slot["unit"] else 0}', 'day': day, 'label': label,
            'property': slot['property'], 'unit': slot['unit'], 'checkout': co, 'checkin': ci, 'staying': stay,
            'visit': visit, 'cleaning': state, 'est_minutes': est,
            'checkout_at': eff_out, 'checkin_at': eff_in, 'scheduled_checkout_at': co_at, 'scheduled_checkin_at': ci_at,
            'late_checkout_moved': bool(co and eff_out != co_at), 'early_checkin_moved': bool(ci and eff_in != ci_at),
            'requests': [], 'turnover': bool(co and ci), 'gap_minutes': None,
        }
        row['sort_at'] = eff_out or eff_in

        # Requests, each with its answer.
        cleaning_begun = bool(visit and (visit.started_at or visit.status in (Visit.Status.IN_PROGRESS, Visit.Status.SUBMITTED, Visit.Status.VERIFIED)))
        for r in late_reqs:
            level, text = late_checkout_verdict(_at(day, r.requested_time), co_at, eff_in if ci else None, est, buffer, cleaning_begun)
            row['requests'].append({'request': r, 'verdict': level, 'text': text, 'kind': r.kind, 'at': _at(day, r.requested_time)})
        for r in early_reqs:
            level, text = early_checkin_verdict(_at(day, r.requested_time), ci_at, _ready_at(row, now, is_today, est), margin)
            row['requests'].append({'request': r, 'verdict': level, 'text': text, 'kind': r.kind, 'at': _at(day, r.requested_time)})

        # Cleaning-side items (only for a real checkout).
        if co and state['code'] not in ('submitted', 'verified'):
            urgent_side = bool(ci)
            if state['code'] == 'none':
                add_item(CRITICAL if urgent_side else WARNING, 'No cleaning scheduled',
                         f'{label} checks out {_fmt(eff_out)}{" and a guest arrives " + _fmt(eff_in) if ci else ""} — nothing is scheduled to clean it.', row)
            elif state['code'] == 'unassigned':
                add_item(CRITICAL if urgent_side else WARNING, 'Nobody assigned',
                         f'The cleaning for {label} ({"checkout " + _fmt(eff_out)}) has no cleaner yet.', row)
            if is_today and ci and state['code'] in ('assigned', 'unassigned', 'none'):
                start_by = max(eff_in - timedelta(minutes=est + buffer), eff_out)
                if now >= start_by:
                    arrival = (f'the next guest arrives {_fmt(eff_in)} ({_duration(_minutes(eff_in - now))} from now)'
                               if eff_in > now else f'the next guest was due {_fmt(eff_in)}')
                    add_item(CRITICAL, 'Cleaning should be under way',
                             f'{label}: about {_duration(est)} of cleaning and {arrival} — it has not started.', row)
            if is_today and ci and state['code'] == 'in_progress' and visit.started_at:
                projected = visit.started_at + timedelta(minutes=est)
                if projected > eff_in:
                    add_item(WARNING, 'Cleaning running behind',
                             f'{label}: started {_fmt(visit.started_at)}, about {_duration(est)} needed — expected done around '
                             f'{_fmt(projected)}, after the {_fmt(eff_in)} check-in.', row)

        # A turnover that is too tight regardless of anything being late.
        if co and ci and state['code'] not in ('submitted', 'verified'):
            gap = _minutes(eff_in - eff_out)
            row['gap_minutes'] = gap
            if gap < est:
                add_item(CRITICAL, 'Not enough time to turn the unit',
                         f'{label}: {_duration(max(gap, 0))} between checkout ({_fmt(eff_out)}) and check-in ({_fmt(eff_in)}), but the cleaning takes about {_duration(est)}.', row)
            elif gap < est + buffer:
                add_item(WARNING, 'Tight turnover',
                         f'{label}: {_duration(gap)} between checkout and check-in for a cleaning of about {_duration(est)}.', row)
        elif co and ci:
            row['gap_minutes'] = _minutes(eff_in - eff_out)

        # Pending requests are decisions.
        for entry in row['requests']:
            r = entry['request']
            if r.status != GuestRequest.Status.PENDING:
                continue
            kind_word = 'late checkout' if r.kind == GuestRequest.Kind.LATE_CHECKOUT else 'early check-in'
            tone = GOOD if entry['verdict'] in (OK, MOOT) else (WARNING if entry['verdict'] in (TIGHT, UNKNOWN) else CRITICAL)
            add_item(tone, f'{kind_word.capitalize()} request',
                     f'{label}: guest asked for a {kind_word} at {_fmt(entry["at"])}. {entry["text"]}', row, request=r, verdict=entry['verdict'])
        rows.append(row)

    rows.sort(key=lambda r: (r['sort_at'] or day_end, r['label']))
    items.sort(key=lambda i: (i['rank'], i['row']['sort_at'] or day_end, i['row']['label']))

    covered = {(r['property'].pk, r['unit'].pk if r['unit'] else None) for r in rows} | {
        (h['property'].pk, h['unit'].pk if h['unit'] else None) for h in in_house}
    vacant = []
    for prop in properties:
        active_units = [u for u in prop.units.all() if u.is_active]
        for unit in (active_units or [None]):
            if (prop.pk, unit.pk if unit else None) not in covered and (prop.pk, None) not in covered:
                vacant.append({'label': _label(prop, unit), 'property': prop, 'unit': unit})
    vacant.sort(key=lambda v: v['label'])

    outs = [r for r in rows if r['checkout']]
    board = {
        'day': day, 'is_today': is_today, 'now': local_now, 'rows': rows, 'in_house': in_house, 'vacant': vacant, 'items': items,
        'counts': {
            'checkouts': len(outs),
            'checkins': sum(1 for r in rows if r['checkin']),
            'cleanings_done': sum(1 for r in outs if r['cleaning']['code'] in ('submitted', 'verified')),
            'cleanings_total': len(outs),
            'attention': sum(1 for i in items if i['tone'] in (CRITICAL, WARNING)),
            'pending_requests': sum(1 for i in items if i['request'] is not None),
        },
        'tomorrow': None,
        'calendar_gaps': coverage_report()['attention'],
    }
    if include_tomorrow and is_today:
        board['tomorrow'] = build_board(day + timedelta(days=1), now, include_tomorrow=False)
    return board


# --- logging and deciding requests ------------------------------------------------

class RequestError(ValueError):
    """A request that can't be logged; the message is written for staff."""


def record_request(booking, kind, requested_time, note='', user=None):
    """Logs a guest's early check-in / late checkout request. It must move the
    time the right way, and a booking has at most one open request of each
    kind (decide or remove it before logging another)."""
    if kind not in GuestRequest.Kind.values:
        raise RequestError('Choose early check-in or late checkout.')
    if booking.status != Booking.Status.ACTIVE:
        raise RequestError('That reservation is cancelled.')
    if not isinstance(requested_time, time):
        raise RequestError('Enter the time the guest asked for.')
    if kind == GuestRequest.Kind.LATE_CHECKOUT:
        scheduled = timezone.localtime(booking.check_out)
        if _at(scheduled.date(), requested_time) <= scheduled:
            raise RequestError(f'A late checkout has to be after the normal checkout ({_fmt(scheduled)}).')
    else:
        scheduled = timezone.localtime(booking.check_in)
        if _at(scheduled.date(), requested_time) >= scheduled:
            raise RequestError(f'An early check-in has to be before the normal check-in ({_fmt(scheduled)}).')
    if GuestRequest.objects.filter(booking=booking, kind=kind).exclude(status=GuestRequest.Status.DECLINED).exists():
        raise RequestError('That reservation already has this kind of request — decide or remove it first.')
    return GuestRequest.objects.create(
        booking=booking, kind=kind, requested_time=requested_time, note=(note or '').strip()[:200], created_by=user,
    )


def decide_request(guest_request, decision, user=None):
    """'approve' or 'decline'. Approving moves that booking's time on the board."""
    if decision not in ('approve', 'decline'):
        raise RequestError('Choose approve or decline.')
    guest_request.status = GuestRequest.Status.APPROVED if decision == 'approve' else GuestRequest.Status.DECLINED
    guest_request.decided_by = user
    guest_request.decided_at = timezone.now()
    guest_request.save(update_fields=['status', 'decided_by', 'decided_at'])
    return guest_request
