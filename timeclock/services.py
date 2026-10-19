"""Time clock logic: who is scheduled when, clocking in and out, pay-period hours, vacation balances, and what is waiting for a manager.

Views stay thin; every rule an employee or manager would ask about is a function here."""
import csv
import io
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from . import policy
from .models import (HolidayPay, OfficeConnection, PayPeriodLock, Punch, PunchChange, PunchRequest, ScheduleChange, Status, TimeOff,
                     TimeProfile)


class ClockError(Exception):
    """Something the person can fix: shown to them as written."""


# ------------------------------------------------------------------ who, and from when

def profile_for(user):
    """The signed-in user's TimeProfile, or None when the time clock does not apply to them."""
    staff = getattr(user, 'staff_profile', None)
    if not staff:
        return None
    try:
        profile = staff.time_profile
    except TimeProfile.DoesNotExist:
        return None
    return profile if profile.applies else None


def tracked_profiles():
    return list(TimeProfile.objects.filter(applies=True).select_related('staff__user').order_by('staff__user__first_name', 'staff__user__last_name'))


def tracking_from(profile):
    """The first day the clock counts for this person: not before the policy start, the day it was switched on for them, or their hire date."""
    start = max(policy.POLICY_START, profile.tracking_start or policy.POLICY_START)
    if profile.hire_date and profile.hire_date > start:
        start = profile.hire_date
    return start


def daterange(start, end):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


# ------------------------------------------------------------------ the schedule

class Roster:
    """Who is scheduled, off, or asked to be off, for a set of people over a stretch of days (loaded once, so a calendar is a handful of queries)."""

    def __init__(self, profiles, start, end):
        self.profiles = list(profiles)
        ids = [p.id for p in self.profiles]
        self.changes = defaultdict(list)
        for c in ScheduleChange.objects.filter(profile_id__in=ids, status__in=[Status.APPROVED, Status.PENDING]).filter(
                Q(drop_date__range=(start, end)) | Q(add_date__range=(start, end))):
            self.changes[c.profile_id].append(c)
        self.approved_off = defaultdict(dict)
        self.pending_off = defaultdict(dict)
        for t in TimeOff.objects.filter(profile_id__in=ids, status__in=[Status.APPROVED, Status.PENDING], start_date__lte=end, end_date__gte=start):
            target = self.approved_off if t.status == Status.APPROVED else self.pending_off
            for d in t.dates:
                target[t.profile_id][d] = t

    def scheduled(self, p, d):
        """True when p is meant to work on d: the regular days, moved by approved changes (the office holiday is separate)."""
        works = d.weekday() in p.days
        for c in self.changes[p.id]:
            if c.status != Status.APPROVED:
                continue
            if c.drop_date == d:
                works = False
            if c.add_date == d:
                works = True
        return works

    def status(self, p, d):
        """(kind, object) for one person on one day. kind is one of: closed, vacation, off, working, pending_off, pending_drop, pending_add, or ''."""
        if p.hire_date and d < p.hire_date:
            return '', None
        if policy.holiday_name(d):
            return 'closed', None
        off = self.approved_off[p.id].get(d)
        if off:
            return ('vacation' if off.kind == TimeOff.Kind.VACATION else 'off'), off
        if self.scheduled(p, d):
            pending = self.pending_off[p.id].get(d)
            if pending:
                return 'pending_off', pending
            for c in self.changes[p.id]:
                if c.status == Status.PENDING and c.drop_date == d:
                    return 'pending_drop', c
            return 'working', None
        for c in self.changes[p.id]:
            if c.status == Status.PENDING and c.add_date == d:
                return 'pending_add', c
        return '', None


def covered_by_others(profile, d, roster):
    """True when someone else is scheduled to work that day (used to warn when a request would leave the office empty)."""
    return any(q.id != profile.id and roster.status(q, d)[0] == 'working' for q in roster.profiles)


def working_days_between(profile, start, end):
    """The scheduled working days from start to end, leaving out office holidays: what a time-off request covers."""
    roster = Roster([profile], start, end)
    return [d for d in daterange(start, end) if not policy.holiday_name(d) and roster.scheduled(profile, d) and not (profile.hire_date and d < profile.hire_date)]


# ------------------------------------------------------------------ the clock

def office_configured():
    return OfficeConnection.objects.exists()


