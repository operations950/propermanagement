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
from django.db.models import Prefetch, Q
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


def early_checkout_verdict(requested_at, checkout_at, assigned):
    """(level, text) for a guest leaving EARLIER than normal: always good news
    for the turnover."""
    saved = _minutes(checkout_at - requested_at)
    tail = ' and the cleaner can start then.' if assigned else ' — nobody is assigned to the cleaning yet, so it is a chance to line someone up early.'
    return OK, f'Leaving {_duration(saved)} early ({_fmt(requested_at)} instead of {_fmt(checkout_at)}){tail}'


def late_checkin_verdict(requested_at, checkin_at):
    """(level, text) for a guest arriving LATER than normal: more time to clean."""
    return OK, f'Arriving {_duration(_minutes(requested_at - checkin_at))} later ({_fmt(requested_at)} instead of {_fmt(checkin_at)}) gives the cleaner more time.'


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


def _requests_for(booking, kinds):
    """The live (not declined) time changes of these kinds for a booking."""
    return [r for r in booking.guest_requests.all() if r.kind in kinds and r.status != GuestRequest.Status.DECLINED]


def _effective_time(booking_dt, requests, day):
    """The booking's time on `day`, moved (earlier or later) by an APPROVED
    change. A booking has at most one live change per time; if history left
    several, the newest approved one stands."""
    best = booking_dt
    for r in requests:
        if r.status == GuestRequest.Status.APPROVED:
            best = _at(day, r.requested_time)
    return best


def _time_tone(delta_minutes, good_when_negative, tight):
    """Colour for a checkout or check-in time compared with normal. Leaving
    early or arriving late is good (green): more time to clean. Leaving late or
    arriving early is worse (amber, red when the turnover no longer fits)."""
    if not delta_minutes:
        return 'neutral'
    good = (delta_minutes < 0) == good_when_negative
    if good:
        return GOOD
    return CRITICAL if tight else WARNING


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


# --- arrivals at a unit nobody checked out of today ------------------------------------

def _arrival_readiness(prop, unit, day_start):
    """For a unit with a check-in but no checkout on the day: is it clean?
    "Clean" only when a turnover (or deep-clean) visit at the unit has been
    submitted since the last guest left; the visit is returned so the board can
    link to it. Otherwise the state of the last checkout's cleaning if one is
    still open, or the plain fact that none is recorded."""
    last = (Booking.objects.filter(property=prop, unit=unit, status=Booking.Status.ACTIVE, check_out__lt=day_start)
            .order_by('-check_out').prefetch_related(
                Prefetch('visits', queryset=Visit.objects.select_related('assigned_staff__user', 'assigned_contact').prefetch_related('checklist_items')))
            .first())
    if last is None:
        return {'code': 'unknown', 'label': 'No earlier stay on record', 'tone': INFO, 'visit': None}
    since = _at(timezone.localtime(last.check_out).date(), time.min)
    done = [
        v for v in Visit.objects.filter(property=prop, unit=unit, status__in=(Visit.Status.SUBMITTED, Visit.Status.VERIFIED))
        .filter(Q(visit_type__slug='turnover') | Q(is_deep_clean=True)).select_related('visit_type')
        if (v.verified_at or v.submitted_at) and (v.verified_at or v.submitted_at) >= since
    ]
    if done:
        best = max(done, key=lambda v: v.submitted_at or v.verified_at)
        when = best.submitted_at or best.verified_at
        return {'code': 'clean', 'label': 'Clean', 'tone': GOOD, 'visit': best, 'when': when,
                'title': f'Cleaned {timezone.localtime(when):%a %b} {timezone.localtime(when).day}, {_fmt(when)}'}
    open_visit = _active_visit(last)
    if open_visit is not None:
        state = _cleaning_state(open_visit)
        return {**state, 'title': 'The cleaning after the last guest is not finished yet.'}
    return {'code': 'none', 'label': 'No cleaning recorded', 'tone': WARNING, 'visit': None,
            'title': f'Nothing on record since the last guest left {timezone.localtime(last.check_out):%b} {timezone.localtime(last.check_out).day}.'}


# --- vacant units: are they clean, and when were they last cleaned -----------------

