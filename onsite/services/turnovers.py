"""The On-Site Turnovers board: every short-term rental, ONE list, seven days across starting from a day (today unless moved a week at a time). Each day shows who leaves and who
arrives and at what time, whether that time was changed (waiting for an answer or agreed), and whether a leaving guest has a cleaning behind them. A turnover (a check-out and a
check-in the same day) is always flagged. Quiet units (nothing checks in or out in the seven days) are hidden unless asked for.

Beside every property is its STATUS RIGHT NOW (see `statuses`): occupied, dirty or clean. The same data drawn for one day is the Day view (`day_view`): boxes for who checks out, who
checks in, what needs cleaning, what is clean and ready, and who is staying through.

Built from the same bookings, requests and visits the rest of On-Site reads (onsite/services/str_board.py); nothing new is stored."""
from datetime import time, timedelta

from django.conf import settings
from django.db.models import Prefetch, Q
from django.utils import timezone

from core.models import StaffProfile
from onsite.models import Booking, GuestRequest, Visit, VisitType
from onsite.services import coverage, str_board
from onsite.services.feeds import eligible_properties

DAYS = 7
STATUS_LOOKBACK_DAYS = 3          # a unit that was left this many days ago or less, with no finished cleaning behind it, is dirty; older than that, we cannot say (nobody recorded one) and call it clean
OCCUPIED, DIRTY, CLEAN = 'occupied', 'dirty', 'clean'
STATUSES = (DIRTY, OCCUPIED, CLEAN)
CLEANED_CODES = ('submitted', 'verified')

# The round mark on a checkout: green = a cleaner is on it, amber = a cleaning exists but nobody is on it, red = no cleaning at all.
CLEAN_MARKS = {'none': 'none', 'unassigned': 'open'}
CLEAN_TITLES = {'none': 'No cleaning scheduled', 'unassigned': 'A cleaning exists but nobody is on it'}


def _mins(dt):
    local = timezone.localtime(dt)
    return local.hour * 60 + local.minute


def short_clock(dt):
    """'11a', '4p', '6:30p': the compact form the board draws."""
    local = timezone.localtime(dt)
    hour = local.hour % 12 or 12
    return f'{hour}{"" if local.minute == 0 else f":{local.minute:02d}"}{"a" if local.hour < 12 else "p"}'


def _clock_time(t):
    hour = t.hour % 12 or 12
    return f'{hour}{"" if t.minute == 0 else f":{t.minute:02d}"}{"a" if t.hour < 12 else "p"}'


def cleaned(state):
    return bool(state) and state.get('code') in CLEANED_CODES


def _visual(visit, state):
    """How one cleaning reads on a badge: (tone, text), the same words on both views. `state` is str_board._cleaning_state."""
    code = state['code']
    who = visit.assignee_label() if visit and (visit.assigned_staff_id or visit.assigned_contact_id) else ''
    at = f' {_clock_time(visit.scheduled_start)}' if visit and visit.scheduled_start and code in ('assigned',) else ''
    if code in CLEANED_CODES:
        return 'good', 'cleaned'
    if code in ('assigned', 'in_progress'):
        return 'good', f'cleaning{at} · {who}' if who else 'cleaning'
    if code == 'later':
        return 'warn', state['label']
    if code == 'unassigned':
        return 'warn', 'needs a cleaner'
    return 'bad', 'no cleaning'


def _event(booking, kind, day, today, default_minutes):
    """One checkout or check-in on `day`: the time to show, whether it was changed, and (for a checkout) the cleaning behind it."""
    is_out = kind == 'checkout'
    normal = timezone.localtime(booking.check_out if is_out else booking.check_in)
    kinds = GuestRequest.CHECKOUT_KINDS if is_out else GuestRequest.CHECKIN_KINDS
    live = [r for r in booking.guest_requests.all() if r.kind in kinds and r.status != GuestRequest.Status.DECLINED]
    req = live[-1] if live else None
    shown = str_board._at(day, req.requested_time) if req else normal
    ev = {
        'booking': booking, 'kind': kind, 'req': req, 'guest': booking.guest_name or 'Guest',
        'time': short_clock(shown), 'long': str_board._fmt(shown), 'value': f'{shown:%H:%M}', 'normal': str_board._fmt(normal), 'minutes': _mins(shown), 'normal_minutes': _mins(normal),
        'state': 'pending' if req and req.status == GuestRequest.Status.PENDING else ('approved' if req else 'normal'),
        'cleaning': None, 'est': default_minutes, 'other': '',
    }
    if is_out:
        visit = str_board._active_visit(booking)
        state = str_board._cleaning_state(visit, day, today)
        code = state['code']
        ev['est'] = str_board._est_minutes(visit, default_minutes)
        tone, text = _visual(visit, state)
        ev['cleaning'] = {'code': code, 'mark': CLEAN_MARKS.get(code, ''), 'label': CLEAN_TITLES.get(code, state['label']), 'text': state['label'],
                          'visit': visit.pk if visit else '', 'who': visit.assignee_label() if visit and (visit.assigned_staff_id or visit.assigned_contact_id) else '',
                          'badge_tone': tone, 'badge': text}
    return ev