def at_office(ip):
    """True when this connection is the office (or when no office connection has been set yet: nothing is flagged until one is)."""
    return (not office_configured()) or OfficeConnection.objects.filter(ip=ip).exists()


def today_punch(profile, today=None):
    return Punch.objects.filter(profile=profile, day=today or timezone.localdate()).first()


def open_punches(profile, before=None):
    """Punches never clocked out, from days before today."""
    return list(Punch.objects.filter(profile=profile, clock_out__isnull=True, day__lt=before or timezone.localdate()).order_by('day'))


def clock_in(profile, ip, reason=''):
    today = timezone.localdate()
    if Punch.objects.filter(profile=profile, day=today).exists():
        raise ClockError('You already clocked in today.')
    remote = not at_office(ip)
    reason = (reason or '').strip()[:200]
    if remote and not reason:
        raise ClockError('You are not on the office connection. Say why you are clocking in remotely.')
    return Punch.objects.create(profile=profile, day=today, clock_in=timezone.now(), in_remote=remote, in_ip=ip or '', remote_reason=reason if remote else '')


def clock_out(profile, ip, lunch, reason=''):
    punch = Punch.objects.filter(profile=profile, day=timezone.localdate(), clock_out__isnull=True).first()
    if not punch:
        raise ClockError('You are not clocked in today.')
    try:
        lunch = int(lunch)
    except (TypeError, ValueError):
        raise ClockError('Tell us about lunch: pick "No lunch" or how many minutes.')
    if not 0 <= lunch <= 240:
        raise ClockError('Lunch has to be between 0 and 240 minutes.')
    remote = not at_office(ip)
    reason = (reason or '').strip()[:200]
    if remote and not (reason or punch.remote_reason):
        raise ClockError('You are not on the office connection. Say why you are clocking out remotely.')
    punch.clock_out = timezone.now()
    punch.lunch_minutes = lunch
    punch.out_remote = remote
    punch.out_ip = ip or ''
    if remote and reason:
        punch.remote_reason = reason if not punch.remote_reason else f'{punch.remote_reason} / {reason}'[:200]
    punch.save()
    return punch


def local_dt(day, t):
    return timezone.make_aware(datetime.combine(day, t))


def save_punch(profile, day, clock_in_t, clock_out_t, lunch, by, reason):
    """A manager's fix (or entry) of one day's punch, with the old values kept as history. Blank times keep what is there."""
    if lunch is not None and not 0 <= lunch <= 240:
        raise ClockError('Lunch has to be between 0 and 240 minutes.')
    punch = Punch.objects.filter(profile=profile, day=day).first()
    new_in = local_dt(day, clock_in_t) if clock_in_t else (punch.clock_in if punch else None)
    new_out = local_dt(day, clock_out_t) if clock_out_t else (punch.clock_out if punch else None)
    new_lunch = lunch if lunch is not None else (punch.lunch_minutes if punch else None)
    if not new_in:
        raise ClockError('A day needs a clock-in time.')
    if new_out and new_out <= new_in:
        raise ClockError('The clock-out has to be after the clock-in.')
    if new_out and new_lunch is None:
        raise ClockError('Say how long the lunch was (0 for none).')
    if punch is None:
        punch = Punch.objects.create(profile=profile, day=day, clock_in=new_in, clock_out=new_out, lunch_minutes=new_lunch, added_by=by)
        PunchChange.objects.create(punch=punch, by=by, reason=reason[:300], new_in=new_in, new_out=new_out, new_lunch=new_lunch)
        return punch
    PunchChange.objects.create(punch=punch, by=by, reason=reason[:300], old_in=punch.clock_in, old_out=punch.clock_out, old_lunch=punch.lunch_minutes,
                               new_in=new_in, new_out=new_out, new_lunch=new_lunch)
    punch.clock_in, punch.clock_out, punch.lunch_minutes = new_in, new_out, new_lunch
    punch.save()
    return punch


def period_locked(d):
    return PayPeriodLock.objects.filter(start=policy.pay_period(d)[0]).exists()


# ------------------------------------------------------------------ hours for a pay period

