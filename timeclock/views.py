from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.auth_backends import _client_ip
from core.models import StaffProfile
from core.views import _is_admin

from . import policy, policy_text, services
from .models import (HolidayPay, OfficeConnection, PayPeriodLock, PolicySignature, Punch, PunchRequest, ScheduleChange, Status, TimeOff,
                     TimeProfile)
from .services import ClockError

admin_only = user_passes_test(_is_admin)


def employee_required(view):
    """The page is for staff the time clock applies to; anyone else goes back to the dashboard."""
    @login_required
    def wrapped(request, *args, **kwargs):
        profile = services.profile_for(request.user)
        if not profile:
            messages.info(request, 'The time clock is not set up for your account.')
            return redirect('dashboard')
        return view(request, profile, *args, **kwargs)
    wrapped.__name__ = view.__name__
    return wrapped


def _date(text):
    try:
        return date.fromisoformat((text or '').strip())
    except ValueError:
        return None


# ------------------------------------------------------------------ the employee's pages

@employee_required
def home(request, profile):
    now = timezone.localtime()
    today = now.date()
    punch = services.today_punch(profile, today)
    roster = services.Roster([profile], today, today)
    kind, _ = roster.status(profile, today)
    start, end = policy.pay_period(today)
    report = services.PeriodReport(profile, start, today)
    office_ip = _client_ip(request)
    mine = {
        'off': TimeOff.objects.filter(profile=profile, status=Status.PENDING),
        'change': ScheduleChange.objects.filter(profile=profile, status=Status.PENDING),
        'fix': PunchRequest.objects.filter(profile=profile, status=Status.PENDING),
    }
    decided = [
        *TimeOff.objects.filter(profile=profile, status__in=[Status.APPROVED, Status.DECLINED], decided_at__gte=now - timedelta(days=14), asked_by_employee=True),
        *ScheduleChange.objects.filter(profile=profile, status__in=[Status.APPROVED, Status.DECLINED], decided_at__gte=now - timedelta(days=14)),
    ]
    team = services.today_board(services.tracked_profiles(), now)
    return render(request, 'timeclock/home.html', {
        'profile': profile, 'now': now, 'today': today, 'punch': punch, 'kind': kind,
        'remote': not services.at_office(office_ip), 'office_set': services.office_configured(),
        'open_before': services.open_punches(profile, today), 'pto': services.pto(profile, today),
        'week': services.week_strip(profile, today), 'report': report, 'period_start': start, 'period_end': end,
        'mine': mine, 'decided': decided, 'attendance': services.attendance(profile, today),
        'holidays': policy.next_holidays(today, 3), 'team': team, 'lunch_choices': (0, 15, 30, 45, 60, 75, 90),
        'needs_signature': needs_signature(profile),
    })


@employee_required
@require_POST
def clock_in(request, profile):
    try:
        services.clock_in(profile, _client_ip(request), request.POST.get('reason', ''))
        messages.success(request, 'Clocked in.')
    except ClockError as e:
        messages.error(request, str(e))
    return redirect(request.POST.get('next') or 'timeclock_home')


@employee_required
@require_POST
def clock_out(request, profile):
    try:
        punch = services.clock_out(profile, _client_ip(request), request.POST.get('lunch'), request.POST.get('reason', ''))
        messages.success(request, f'Clocked out. You worked {policy.minutes_label(punch.minutes)} today.')
    except ClockError as e:
        messages.error(request, str(e))
    return redirect(request.POST.get('next') or 'timeclock_home')