def cleaners():
    """The people a cleaning can be given to from the board: active cleaner staff and the cleaning contacts the visit screen offers."""
    from core.models import Contact
    out = [{'value': f'staff-{sp.pk}', 'label': sp.user.first_name or sp.user.get_username()} for sp in
           StaffProfile.objects.select_related('user').filter(user__is_active=True, role=StaffProfile.Role.CLEANER).order_by('user__first_name')]
    out += [{'value': f'contact-{c.pk}', 'label': c.name} for c in
            Contact.objects.filter(Q(contact_type=Contact.ContactType.VENDOR, trade__icontains='clean') | Q(contact_type=Contact.ContactType.ON_SITE_STAFF)).order_by('name')[:8]]
    return out[:8]


# ---------------------------------------------------------------- the status of a unit right now

def statuses(property_ids, now=None):
    """{(property_id, unit_id): {'status', 'prev', 'cleaning', 'prev_visit'}}: where each unit stands at `now`.

      occupied  a guest is in the unit now (a stay that has started and not ended).
      dirty     nobody is in it, the last guest left within STATUS_LOOKBACK_DAYS, and the cleaning after them is not finished (none scheduled, or one that is unassigned, scheduled or
                under way). Only a cleaning that has been submitted or verified makes it clean.
      clean     everything else: empty and cleaned, or empty with nobody recent to have left it dirty.
    A unit with no bookings at all is missing from the answer (the caller treats that as clean)."""
    now = now or timezone.now()
    since = now - timedelta(days=STATUS_LOOKBACK_DAYS)
    bookings = list(coverage.operational(Booking.objects.filter(
        status=Booking.Status.ACTIVE, property_id__in=list(property_ids), check_in__lte=now, check_out__gte=since,
    ), None).prefetch_related(
        Prefetch('visits', queryset=Visit.objects.select_related('assigned_staff__user', 'assigned_contact').prefetch_related('checklist_items')),
    ))
    out, last = {}, {}
    for b in bookings:
        key = (b.property_id, b.unit_id)
        if b.check_in <= now < b.check_out:
            out[key] = {'status': OCCUPIED, 'prev': None, 'cleaning': None, 'prev_visit': None}
        elif b.check_out <= now and (key not in last or b.check_out > last[key].check_out):
            last[key] = b
    for key, b in last.items():
        if key in out:
            continue
        visit = str_board._active_visit(b)
        state = str_board._cleaning_state(visit)
        out[key] = {'status': CLEAN if cleaned(state) else DIRTY, 'prev': b, 'cleaning': state, 'prev_visit': visit}
    return out


# ---------------------------------------------------------------- the board