def week_totals(profile, start, end, today):
    """The hours in a stretch of days: punched minutes, approved vacation/sick hours, approved holiday hours."""
    worked = sum(p.minutes or 0 for p in Punch.objects.filter(profile=profile, day__range=(start, end), clock_out__isnull=False))
    off = 0
    for t in TimeOff.objects.filter(profile=profile, status=Status.APPROVED, start_date__lte=end, end_date__gte=start):
        if t.takes_balance:
            off += sum(policy.DAY_HOURS for d in t.dates if start <= d <= end)
    holiday = sum(h.hours for h in HolidayPay.objects.filter(profile=profile, status=Status.APPROVED, day__range=(start, end)))
    return {'worked_min': worked, 'off_hours': Decimal(off), 'holiday_hours': Decimal(holiday)}


def week_context(profile, holiday_day):
    """For a holiday-pay decision: the hours that Monday-Sunday week without the holiday (worked + approved vacation/sick + other approved holiday pay)."""
    ws = policy.week_start(holiday_day)
    t = week_totals(profile, ws, ws + timedelta(days=6), timezone.localdate())
    others = HolidayPay.objects.filter(profile=profile, status=Status.APPROVED, day__range=(ws, ws + timedelta(days=6))).exclude(day=holiday_day)
    holiday_other = sum(h.hours for h in others)
    t['holiday_hours'] = Decimal(holiday_other)
    return round(Decimal(t['worked_min']) / 60 + t['off_hours'] + t['holiday_hours'], 2)


def _holiday_dates(profile, start, end):
    """The holidays that need an hourly holiday-pay decision: office holidays on one of the person's regular working days."""
    found = []
    for year in range(start.year, end.year + 1):
        for d, name in policy.holidays(year).items():
            if start <= d <= end and d.weekday() in profile.days and not (profile.hire_date and d < profile.hire_date):
                found.append((d, name))
    return sorted(found)


class PeriodReport:
    """One person's pay period: a row per day, two Monday-Sunday weeks, totals, and whatever still has to be settled before payroll."""

    def __init__(self, profile, start, today=None):
        self.profile = profile
        self.start = start
        self.end = start + timedelta(days=13)
        self.today = today or timezone.localdate()
        punches = {p.day: p for p in Punch.objects.filter(profile=profile, day__range=(start, self.end)).prefetch_related('changes')}
        roster = Roster([profile], start, self.end)
        hp = {h.day: h for h in HolidayPay.objects.filter(profile=profile, day__range=(start, self.end))}
        holiday_days = {d for d, _ in _holiday_dates(profile, start, self.end)}
        first = tracking_from(profile)
        self.rows = []
        self.blockers = []
        for d in daterange(start, self.end):
            row = {'date': d, 'punch': punches.get(d), 'kind': 'none', 'worked_min': 0, 'off_hours': Decimal(0), 'holiday_hours': Decimal(0),
                   'holiday': policy.holiday_name(d), 'hp': hp.get(d), 'off': None, 'needs': ''}
            punch = row['punch']
            kind, obj = roster.status(profile, d)
            if punch:
                row['worked_min'] = punch.minutes or 0
                row['kind'] = 'worked' if punch.clock_out else ('in' if d >= self.today else 'open')
                if row['kind'] == 'open':
                    row['needs'] = 'No clock-out'
            elif kind in ('vacation', 'off'):
                row['kind'] = 'vacation' if obj.kind == TimeOff.Kind.VACATION else obj.kind
                row['off'] = obj
            elif kind == 'pending_off':
                row['kind'] = 'pending_off'
                row['needs'] = 'Time off waiting for a decision'
            elif kind == 'closed':
                row['kind'] = 'holiday'
            elif kind == 'working' and first <= d < self.today:
                row['kind'] = 'missed'
                row['needs'] = 'Missed day'
            if row['kind'] in ('vacation', 'sick') and obj and obj.takes_balance:
                row['off_hours'] = Decimal(policy.DAY_HOURS)
            if profile.is_hourly and d in holiday_days and d >= first:
                row['kind'] = 'holiday' if row['kind'] == 'none' else row['kind']
                h = row['hp']
                if h and h.status == Status.APPROVED:
                    row['holiday_hours'] = h.hours
                elif not h and d <= self.today:
                    row['needs'] = 'Holiday pay waiting for a decision'
            if row['needs']:
                self.blockers.append((d, row['needs']))
            self.rows.append(row)
        for r in PunchRequest.objects.filter(profile=profile, day__range=(start, self.end), status=Status.PENDING):
            self.blockers.append((r.day, 'Punch fix waiting for a decision'))
        self.weeks = []
        for i in (0, 7):
            rows = self.rows[i:i + 7]
            worked = sum(r['worked_min'] for r in rows)
            off = sum((r['off_hours'] for r in rows), Decimal(0))
            hol = sum((r['holiday_hours'] for r in rows), Decimal(0))
            worked_h = round(Decimal(worked) / 60, 2)
            paid = worked_h + off + hol
            self.weeks.append({'rows': rows, 'worked_min': worked, 'worked_h': worked_h, 'off_hours': off, 'holiday_hours': hol, 'paid': paid, 'over': paid > 40,
                               'start': rows[0]['date'], 'end': rows[-1]['date']})
        self.worked_min = sum(w['worked_min'] for w in self.weeks)
        self.off_hours = sum((w['off_hours'] for w in self.weeks), Decimal(0))
        self.holiday_hours = sum((w['holiday_hours'] for w in self.weeks), Decimal(0))
        self.paid = sum((w['paid'] for w in self.weeks), Decimal(0))
        self.blockers.sort()
        self.locked = PayPeriodLock.objects.filter(start=start).exists()

    @property
    def worked_hours(self):
        """Hours worked, the sum of the two weeks' hundredths, so it always adds up to the paid total."""
        return sum((w['worked_h'] for w in self.weeks), Decimal(0))


