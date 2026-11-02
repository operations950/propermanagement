"""The On-Site properties board: every short-term rental, grouped by city, seven days across. Each day shows who leaves and who arrives and at what time, whether that time was changed (waiting for an answer or agreed), and whether a leaving guest has a cleaning behind them. Built from the same bookings, requests and visits the Today board reads (onsite/services/str_board.py); nothing new is stored."""
from datetime import time, timedelta

from django.conf import settings
from django.db.models import Prefetch, Q
from django.utils import timezone

from core.models import StaffProfile
from onsite.models import Booking, GuestRequest, Visit, VisitType
from onsite.services import coverage, str_board
from onsite.services.feeds import eligible_properties

DAYS = 7

# The round mark on a checkout: green = a cleaner is on it, amber = a cleaning exists but nobody is on it, red = no cleaning at all.
CLEAN_MARKS = {'none': 'none', 'unassigned': 'open'}
CLEAN_TITLES = {'none': 'No cleaning scheduled', 'unassigned': 'A cleaning exists but nobody is on it'}


def _mins(dt):
    local = timezone.localtime(dt)
    return local.hour * 60 + local.minute


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
        ev['cleaning'] = {'code': code, 'mark': CLEAN_MARKS.get(code, ''), 'label': CLEAN_TITLES.get(code, state['label']), 'text': state['label'],
                          'visit': visit.pk if visit else '', 'who': visit.assignee_label() if visit and (visit.assigned_staff_id or visit.assigned_contact_id) else ''}
    return ev


def short_clock(dt):
    """'11a', '4p', '6:30p': the compact form the board draws."""
    local = timezone.localtime(dt)
    hour = local.hour % 12 or 12
    return f'{hour}{"" if local.minute == 0 else f":{local.minute:02d}"}{"a" if local.hour < 12 else "p"}'


def cleaners():
    """The people a cleaning can be given to from the board: active cleaner staff and the cleaning contacts the visit screen offers."""
    from core.models import Contact
    out = [{'value': f'staff-{sp.pk}', 'label': sp.user.first_name or sp.user.get_username()} for sp in
           StaffProfile.objects.select_related('user').filter(user__is_active=True, role=StaffProfile.Role.CLEANER).order_by('user__first_name')]
    out += [{'value': f'contact-{c.pk}', 'label': c.name} for c in
            Contact.objects.filter(Q(contact_type=Contact.ContactType.VENDOR, trade__icontains='clean') | Q(contact_type=Contact.ContactType.ON_SITE_STAFF)).order_by('name')[:8]]
    return out[:8]


def week(start=None, today=None):
    today = today or timezone.localdate()
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

    cities = {}
    pending = outs = without_cleaning = 0
    buffer = settings.STR_BOARD_BUFFER_MINUTES
    for p in props:
        for unit in ([u for u in p.units.all() if u.is_active] or [None]):
            mine = cells.get((p.pk, unit.pk if unit else None), {})
            row_cells = []
            for d in days:
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
                kind = 'both' if c['outs'] and c['ins'] else ('out' if c['outs'] else ('in' if c['ins'] else ('stay' if c['stay'] else '')))
                row_cells.append({'date': d, 'today': d == today, 'kind': kind, **c})
            cities.setdefault((p.city or '').strip() or 'No city', []).append({'label': str_board._label(p, unit), 'property': p, 'cells': row_cells})
    by_city = [(city, sorted(rows, key=lambda r: r['label'].lower())) for city, rows in sorted(cities.items(), key=lambda kv: (kv[0] == 'No city', kv[0].lower()))]
    return {
        'start': start, 'end': days[-1], 'days': [{'date': d, 'today': d == today} for d in days], 'by_city': by_city,
        'previous': start - timedelta(days=DAYS), 'next': start + timedelta(days=DAYS), 'is_current': start == today,
        'pending': pending, 'outs': outs, 'without_cleaning': without_cleaning, 'cleaners': cleaners(),
    }