def week(start=None, today=None, now=None, show_all=False, status_filter=''):
    """The seven days from `start` (today unless given). `rows` are the ones drawn: units with something checking in or out (or, with `status_filter`, every unit of that status; with
    `show_all`, every unit), sorted by when the first thing happens. `counts` are the statuses of ALL units; `hidden` is how many units are not drawn."""
    today = today or timezone.localdate()
    now = now or timezone.now()
    start = start or today
    days = [start + timedelta(days=i) for i in range(DAYS)]
    first = str_board._at(start, time.min)
    last = first + timedelta(days=DAYS)

    turnover = VisitType.objects.filter(slug='turnover').first()
    default_minutes = turnover.default_duration_minutes if turnover else str_board.FALLBACK_TURNOVER_MINUTES
    props = list(eligible_properties())
    bookings = list(coverage.operational(Booking.objects.filter(
        status=Booking.Status.ACTIVE, property_id__in=[p.pk for p in props], check_out__gte=first, check_in__lt=last,
    ), None).select_related('property', 'unit').prefetch_related(
        Prefetch('visits', queryset=Visit.objects.select_related('assigned_staff__user', 'assigned_contact').prefetch_related('checklist_items')),
        'guest_requests',
    ))
    now_status = statuses([p.pk for p in props], now)

    cells = {}      # (property_id, unit_id) -> {date: {'outs': [], 'ins': [], 'stay': booking or None}}

    def slot(b, d):
        return cells.setdefault((b.property_id, b.unit_id), {}).setdefault(d, {'outs': [], 'ins': [], 'stay': None})

    for b in bookings:
        out_day, in_day = timezone.localtime(b.check_out).date(), timezone.localtime(b.check_in).date()
        for d in days:
            if in_day < d < out_day:
                slot(b, d)['stay'] = b
        if out_day in days:
            slot(b, out_day)['outs'].append(_event(b, 'checkout', out_day, today, default_minutes))
        if in_day in days:
            slot(b, in_day)['ins'].append(_event(b, 'checkin', in_day, today, default_minutes))

    rows = []
    pending = outs = without_cleaning = open_cleanings = 0
    buffer = settings.STR_BOARD_BUFFER_MINUTES
    for p in props:
        for unit in ([u for u in p.units.all() if u.is_active] or [None]):
            key = (p.pk, unit.pk if unit else None)
            mine = cells.get(key, {})
            row_cells, first_at = [], None
            for i, d in enumerate(days):
                c = mine.get(d, {'outs': [], 'ins': [], 'stay': None})
                if c['outs'] and c['ins']:          # a turnover: each side needs to know the other's time to say whether a change works
                    c['outs'][0]['other'], c['ins'][0]['other'] = c['ins'][0]['minutes'], c['outs'][0]['minutes']
                    c['ins'][0]['est'] = c['outs'][0]['est']
                for ev in c['outs'] + c['ins']:
                    ev['buffer'] = buffer
                    pending += ev['state'] == 'pending'
                for ev in c['outs']:
                    outs += 1
                    without_cleaning += ev['cleaning']['code'] == 'none'
                    open_cleanings += ev['cleaning']['code'] == 'unassigned'
                kind = 'both' if c['outs'] and c['ins'] else ('out' if c['outs'] else ('in' if c['ins'] else ('stay' if c['stay'] else '')))
                if first_at is None and (c['outs'] or c['ins']):
                    first_at = (i, min(e['minutes'] for e in c['outs'] + c['ins']))
                row_cells.append({'date': d, 'today': d == today, 'kind': kind, **c})
            info = now_status.get(key) or {'status': CLEAN, 'prev': None, 'cleaning': None, 'prev_visit': None}
            rows.append({'label': str_board._label(p, unit), 'unit': unit.label if unit else '', 'property': p, 'cells': row_cells, 'status': info['status'], 'prev_cleaning': info['cleaning'],
                         'prev_visit': info['prev_visit'], 'first': first_at, 'busy': first_at is not None})
    rows.sort(key=lambda r: (r['first'] is None, r['first'] or (0, 0), r['label'].lower()))
    counts = {s: sum(1 for r in rows if r['status'] == s) for s in STATUSES}
    status_filter = status_filter if status_filter in STATUSES else ''
    if status_filter:
        shown = [r for r in rows if r['status'] == status_filter]
    elif show_all:
        shown = list(rows)
    else:
        shown = [r for r in rows if r['busy']]
    # The same board turned the other way round for a phone: one page per day, listing only the properties that have something on it (a check-out, a check-in or a guest in house).
    by_day = []
    for i, d in enumerate(days):
        items = [{'row': row, 'c': row['cells'][i]} for row in rows if row['cells'][i]['kind']]
        by_day.append({'date': d, 'today': d == today, 'items': items, 'count': len(items)})
    return {
        'start': start, 'end': days[-1], 'days': [{'date': d, 'today': d == today} for d in days], 'rows': shown, 'all_rows': rows, 'hidden': len(rows) - len(shown), 'counts': counts,
        'filter': status_filter, 'show_all': show_all, 'phone_days': by_day,
        'previous': start - timedelta(days=DAYS), 'next': start + timedelta(days=DAYS), 'is_current': start == today,
        'pending': pending, 'outs': outs, 'without_cleaning': without_cleaning, 'open_cleanings': open_cleanings, 'cleaners': cleaners(), 'total': len(rows),
    }