def hours_csv(reports):
    """The pay period as a spreadsheet: one line per person per day with something on it."""
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(['Name', 'Pay', 'Date', 'Clock in', 'Clock out', 'Lunch (min)', 'Hours worked', 'Vacation/sick hours', 'Holiday hours', 'Paid hours', 'Note'])
    for rep in reports:
        p = rep.profile
        for r in rep.rows:
            punch = r['punch']
            if not (punch or r['off_hours'] or r['holiday_hours'] or r['kind'] in ('other',)):
                continue
            paid = round(Decimal(r['worked_min']) / 60 + r['off_hours'] + r['holiday_hours'], 2)
            note = {'vacation': 'Vacation', 'sick': 'Sick', 'holiday': r['holiday'] or ''}.get(r['kind'], '')
            if punch and punch.remote:
                note = (note + ' Remote: ' + punch.remote_reason).strip()
            if punch and punch.changes.all():
                note = (note + ' Corrected by a manager').strip()
            w.writerow([rep.profile.name, p.get_pay_type_display(), r['date'].isoformat(),
                        timezone.localtime(punch.clock_in).strftime('%H:%M') if punch else '',
                        timezone.localtime(punch.clock_out).strftime('%H:%M') if punch and punch.clock_out else '',
                        punch.lunch_minutes if punch and punch.lunch_minutes is not None else '',
                        f'{Decimal(r["worked_min"]) / 60:.2f}' if punch else '', r['off_hours'] or '', r['holiday_hours'] or '',
                        f'{paid:.2f}' if p.is_hourly else '', note])
        w.writerow([p.name, p.get_pay_type_display(), 'TOTAL', '', '', '', f'{rep.worked_hours:.2f}', f'{rep.off_hours:.2f}', f'{rep.holiday_hours:.2f}',
                    f'{rep.paid:.2f}' if p.is_hourly else 'salary', ''])
    return out.getvalue()


# ------------------------------------------------------------------ vacation balance

def accrual_start(profile):
    """When vacation starts building: the policy start, or the hire date if later. None until a hire date is entered."""
    if not profile.hire_date:
        return None
    return max(policy.POLICY_START, profile.hire_date)


def _earned_salaried(profile, start, today):
    total = Decimal(0)
    d = start
    while d <= today:
        year = policy.year_of_employment(profile.hire_date, d)
        seg_end = min(today, policy.anniversary(profile.hire_date, year) - timedelta(days=1))
        total += Decimal((seg_end - d).days + 1) * policy.entitlement_hours(year) / 364
        d = seg_end + timedelta(days=1)
    return total


def _earned_hourly(profile, start, today):
    by_year = defaultdict(int)
    for p in Punch.objects.filter(profile=profile, day__range=(start, today), clock_out__isnull=False):
        by_year[policy.year_of_employment(profile.hire_date, p.day)] += p.minutes or 0
    total = Decimal(0)
    for year, minutes in by_year.items():
        ent = policy.entitlement_hours(year)
        total += min(Decimal(ent), Decimal(minutes) / 60 * ent / policy.YEAR_HOURS)
    return total