@employee_required
def request_time_off(request, profile):
    today = timezone.localdate()
    form = {'start': request.POST.get('start', ''), 'end': request.POST.get('end', ''), 'note': request.POST.get('note', '')}
    if request.method == 'POST':
        start, end = _date(form['start']), _date(form['end']) or _date(form['start'])
        error = None
        if not start:
            error = 'Pick the first day.'
        elif start < today:
            error = 'Time off has to start today or later. For a day that has passed, a manager records it.'
        elif end < start:
            error = 'The last day cannot be before the first.'
        elif (end - start).days > 60:
            error = 'That is more than 60 days. Ask for a shorter stretch.'
        if not error:
            days = services.working_days_between(profile, start, end)
            taken = {d for t in TimeOff.objects.filter(profile=profile, status__in=[Status.APPROVED, Status.PENDING], end_date__gte=start, start_date__lte=end) for d in t.dates}
            if not days:
                error = 'None of those days are working days on your schedule.'
            elif taken & set(days):
                error = 'You already asked for one of those days.'
        if error:
            messages.error(request, error)
        else:
            TimeOff.objects.create(profile=profile, kind=TimeOff.Kind.VACATION, days=[d.isoformat() for d in days], start_date=days[0], end_date=days[-1],
                                   note=form['note'].strip()[:300])
            messages.success(request, f'Asked for {len(days)} day{"s" if len(days) != 1 else ""} off. A manager will decide.')
            return redirect('timeclock_home')
    return render(request, 'timeclock/request.html', {'profile': profile, 'kind': 'off', 'form': form, 'pto': services.pto(profile, today), 'today': today})


@employee_required
def request_change(request, profile):
    today = timezone.localdate()
    form = {'drop': request.POST.get('drop', ''), 'add': request.POST.get('add', ''), 'note': request.POST.get('note', '')}
    if request.method == 'POST':
        drop, add = _date(form['drop']), _date(form['add'])
        horizon = today + timedelta(days=120)
        roster = services.Roster([profile], today, horizon)
        error = None
        if not drop and not add:
            error = 'Pick the day you will not work, the day you will work, or both.'
        elif drop and (drop < today or not roster.scheduled(profile, drop) or policy.holiday_name(drop)):
            error = f'You are not scheduled to work on {policy.short_day(drop)}, so there is nothing to move.'
        elif add and (add < today or roster.scheduled(profile, add) or policy.holiday_name(add)):
            error = f'You already work {policy.short_day(add)}, or the office is closed that day.'
        elif add and add > horizon:
            error = 'That day is too far ahead.'
        if error:
            messages.error(request, error)
        else:
            ScheduleChange.objects.create(profile=profile, drop_date=drop, add_date=add, note=form['note'].strip()[:300])
            messages.success(request, 'Asked. A manager will decide, and the calendar changes once approved.')
            return redirect('timeclock_home')
    return render(request, 'timeclock/request.html', {'profile': profile, 'kind': 'change', 'form': form, 'today': today})


@employee_required
def request_fix(request, profile):
    today = timezone.localdate()
    day = _date(request.POST.get('day') or request.GET.get('day')) or today - timedelta(days=1)
    form = {'day': day.isoformat(), 'in': request.POST.get('in', ''), 'out': request.POST.get('out', ''), 'lunch': request.POST.get('lunch', ''), 'note': request.POST.get('note', '')}
    if request.method == 'POST':
        t_in, t_out, lunch = services.parse_time(form['in']), services.parse_time(form['out']), services.parse_int(form['lunch'])
        punch = Punch.objects.filter(profile=profile, day=day).first()
        error = None
        if day >= today or day < today - timedelta(days=45):
            error = 'Pick a day from the last 45 days (not today).'
        elif not (t_in or t_out):
            error = 'Give the time that is wrong or missing.'
        elif not punch and not (t_in and t_out):
            error = 'That day has no clock-in. Give both the time you started and the time you finished.'
        elif (t_out or (punch and punch.clock_out is None)) and lunch is None and not (punch and punch.lunch_minutes is not None):
            error = 'Say how long the lunch was (0 for none).'
        elif services.period_locked(day):
            error = 'That pay period has already gone to payroll. Ask a manager.'
        if error:
            messages.error(request, error)
        else:
            PunchRequest.objects.create(profile=profile, day=day, clock_in=t_in, clock_out=t_out, lunch_minutes=lunch, note=form['note'].strip()[:300])
            messages.success(request, 'Sent. A manager will check it and fix the time.')
            return redirect('timeclock_home')
    return render(request, 'timeclock/request.html', {'profile': profile, 'kind': 'fix', 'form': form, 'today': today, 'day': day,
                                                      'days': [today - timedelta(days=i) for i in range(1, 15)]})