# ---------------------------------------------------------------- the day

def _ready_badge(state_cleaning, visit):
    """For a check-in: is the unit ready? `state_cleaning` is the cleaning state of the stay before it (None = nobody to clean after)."""
    if state_cleaning is None or cleaned(state_cleaning):
        return 'good', 'clean & ready'
    tone, text = _visual(visit, state_cleaning)
    code = state_cleaning['code']
    if code == 'none':
        return 'bad', 'not clean · no cleaning'
    if code == 'unassigned':
        return 'warn', 'cleaner needed'
    return 'warn', text


def day_view(day, today=None, now=None):
    """One day as the newspaper: {'summary', 'out', 'in', 'dirty', 'clean', 'occupied', 'day', ...}. The rows are the week board's own (same events, same cleaning states), cut to
    this day. For today the dirty and clean boxes are the units' status right now; for a later day a vacant unit is dirty only when the stay before it has no cleaning scheduled at all;
    for an earlier day nothing can be said about a vacant unit, so it counts as clean."""
    today = today or timezone.localdate()
    now = now or timezone.now()
    w = week(day, today, now)
    boxes = {'out': [], 'in': [], 'dirty': [], 'clean': [], 'occupied': []}
    turnovers = done = 0
    for r in w['all_rows']:
        c = r['cells'][0]
        base = {'row': r, 'name': r['property'].name, 'unit': r['unit'], 'label': r['label']}
        same = bool(c['outs'] and c['ins'])
        for e in c['outs']:
            cl = e['cleaning']
            done += cl['code'] in CLEANED_CODES
            boxes['out'].append({**base, 'e': e, 'same_day': same, 'sort': e['minutes']})
        for e in c['ins']:
            if c['outs']:
                out_ev = c['outs'][0]
                v = str_board._active_visit(out_ev['booking'])
                tone, text = _ready_badge(str_board._cleaning_state(v, day, today), v)
            elif day == today and r['status'] == DIRTY:
                tone, text = _ready_badge(r['prev_cleaning'], r['prev_visit'])
            else:
                tone, text = 'good', 'clean & ready'
            turnovers += same
            boxes['in'].append({**base, 'e': e, 'same_day': same, 'ready_tone': tone, 'ready': text, 'sort': e['minutes']})
        if c['outs'] or c['ins']:
            continue
        if c['stay']:
            b = c['stay']
            out_day = timezone.localtime(b.check_out).date()
            boxes['occupied'].append({**base, 'out_day': out_day, 'sort': out_day})
            continue
        if day == today:
            dirty = r['status'] == DIRTY
        elif day > today:
            dirty = bool(r['prev_cleaning']) and r['prev_cleaning']['code'] == 'none' and r['status'] == DIRTY
        else:
            dirty = False
        if r['status'] == OCCUPIED and day == today:       # in the unit now but the board has no stay for it today (a check-in earlier today shows under Checking in)
            boxes['occupied'].append({**base, 'out_day': None, 'sort': day})
        elif dirty:
            cs = r['prev_cleaning']
            tone, text = _visual(r['prev_visit'], cs) if cs else ('bad', 'no cleaning')
            boxes['dirty'].append({**base, 'tone': tone, 'text': text, 'sort': 0})
        else:
            boxes['clean'].append({**base, 'sort': 0})
    for k in boxes:
        boxes[k].sort(key=lambda x: (x['sort'], x['label'].lower()) if k in ('out', 'in', 'occupied') else x['label'].lower())
    summary = {'out': len(boxes['out']), 'in': len(boxes['in']), 'cleanings_done': done, 'turnovers': turnovers, 'staying': len(boxes['occupied'])}
    return {**boxes, 'day': day, 'is_today': day == today, 'summary': summary, 'previous': day - timedelta(days=1), 'next': day + timedelta(days=1), 'cleaners': w['cleaners'],
            'pending': w['pending'], 'without_cleaning': w['without_cleaning'], 'open_cleanings': w['open_cleanings'], 'now': now}