def pto(profile, today=None):
    """The vacation picture for one person: earned, taken, coming, balance (earned - taken, may be negative) and what is left after what is coming."""
    today = today or timezone.localdate()
    start = accrual_start(profile)
    earned = Decimal(0)
    year = None
    if start and start <= today:
        earned = _earned_salaried(profile, start, today) if not profile.is_hourly else _earned_hourly(profile, start, today)
    if profile.hire_date:
        year = policy.year_of_employment(profile.hire_date, max(today, profile.hire_date))
    taken = coming = pending = Decimal(0)
    for t in TimeOff.objects.filter(profile=profile, status__in=[Status.APPROVED, Status.PENDING]):
        if not t.takes_balance:
            continue
        for d in t.dates:
            if d < policy.POLICY_START:
                continue
            h = Decimal(policy.DAY_HOURS)
            if t.status == Status.PENDING:
                pending += h
            elif d <= today:
                taken += h
            else:
                coming += h
    balance = earned - taken
    out = {'earned': earned, 'taken': taken, 'coming': coming, 'pending': pending, 'balance': balance, 'available': balance - coming,
           'balance_days': balance / policy.DAY_HOURS, 'year': year, 'has_hire': bool(profile.hire_date), 'start': start,
           'negative': balance < 0, 'rate': '', 'step': None}
    if year:
        ent = policy.entitlement_hours(year)
        out['entitlement_days'] = policy.entitlement_days(year)
        out['rate'] = (f'{ent / 52:.2f} hours a week' if not profile.is_hourly else f'1 hour per {policy.YEAR_HOURS / ent:.1f} worked').replace('.0 ', ' ')
        out['step'] = policy.anniversary(profile.hire_date, year)
    return out


# ------------------------------------------------------------------ the day, the week, the calendar

def today_board(profiles, now=None):
    """Who is scheduled today and where they are: for the manager's home and everyone's calendar."""
    now = now or timezone.localtime()
    today = now.date()
    roster = Roster(profiles, today, today)
    punches = {p.profile_id: p for p in Punch.objects.filter(profile__in=profiles, day=today)}
    board = []
    for p in profiles:
        kind, obj = roster.status(p, today)
        punch = punches.get(p.id)
        row = {'profile': p, 'kind': kind, 'punch': punch, 'state': '', 'late': 0}
        if punch:
            row['state'] = 'in' if not punch.clock_out else 'done'
        elif kind == 'working':
            start = local_dt(today, p.day_start)
            row['state'] = 'not_in'
            if now > start:
                row['late'] = int((now - start).total_seconds() // 60)
        elif kind == 'vacation':
            row['state'] = 'vacation'
        elif kind == 'off':
            row['state'] = 'off'
        elif kind == 'closed':
            row['state'] = 'closed'
        else:
            row['state'] = 'free'
        board.append(row)
    return board


def week_strip(profile, today):
    """Monday to Sunday for one person: what each day is."""
    start = policy.week_start(today)
    end = start + timedelta(days=6)
    roster = Roster([profile], start, end)
    punches = {p.day: p for p in Punch.objects.filter(profile=profile, day__range=(start, end))}
    days = []
    for d in daterange(start, end):
        kind, obj = roster.status(profile, d)
        days.append({'date': d, 'kind': kind, 'today': d == today, 'punch': punches.get(d), 'holiday': policy.holiday_name(d)})
    return days


def month_grid(year, month, viewer_is_manager):
    """A month for the team calendar: weeks of days, each with who is working, on vacation, or off for the holiday."""
    first = date(year, month, 1)
    start = policy.week_start(first)
    last = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1))
    end = last + timedelta(days=6 - last.weekday())
    profiles = tracked_profiles()
    roster = Roster(profiles, start, end)
    today = timezone.localdate()
    weeks, week = [], []
    for d in daterange(start, end):
        chips = []
        holiday = policy.holiday_name(d)
        for p in profiles:
            kind, obj = roster.status(p, d)
            label = p.first_name
            if kind == 'working':
                standard = p.day_start.hour == 9 and p.day_start.minute == 0 and p.day_end.hour == 17 and p.day_end.minute == 0
                chips.append({'kind': 'working', 'text': label if standard else f'{label} {policy.clock_label(p.day_start)}–{policy.clock_label(p.day_end)}'})
            elif kind == 'vacation':
                chips.append({'kind': 'vacation', 'text': f'{label} · vacation'})
            elif kind == 'off' and viewer_is_manager:
                chips.append({'kind': 'vacation', 'text': f'{label} · {obj.get_kind_display().lower()}'})
            elif kind == 'pending_off' and viewer_is_manager:
                chips.append({'kind': 'pending', 'text': f'{label} · off?'})
            elif kind == 'pending_drop' and viewer_is_manager:
                chips.append({'kind': 'pending', 'text': f'{label} · out?'})
            elif kind == 'pending_add' and viewer_is_manager:
                chips.append({'kind': 'pending', 'text': f'{label} · in?'})
        week.append({'date': d, 'in_month': d.month == month, 'today': d == today, 'holiday': holiday, 'chips': chips})
        if len(week) == 7:
            weeks.append(week)
            week = []
    return weeks