@employee_required
@require_POST
def cancel_request(request, profile, kind, pk):
    model = {'off': TimeOff, 'change': ScheduleChange, 'fix': PunchRequest}.get(kind)
    if not model:
        raise Http404
    obj = get_object_or_404(model, pk=pk, profile=profile, status=Status.PENDING)
    obj.delete()
    messages.success(request, 'Request withdrawn.')
    return redirect('timeclock_home')


# ------------------------------------------------------------------ the policy

def needs_signature(profile):
    return not PolicySignature.objects.filter(profile=profile, version=policy.POLICY_VERSION).exists()


@login_required
def policy_page(request):
    profile = services.profile_for(request.user)
    if profile:
        pay_type, days_text = profile.pay_type, policy.days_label(profile.days)
    elif _is_admin(request.user):
        pay_type = request.GET.get('as') if request.GET.get('as') in ('salaried', 'hourly') else 'salaried'
        days_text = 'Monday to Friday' if pay_type == 'salaried' else 'Monday, Tuesday, Thursday, Friday and Saturday'
    else:
        return redirect('dashboard')
    doc = policy_text.for_employee(pay_type, days_text)
    signature = None
    if profile:
        if request.method == 'POST' and needs_signature(profile):
            name = ' '.join((request.POST.get('typed_name') or '').split())
            if not request.POST.get('agree'):
                messages.error(request, 'Tick the box to say you have read and agree.')
            elif len(name.split(' ')) < 2:
                messages.error(request, 'Type your full name (first and last) to sign.')
            else:
                PolicySignature.objects.create(profile=profile, version=policy.POLICY_VERSION, typed_name=name[:120], ip=_client_ip(request),
                                               text=render_to_string('timeclock/_policy_body.html', {'doc': doc}, request=request))
                messages.success(request, 'Signed. You can download a copy any time from this page.')
                return redirect('timeclock_home')
        signature = PolicySignature.objects.filter(profile=profile, version=policy.POLICY_VERSION).first()
    return render(request, 'timeclock/policy.html', {'doc': doc, 'profile': profile, 'signature': signature, 'preview': not profile})