def _vacancy_details(vacant, property_ids, ref, now, add_item):
    """Enriches each vacant unit with its cleaning state: whether it is clean,
    when it was last cleaned, when the last guest left, and when the next one
    arrives. "Clean" means a cleaning was submitted/verified at or after the
    last checkout. A vacant unit that is NOT clean, has no cleaning lined up
    and has a guest arriving within 3 days is flagged."""
    bookings = list(Booking.objects.filter(status=Booking.Status.ACTIVE, property_id__in=property_ids)
                    .values('property_id', 'unit_id', 'check_in', 'check_out'))
    visits = list(Visit.objects.filter(property_id__in=property_ids)
                  .exclude(status__in=(Visit.Status.CANCELLED, Visit.Status.SKIPPED))
                  .values('pk', 'property_id', 'unit_id', 'status', 'submitted_at', 'verified_at', 'scheduled_date'))
    for entry in vacant:
        pid, uid = entry['property'].pk, entry['unit'].pk if entry['unit'] else None
        mine_b = [b for b in bookings if b['property_id'] == pid and b['unit_id'] == uid]
        mine_v = [v for v in visits if v['property_id'] == pid and v['unit_id'] == uid]
        past = [b['check_out'] for b in mine_b if b['check_out'] <= ref]
        future = [b['check_in'] for b in mine_b if b['check_in'] > ref]
        last_out = max(past) if past else None
        next_in = min(future) if future else None
        done = [v['verified_at'] or v['submitted_at'] for v in mine_v
                if v['status'] in (Visit.Status.SUBMITTED, Visit.Status.VERIFIED) and (v['verified_at'] or v['submitted_at'])]
        last_cleaned = max(done) if done else None
        open_visits = [v for v in mine_v if v['status'] in (Visit.Status.SCHEDULED, Visit.Status.UNASSIGNED, Visit.Status.IN_PROGRESS)]

        if last_cleaned and (last_out is None or last_cleaned >= last_out):
            state, label, tone = 'clean', 'Clean', GOOD
        elif last_out is None:
            state, label, tone = 'unknown', 'No stays on record', INFO
        elif open_visits:
            state, label, tone = 'pending', 'Cleaning lined up', INFO
        else:
            state, label, tone = 'dirty', 'Not clean — no cleaning scheduled', WARNING
        soon = next_in is not None and next_in - now <= timedelta(days=3)
        if state == 'dirty' and soon:
            tone = CRITICAL
        entry.update({
            'state': state, 'state_label': label, 'tone': tone, 'last_cleaned_at': last_cleaned, 'last_checkout_at': last_out,
            'next_checkin_at': next_in, 'key': f'v-{pid}-{uid or 0}',
            'days_since_clean': (timezone.localtime(now).date() - timezone.localtime(last_cleaned).date()).days if last_cleaned else None,
            'days_vacant': (timezone.localtime(now).date() - timezone.localtime(last_out).date()).days if last_out else None,
        })
        if state == 'dirty' and soon:
            pseudo = {'key': entry['key'], 'label': entry['label'], 'visit': None, 'sort_at': timezone.localtime(next_in)}
            add_item(CRITICAL, 'Vacant unit not clean',
                     f'{entry["label"]} is empty and hasn\'t been cleaned since the last guest left, with no cleaning scheduled — '
                     f'a guest arrives {_fmt(next_in)} on {timezone.localtime(next_in):%a %b} {timezone.localtime(next_in).day}.', pseudo)


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

    next_in = {}
    for row in (Booking.objects.filter(status=Booking.Status.ACTIVE, property_id__in=[p.pk for p in properties], check_in__gte=day_end)
                .order_by('check_in').values('property_id', 'unit_id', 'check_in')):
        next_in.setdefault((row['property_id'], row['unit_id']), row['check_in'])

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
        out_reqs = _requests_for(co, GuestRequest.CHECKOUT_KINDS) if co else []
        in_reqs = _requests_for(ci, GuestRequest.CHECKIN_KINDS) if ci else []
        eff_out = _effective_time(co_at, out_reqs, day) if co else None
        eff_in = _effective_time(ci_at, in_reqs, day) if ci else None
        out_delta = _minutes(eff_out - co_at) if co else 0
        in_delta = _minutes(eff_in - ci_at) if ci else 0
        tight_turn = bool(co and ci and _minutes(eff_in - eff_out) < est)

        row = {
            'key': f'{slot["property"].pk}-{slot["unit"].pk if slot["unit"] else 0}', 'day': day, 'label': label,
            'property': slot['property'], 'unit': slot['unit'], 'checkout': co, 'checkin': ci, 'staying': stay,
            'visit': visit, 'cleaning': state, 'est_minutes': est,
            'checkout_at': eff_out, 'checkin_at': eff_in, 'scheduled_checkout_at': co_at, 'scheduled_checkin_at': ci_at,
            'late_checkout_moved': bool(co and eff_out > co_at), 'early_checkin_moved': bool(ci and eff_in < ci_at),
            'checkout_delta': out_delta, 'checkin_delta': in_delta,
            'checkout_tone': _time_tone(out_delta, True, tight_turn), 'checkin_tone': _time_tone(in_delta, False, tight_turn),
            'checkout_shift': _duration(abs(out_delta)) if out_delta else '', 'checkin_shift': _duration(abs(in_delta)) if in_delta else '',
            'requests': [], 'turnover': bool(co and ci), 'gap_minutes': None,
        }
        row['sort_at'] = eff_out or eff_in
        # What order the board reads in: same-day turnovers first, then other
        # cleanings, then arrivals into units that need no cleaning that day;
        # inside each, by when the next guest walks in.
        row['group'] = 0 if (co and ci) else (1 if co else 2)
        following = next_in.get((slot['property'].pk, slot['unit'].pk if slot['unit'] else None))
        row['next_checkin_at'] = eff_in if ci else (timezone.localtime(following) if following else None)
        row['next_checkin_days'] = (row['next_checkin_at'].date() - day).days if row['next_checkin_at'] else None
        row['arrival_ready'] = _arrival_readiness(slot['property'], slot['unit'], day_start) if (ci and not co) else None

        # Requests, each with its answer.
        cleaning_begun = bool(visit and (visit.started_at or visit.status in (Visit.Status.IN_PROGRESS, Visit.Status.SUBMITTED, Visit.Status.VERIFIED)))
        assigned = bool(visit and (visit.assigned_staff_id or visit.assigned_contact_id))
        for r in out_reqs + in_reqs:
            at_time = _at(day, r.requested_time)
            if r.kind == GuestRequest.Kind.LATE_CHECKOUT:
                level, text = late_checkout_verdict(at_time, co_at, eff_in if ci else None, est, buffer, cleaning_begun)
            elif r.kind == GuestRequest.Kind.EARLY_CHECKOUT:
                level, text = early_checkout_verdict(at_time, co_at, assigned)
            elif r.kind == GuestRequest.Kind.LATE_CHECKIN:
                level, text = late_checkin_verdict(at_time, ci_at)
            else:
                level, text = early_checkin_verdict(at_time, ci_at, _ready_at(row, now, is_today, est), margin)
            row['requests'].append({'request': r, 'verdict': level, 'text': text, 'kind': r.kind, 'at': at_time})

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
            kind_word = r.get_kind_display().lower()
            tone = GOOD if entry['verdict'] in (OK, MOOT) else (WARNING if entry['verdict'] in (TIGHT, UNKNOWN) else CRITICAL)
            add_item(tone, f'{kind_word.capitalize()} request',
                     f'{label}: guest asked for {"an" if kind_word[0] in "ae" else "a"} {kind_word} at {_fmt(entry["at"])}. {entry["text"]}', row, request=r, verdict=entry['verdict'])
        rows.append(row)

    far = day_end + timedelta(days=3650)
    rows.sort(key=lambda r: (r['group'], r['next_checkin_at'] or far, r['sort_at'] or day_end, r['label']))
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
    _vacancy_details(vacant, [p.pk for p in properties], max(now, day_start), now, add_item)
    vacant.sort(key=lambda v: (v['next_checkin_at'] is None, v['next_checkin_at'] or far, v['label']))
    items.sort(key=lambda i: (i['rank'], i['row']['sort_at'] or day_end, i['row']['label']))

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
    """Logs a change to a booking's checkout or check-in time — earlier or later.
    `kind` is 'checkout' or 'checkin' (the direction is worked out from the time
    against the normal one) or one of the four specific kinds, which must match
    the direction. A booking has at most one live change per time (decide or
    remove it before logging another). A change that helps the turnover — a
    guest leaving early or arriving late — is simply approved and the cleaner
    told; one that hurts (leaving late, arriving early) waits for a decision."""
    if kind not in GuestRequest.Kind.values and kind not in ('checkout', 'checkin'):
        raise RequestError('Choose whether the checkout or the check-in time is changing.')
    if booking.status != Booking.Status.ACTIVE:
        raise RequestError('That reservation is cancelled.')
    if not isinstance(requested_time, time):
        raise RequestError('Enter the time the guest asked for.')
    is_checkout = kind == 'checkout' or kind in GuestRequest.CHECKOUT_KINDS
    scheduled = timezone.localtime(booking.check_out if is_checkout else booking.check_in)
    requested_dt = _at(scheduled.date(), requested_time)
    word = 'checkout' if is_checkout else 'check-in'
    if requested_dt == scheduled:
        raise RequestError(f'That is the normal {word} time ({_fmt(scheduled)}) — nothing to change.')
    later = requested_dt > scheduled
    inferred = (
        (GuestRequest.Kind.LATE_CHECKOUT if later else GuestRequest.Kind.EARLY_CHECKOUT) if is_checkout
        else (GuestRequest.Kind.LATE_CHECKIN if later else GuestRequest.Kind.EARLY_CHECKIN)
    )
    if kind in GuestRequest.Kind.values and kind != inferred:
        if kind == GuestRequest.Kind.LATE_CHECKOUT:
            raise RequestError(f'A late checkout has to be after the normal checkout ({_fmt(scheduled)}).')
        if kind == GuestRequest.Kind.EARLY_CHECKIN:
            raise RequestError(f'An early check-in has to be before the normal check-in ({_fmt(scheduled)}).')
        raise RequestError(f'That time is {"later" if later else "earlier"} than the normal {word} ({_fmt(scheduled)}), so it is not an {GuestRequest.Kind(kind).label.lower()}.')
    live = GuestRequest.objects.filter(booking=booking, kind__in=GuestRequest.CHECKOUT_KINDS if is_checkout else GuestRequest.CHECKIN_KINDS)
    if live.exclude(status=GuestRequest.Status.DECLINED).exists():
        raise RequestError(f'That reservation already has this kind of request (a {word} time change) — decide or remove it first.')
    helpful = inferred in GuestRequest.HELPFUL_KINDS
    created = GuestRequest.objects.create(
        booking=booking, kind=inferred, requested_time=requested_time, note=(note or '').strip()[:200], created_by=user,
        status=GuestRequest.Status.APPROVED if helpful else GuestRequest.Status.PENDING,
        decided_by=user if helpful else None, decided_at=timezone.now() if helpful else None,
    )
    if helpful:
        tell_cleaners(created)
    return created


def tell_cleaners(guest_request):
    """After a time change is approved: message the cleaner of each affected
    visit (the one for this booking's checkout, or the one before this booking's
    check-in) who already has their link. Best-effort."""
    from django.db import transaction
    from django.db.models import Q
    from .notify import notify_time_change

    booking_id = guest_request.booking_id
    pks = list(Visit.objects.filter(Q(booking_id=booking_id) | Q(next_booking_id=booking_id)).values_list('pk', flat=True))
    for pk in pks:
        transaction.on_commit(lambda pk=pk: notify_time_change(Visit(pk=pk)))


def decide_request(guest_request, decision, user=None):
    """'approve' or 'decline'. Approving moves that booking's time on the board."""
    if decision not in ('approve', 'decline'):
        raise RequestError('Choose approve or decline.')
    guest_request.status = GuestRequest.Status.APPROVED if decision == 'approve' else GuestRequest.Status.DECLINED
    guest_request.decided_by = user
    guest_request.decided_at = timezone.now()
    guest_request.save(update_fields=['status', 'decided_by', 'decided_at'])
    if guest_request.status == GuestRequest.Status.APPROVED:
        tell_cleaners(guest_request)
    return guest_request