def attendance(profile, today, days=30):
    """The last 30 days in numbers: scheduled, worked, late, remote, missed."""
    start = max(tracking_from(profile), today - timedelta(days=days))
    end = today - timedelta(days=1)
    if start > end:
        return None
    roster = Roster([profile], start, end)
    punches = {p.day: p for p in Punch.objects.filter(profile=profile, day__range=(start, end))}
    scheduled = worked = late = remote = missed = 0
    for d in daterange(start, end):
        kind, _ = roster.status(profile, d)
        punch = punches.get(d)
        if punch:
            worked += 1
            remote += punch.remote
            if punch.clock_in > local_dt(d, profile.day_start) + timedelta(minutes=policy.LATE_GRACE_MINUTES):
                late += 1
        if kind in ('working', 'pending_off', 'pending_drop'):
            scheduled += 1
            if not punch:
                missed += 1
        elif punch:
            scheduled += 1
    return {'scheduled': scheduled, 'worked': worked, 'late': late, 'remote': remote, 'missed': missed}


# ------------------------------------------------------------------ what is waiting for a manager

def missed_days(profile, today):
    """Scheduled working days with no punch and no time off asked for or approved: a manager says what they were."""
    start = tracking_from(profile)
    end = today - timedelta(days=1)
    if start > end:
        return []
    roster = Roster([profile], start, end)
    have = set(Punch.objects.filter(profile=profile, day__range=(start, end)).values_list('day', flat=True))
    return [d for d in daterange(start, end) if d not in have and roster.status(profile, d)[0] == 'working']


def holiday_items(profile, today):
    """Hourly holidays so far with no decision yet."""
    if not profile.is_hourly:
        return []
    start = tracking_from(profile)
    decided = set(HolidayPay.objects.filter(profile=profile, day__gte=start).values_list('day', flat=True))
    return [(d, name) for d, name in _holiday_dates(profile, start, today) if d not in decided]


def collect_items(today=None):
    """Everything waiting on a manager, oldest first within each kind. Each item is a dict: kind, profile, day, plus what its row needs."""
    today = today or timezone.localdate()
    profiles = tracked_profiles()
    items = []
    far = today + timedelta(days=400)
    roster = Roster(profiles, today - timedelta(days=400), far)
    for p in profiles:
        for r in PunchRequest.objects.filter(profile=p, status=Status.PENDING):
            punch = Punch.objects.filter(profile=p, day=r.day).first()
            items.append({'kind': 'fix', 'profile': p, 'day': r.day, 'obj': r, 'punch': punch})
        asked = set(PunchRequest.objects.filter(profile=p, status=Status.PENDING).values_list('day', flat=True))
        for punch in open_punches(p, today):
            if punch.day not in asked:
                items.append({'kind': 'noout', 'profile': p, 'day': punch.day, 'punch': punch})
        for d in missed_days(p, today):
            items.append({'kind': 'missed', 'profile': p, 'day': d})
        for d, name in holiday_items(p, today):
            before = week_context(p, d)
            items.append({'kind': 'holiday', 'profile': p, 'day': d, 'name': name, 'week_before': before, 'week_after': before + policy.DAY_HOURS})
    for t in TimeOff.objects.filter(status=Status.PENDING, profile__applies=True).select_related('profile__staff__user'):
        gaps = [d for d in t.dates if not covered_by_others(t.profile, d, roster)]
        s = pto(t.profile, today)
        items.append({'kind': 'off', 'profile': t.profile, 'day': t.start_date, 'obj': t, 'left': s['available'] - (t.hours if t.takes_balance else 0),
                      'gaps': gaps, 'balance': s['available']})
    for c in ScheduleChange.objects.filter(status=Status.PENDING, profile__applies=True).select_related('profile__staff__user'):
        gap = []
        if c.drop_date and not covered_by_others(c.profile, c.drop_date, roster):
            gap.append(c.drop_date)
        items.append({'kind': 'swap', 'profile': c.profile, 'day': c.drop_date or c.add_date, 'obj': c, 'gaps': gap})
    order = {'holiday': 0, 'noout': 1, 'fix': 1, 'missed': 2, 'off': 3, 'swap': 4}
    items.sort(key=lambda i: (order[i['kind']], i['day'], i['profile'].name))
    return items