@employee_required
def policy_download(request, profile):
    sig = PolicySignature.objects.filter(profile=profile).first()
    if not sig:
        raise Http404
    html = render_to_string('timeclock/policy_copy.html', {'sig': sig, 'profile': profile, 'title': policy_text.TITLE})
    response = HttpResponse(html, content_type='text/html; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="time-off-policy-v{sig.version}-{profile.name.replace(" ", "-")}.html"'
    return response


# ------------------------------------------------------------------ the team calendar (everyone the clock covers, and managers)

@login_required
def team_calendar(request):
    manager = _is_admin(request.user)
    profile = services.profile_for(request.user)
    if not (manager or profile):
        return redirect('dashboard')
    today = timezone.localdate()
    try:
        year, month = (int(x) for x in (request.GET.get('month') or f'{today.year}-{today.month}').split('-'))
        date(year, month, 1)
    except ValueError:
        year, month = today.year, today.month
    prev_m = date(year, month, 1) - timedelta(days=1)
    next_m = date(year, month, 28) + timedelta(days=5)
    return render(request, 'timeclock/calendar.html', {
        'weeks': services.month_grid(year, month, manager), 'first': date(year, month, 1), 'manager': manager,
        'prev': f'{prev_m.year}-{prev_m.month}', 'next': f'{next_m.year}-{next_m.month}', 'is_current': (year, month) == (today.year, today.month),
        'people': services.tracked_profiles(), 'holidays': policy.next_holidays(today, 3),
    })


# ------------------------------------------------------------------ the manager's pages

@login_required
@admin_only
def manage_home(request):
    now = timezone.localtime()
    profiles = services.tracked_profiles()
    items = services.collect_items(now.date())
    # the pay period that has just ended is the one being entered in payroll; before that it is the current one
    last_start = policy.pay_period(now.date())[0] - timedelta(days=14)
    last_blockers = sum(len(services.PeriodReport(p, last_start, now.date()).blockers) for p in profiles)
    return render(request, 'timeclock/manage.html', {
        'board': services.today_board(profiles, now), 'items': items, 'now': now, 'profiles': profiles,
        'last_start': last_start, 'last_end': last_start + timedelta(days=13), 'last_locked': PayPeriodLock.objects.filter(start=last_start).exists(),
        'last_blockers': last_blockers, 'office_set': services.office_configured(),
    })


@login_required
@admin_only
@require_POST
def manage_decide(request):
    kind = request.POST.get('kind')
    profile = get_object_or_404(TimeProfile, pk=request.POST.get('pid'), applies=True)
    day = _date(request.POST.get('day'))
    obj = None
    if kind in ('fix', 'off', 'swap'):
        model = {'fix': PunchRequest, 'off': TimeOff, 'swap': ScheduleChange}[kind]
        obj = get_object_or_404(model, pk=request.POST.get('obj'), profile=profile, status=Status.PENDING)
        day = {'fix': lambda: obj.day, 'off': lambda: obj.start_date, 'swap': lambda: obj.drop_date or obj.add_date}[kind]()
    if not day or kind not in ('fix', 'off', 'swap', 'noout', 'missed', 'holiday'):
        raise Http404
    try:
        messages.success(request, services.apply_decision(request.user, kind, profile, day, obj, request.POST.get('action'), request.POST))
    except ClockError as e:
        messages.error(request, str(e))
    return redirect(request.POST.get('next') or 'timeclock_manage')


@login_required
@admin_only
def manage_hours(request):
    today = timezone.localdate()
    start = _date(request.GET.get('start')) or policy.pay_period(today)[0] - timedelta(days=14)
    start = policy.pay_period(start)[0]
    profiles = services.tracked_profiles()
    reports = [services.PeriodReport(p, start, today) for p in profiles]
    if request.GET.get('csv'):
        response = HttpResponse(services.hours_csv(reports), content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="hours-{start:%Y-%m-%d}.csv"'
        return response
    blockers = sum(len(r.blockers) for r in reports)
    return render(request, 'timeclock/hours.html', {
        'reports': reports, 'start': start, 'end': start + timedelta(days=13), 'prev': start - timedelta(days=14), 'next': start + timedelta(days=14),
        'is_future': start > today, 'blockers': blockers, 'locked': any(r.locked for r in reports) or PayPeriodLock.objects.filter(start=start).exists(),
        'in_progress': start + timedelta(days=13) >= today,
    })


@login_required
@admin_only
@require_POST
def manage_lock(request):
    start = _date(request.POST.get('start'))
    if not start or policy.pay_period(start)[0] != start:
        raise Http404
    if request.POST.get('action') == 'unlock':
        PayPeriodLock.objects.filter(start=start).delete()
        messages.success(request, 'Reopened. Its punches can be changed again.')
    else:
        today = timezone.localdate()
        if start + timedelta(days=13) >= today:
            messages.error(request, 'That pay period is not over yet.')
        elif sum(len(services.PeriodReport(p, start, today).blockers) for p in services.tracked_profiles()):
            messages.error(request, 'Settle everything listed for the period first.')
        else:
            PayPeriodLock.objects.get_or_create(start=start, defaults={'locked_by': request.user})
            messages.success(request, 'Marked as entered. Its punches are now locked.')
    return redirect(f"{reverse('timeclock_hours')}?start={start.isoformat()}")


@login_required
@admin_only
def manage_punch(request):
    profile = get_object_or_404(TimeProfile, pk=request.GET.get('pid') or request.POST.get('pid'), applies=True)
    day = _date(request.GET.get('day') or request.POST.get('day')) or timezone.localdate()
    punch = Punch.objects.filter(profile=profile, day=day).prefetch_related('changes').first()
    back = f"{reverse('timeclock_hours')}?start={policy.pay_period(day)[0].isoformat()}"
    if request.method == 'POST':
        try:
            if services.period_locked(day):
                raise ClockError('That pay period is marked as entered in payroll. Reopen it on the hours page first.')
            reason = (request.POST.get('reason') or '').strip()
            if not reason:
                raise ClockError('Write a short reason; it is kept with the punch.')
            t_in, t_out = services.parse_time(request.POST.get('in')), services.parse_time(request.POST.get('out'))
            lunch = services.parse_int(request.POST.get('lunch'))
            if not punch and not t_in:
                raise ClockError('A new day needs a clock-in time.')
            services.save_punch(profile, day, t_in, t_out, lunch, request.user, reason)
            messages.success(request, 'Saved. The old times are kept in the punch history.')
            return redirect(back)
        except ClockError as e:
            messages.error(request, str(e))
    return render(request, 'timeclock/punch_edit.html', {'profile': profile, 'day': day, 'punch': punch, 'back': back, 'post': request.POST})


@login_required
@admin_only
def manage_setup(request):
    rows = []
    for sp in StaffProfile.objects.select_related('user').order_by('user__first_name', 'user__last_name'):
        try:
            tp = sp.time_profile
        except TimeProfile.DoesNotExist:
            tp = None
        sig = PolicySignature.objects.filter(profile=tp, version=policy.POLICY_VERSION).first() if tp else None
        rows.append({'staff': sp, 'tp': tp, 'days': tp.days if tp else [0, 1, 2, 3, 4], 'sig': sig, 'pto': services.pto(tp) if tp and tp.applies else None})
    return render(request, 'timeclock/setup.html', {
        'rows': rows, 'weekdays': list(enumerate(policy.WEEKDAYS)), 'offices': OfficeConnection.objects.all(), 'my_ip': _client_ip(request),
        'office_here': OfficeConnection.objects.filter(ip=_client_ip(request)).exists(), 'policy_version': policy.POLICY_VERSION,
    })


@login_required
@admin_only
@require_POST
def manage_setup_save(request, pk):
    staff = get_object_or_404(StaffProfile, pk=pk)
    tp, _ = TimeProfile.objects.get_or_create(staff=staff)
    was_on = tp.applies
    tp.applies = bool(request.POST.get('applies'))
    if tp.applies and not was_on:
        tp.tracking_start = timezone.localdate()
    pay = request.POST.get('pay_type')
    if pay in TimeProfile.PayType.values:
        tp.pay_type = pay
    tp.hire_date = _date(request.POST.get('hire_date'))
    days = sorted({int(x) for x in request.POST.getlist('days') if x.isdigit() and 0 <= int(x) <= 6})
    tp.work_days = ','.join(str(d) for d in days)
    start, end = services.parse_time(request.POST.get('day_start')), services.parse_time(request.POST.get('day_end'))
    if start and end and end > start:
        tp.day_start, tp.day_end = start, end
    tp.save()
    messages.success(request, f'Saved {tp.name}.' + ('' if tp.hire_date or not tp.applies else ' Add a hire date so vacation starts building.'))
    return redirect('timeclock_setup')


@login_required
@admin_only
@require_POST
def office_connection(request):
    ip = _client_ip(request)
    if request.POST.get('action') == 'remove':
        OfficeConnection.objects.filter(pk=request.POST.get('pk')).delete()
        messages.success(request, 'Removed.')
    else:
        OfficeConnection.objects.get_or_create(ip=ip, defaults={'added_by': request.user})
        messages.success(request, 'This connection now counts as the office.')
    return redirect('timeclock_setup')