def apply_decision(by, kind, profile, day, obj, action, data):
    """Carry out a manager's decision on one item. Raises ClockError with something readable when it cannot be done."""
    approve = action == 'ok'
    note = (data.get('note') or '').strip()[:300]
    now = timezone.now()
    if period_locked(day) and kind in ('holiday', 'noout', 'fix', 'missed'):
        raise ClockError('That pay period is marked as entered in payroll. Reopen it on the hours page first.')
    if kind == 'holiday':
        name = policy.holiday_name(day)
        if not name:
            raise ClockError('That is not an office holiday.')
        try:
            hours = Decimal(str(data.get('hours') or policy.DAY_HOURS))
        except Exception:
            raise ClockError('Hours has to be a number.')
        if not 0 <= hours <= 24:
            raise ClockError('Hours has to be between 0 and 24.')
        HolidayPay.objects.update_or_create(profile=profile, day=day, defaults={
            'name': name, 'status': Status.APPROVED if approve and hours > 0 else Status.DECLINED, 'hours': hours if approve else Decimal(0),
            'decided_by': by, 'decision_note': note})
        return f'{name} pay saved.'
    if kind in ('noout', 'fix'):
        if kind == 'fix':
            if not approve:
                obj.status, obj.decided_by, obj.decided_at, obj.decision_note = Status.DECLINED, by, now, note
                obj.save()
                return 'Declined.'
            in_t, out_t, lunch = obj.clock_in, obj.clock_out, obj.lunch_minutes
            reason = f'{profile.first_name} asked for this fix: {obj.note}'.strip(': ')
        else:
            if not approve:
                return 'Left as it is.'
            out_t = _parse_time(data.get('out'))
            in_t = None
            lunch = _parse_int(data.get('lunch'))
            reason = note or 'Forgot to clock out; fixed by a manager.'
            if not out_t or lunch is None:
                raise ClockError('Give the clock-out time and the lunch minutes.')
        save_punch(profile, day, in_t, out_t, lunch, by, reason)
        if kind == 'fix':
            obj.status, obj.decided_by, obj.decided_at, obj.decision_note = Status.APPROVED, by, now, note
            obj.save()
        return 'Punch fixed. The old times are kept in its history.'
    if kind == 'missed':
        cls = data.get('cls')
        if cls == 'worked':
            save_punch(profile, day, profile.day_start, profile.day_end, 60, by, note or 'Worked; entered by a manager.')
            return 'Entered as a normal day (9 to 5, one hour lunch). Fix the times from the hours page if they were different.'
        if cls not in (TimeOff.Kind.VACATION, TimeOff.Kind.SICK, TimeOff.Kind.OTHER):
            raise ClockError('Pick what the day was.')
        TimeOff.objects.create(profile=profile, kind=cls, days=[day.isoformat()], start_date=day, end_date=day, status=Status.APPROVED,
                               asked_by_employee=False, decided_by=by, decided_at=now, note='', decision_note=note)
        return 'Saved.'
    if kind == 'off':
        obj.status, obj.decided_by, obj.decided_at, obj.decision_note = (Status.APPROVED if approve else Status.DECLINED), by, now, note
        obj.save()
        return 'Approved.' if approve else 'Declined.'
    if kind == 'swap':
        obj.status, obj.decided_by, obj.decided_at, obj.decision_note = (Status.APPROVED if approve else Status.DECLINED), by, now, note
        obj.save()
        return 'Approved. The calendar is updated.' if approve else 'Declined.'
    raise ClockError('Unknown item.')


def _parse_time(text):
    text = (text or '').strip()
    for fmt in ('%H:%M', '%I:%M %p', '%I:%M%p', '%I %p'):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return None


def _parse_int(text):
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return None


parse_time = _parse_time
parse_int = _parse_int
