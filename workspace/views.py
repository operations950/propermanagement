from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_time
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from core import nav
from core.models import Property, StaffProfile
from core.people import active_staff
from worksessions.models import Frequency

from . import menu, services, week as week_service


def new_face_only(view=None, *, admins_too=False):
    """The new screens are for people the new layout is switched on for. Anyone else who somehow reaches one is sent to the original dashboard.
    `admins_too` lets a company admin in even before they are switched on (how the first people get switched on from the screen)."""
    def deco(fn):
        @wraps(fn)
        @login_required
        def wrapper(request, *args, **kwargs):
            profile = getattr(request.user, 'staff_profile', None)
            allowed = bool(profile) or request.user.is_superuser           # everyone on the team is on the new layout
            if not allowed:
                return redirect('dashboard')
            return fn(request, *args, **kwargs)
        return wrapper
    return deco(view) if view else deco


def _profile(request):
    return getattr(request.user, 'staff_profile', None)


def _is_boss(request):
    p = _profile(request)
    return bool(p and (p.is_company_admin or p.can_financial)) or request.user.is_superuser


PHONE_DAYS_AHEAD = 13          # the phone's calendar agenda: today and the next two weeks


def _calendar(request, horizon):
    """Your Google Calendar for the range on screen, built the way the owner dashboard builds it."""
    from tickets.views import _calendar_color, _format_calendar_events
    from core.google_calendar import get_upcoming_events, is_configured
    staff = _profile(request)
    if not staff:
        return {}
    token = getattr(staff, 'google_calendar_token', None)
    ctx = {'calendar_configured': is_configured(), 'calendar_token': token, 'calendar_days': [], 'calendar_days_all': [], 'available_calendars': [], 'enabled_calendar_ids': '',
           'current_timezone': getattr(staff, 'timezone', ''), 'timezone_choices': StaffProfile.Timezone.choices}
    if token:
        try:
            days_ahead = {'today': 0, 'week': 6, 'month': 13}[horizon]
            # One read of Google covers the phone's two-week agenda too (calendar_days_all); the desktop column still gets exactly the days the screen's range asks for.
            raw, available = get_upcoming_events(token, days_ahead=max(days_ahead, PHONE_DAYS_AHEAD))
            ctx['calendar_days_all'] = _format_calendar_events(raw, days_ahead=max(days_ahead, PHONE_DAYS_AHEAD))
            ctx['calendar_days'] = ctx['calendar_days_all'][:days_ahead + 1]
            primary = next((c['id'] for c in available if c['is_primary']), 'primary')
            for c in available:
                c['color'] = _calendar_color(c['id'])
            ctx['available_calendars'] = available
            ctx['enabled_calendar_ids'] = ','.join(token.enabled_calendar_ids or [primary])
        except Exception:
            ctx['calendar_days'] = []
    return ctx


@new_face_only
def home(request):
    horizon = request.GET.get('h') if request.GET.get('h') in services.HORIZONS else 'today'
    can_everyone = _is_boss(request)
    everyone = can_everyone and request.GET.get('scope') == 'all'
    staff = _profile(request)
    data = services.home(staff, horizon, everyone=everyone) if staff else {'groups': [], 'count': 0, 'horizon': horizon}
    today = timezone.localdate()
    heading = {'today': today.strftime('%A, %B ') + str(today.day), 'week': 'The next 7 days', 'month': 'The next 30 days'}[horizon]
    ctx = {'data': data, 'horizon': horizon, 'horizons': list(services.HORIZON_LABELS.items()), 'heading': heading, 'everyone': everyone, 'can_everyone': can_everyone}
    ctx.update(_calendar(request, horizon))
    return render(request, 'workspace/home.html', ctx)


@new_face_only
def compliance(request):
    """Compliance: the items the rules have opened (the Tickets screen in green), and the rules themselves (big blocks by frequency, then one frequency's rules)."""
    from django.core.paginator import Paginator
    from django.http import JsonResponse
    from django.template.loader import render_to_string
    from core.models import properties_by_type
    from tickets.views import DUE_OPTIONS

    tab = 'rules' if request.GET.get('tab') == 'rules' else 'items'
    params = request.GET
    staff_list = active_staff(also=[params.get('owner')])
    freq_labels = dict(Frequency.choices)
    ctx = {'tab': tab, 'staff_list': staff_list, 'frequencies': Frequency.choices, 'status_options': services.COMPLIANCE_STATUS,
           'due_options': DUE_OPTIONS, 'properties_by_type': properties_by_type()}
    if tab == 'items':
        qs, sel = services.compliance_items(params)
        page = Paginator(qs, services.COMPLIANCE_PER_PAGE).get_page(params.get('page'))
        today = timezone.localdate()
        sel.update(frequency_label=freq_labels.get(sel['frequency'], ''), due_label=dict(DUE_OPTIONS).get(sel['due'], 'Custom date' if sel['due'] == 'custom' else ''),
                   owner_name=next((str(s) for s in staff_list if str(s.pk) == sel['owner']), ''))
        ctx.update(sel=sel, page_obj=page, rows=[services.compliance_row(s, today) for s in page], sort_columns=services.sort_columns(services.COMPLIANCE_COLUMNS, params.get('sort')))
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({'desktop': render_to_string('workspace/_compliance_rows.html', ctx, request), 'mobile': render_to_string('workspace/_compliance_cards.html', ctx, request),
                                 'pager': render_to_string('core/_pager.html', ctx, request)})
    else:
        rows, sel = services.compliance_rules(params)
        sel['frequency_label'] = freq_labels.get(sel['frequency'], '')
        ctx.update(sel=sel, rows=rows, blocks=services.rule_blocks(), sort_columns=services.sort_columns(services.RULE_COLUMNS, params.get('sort')))
    return render(request, 'workspace/compliance.html', ctx)


@new_face_only
def projects(request):
    tab = 'templates' if request.GET.get('tab') == 'templates' else 'active'
    params = request.GET
    ctx = {'tab': tab, 'params': params}
    if tab == 'active':
        from core.models import properties_by_type
        from tickets.views import DUE_OPTIONS
        staff_list = active_staff(also=[params.get('owner')])
        sel, ctx['rows'] = services.project_runs(params)
        sel.update(due_label=dict(DUE_OPTIONS).get(sel['due'], 'Custom date' if sel['due'] == 'custom' else ''),
                   owner_name=next((str(s) for s in staff_list if str(s.pk) == sel['owner']), ''))
        ctx.update(sel=sel, staff_list=staff_list, due_options=DUE_OPTIONS, properties_by_type=properties_by_type())
    else:
        ctx['project_templates'], ctx['process_templates'] = services.project_templates(params)
    return render(request, 'workspace/projects.html', ctx)


@new_face_only
def financials(request):
    from core.permissions import has_financial
    if not has_financial(request.user):
        return redirect('ws_home')
    return render(request, 'workspace/financials.html', {})


@new_face_only
def line(request, slug):
    if slug not in services.LINE_TYPES:
        raise Http404
    from core.permissions import has_financial
    label, by_city = services.line_properties(slug)
    # The money screens show only to people whose switch allows them (the screens check the same switch: a hidden card is also a closed door).
    return render(request, 'workspace/line.html', {'slug': slug, 'label': label, 'by_city': by_city, 'can_financial': has_financial(request.user)})


@new_face_only(admins_too=True)
def admin_home(request):
    from core.views import _is_admin
    if not _is_admin(request.user):
        return redirect('ws_home')
    return render(request, 'workspace/admin.html', {})


@new_face_only(admins_too=True)
def people(request):
    """People and roles: add or remove a person and choose what each one can use. Four switches per person (Financial, Time clock management, Admin; Portfolio is yours alone);
    everything else is open to the whole team. Removing a person turns their login off and keeps their history."""
    from django.contrib.auth import get_user_model

    from core.forms import StaffCreateForm
    from core.models import Contact
    from core.views import _is_admin
    if not _is_admin(request.user):
        return redirect('ws_home')
    me = _profile(request)
    form = StaffCreateForm()
    if request.method == 'POST':
        action = request.POST.get('action', 'save')
        if action == 'add':
            form = StaffCreateForm(request.POST)
            if form.is_valid():
                User = get_user_model()
                email = form.cleaned_data['email']
                with transaction.atomic():
                    user = User.objects.create_user(username=email, email=email, first_name=form.cleaned_data['first_name'], last_name=form.cleaned_data['last_name'], password=form.cleaned_data['password'])
                    StaffProfile.objects.create(user=user, role=form.cleaned_data['role'], phone=form.cleaned_data['phone'], timezone=form.cleaned_data['timezone'])
                    match = Contact.objects.filter(email__iexact=email).first()
                    if match:
                        match.contact_type, match.secondary_types = Contact.ContactType.STAFF_ADJACENT, []
                        match.save(update_fields=['contact_type', 'secondary_types'])
                    else:
                        Contact.objects.create(name=user.get_full_name() or email, contact_type=Contact.ContactType.STAFF_ADJACENT, phone=form.cleaned_data['phone'], email=email)
                messages.success(request, f'{user.get_full_name() or email} added. Tick what they can use.')
                return redirect('ws_people')
            messages.error(request, 'That person could not be added: ' + '; '.join(' '.join(e) for e in form.errors.values()))
        elif action in ('remove', 'restore'):
            sp = get_object_or_404(StaffProfile, pk=request.POST.get('person'))
            if sp.pk == me.pk and action == 'remove':
                messages.error(request, 'You cannot remove yourself.')
            else:
                sp.user.is_active = action == 'restore'
                sp.user.save(update_fields=['is_active'])
                messages.success(request, f'{sp} is {"back" if action == "restore" else "removed: their login is off, and their history is kept"}.')
            return redirect('ws_people')
        else:
            changed = 0
            for sp in StaffProfile.objects.all():
                if f'row_{sp.pk}' not in request.POST:
                    continue            # only the rows that were on the form: a person added since it loaded is left alone
                want = {'can_financial': request.POST.get(f'financial_{sp.pk}') == 'on', 'can_time_admin': request.POST.get(f'time_{sp.pk}') == 'on',
                        'is_company_admin': request.POST.get(f'admin_{sp.pk}') == 'on'}
                if sp.pk == me.pk:
                    want['is_company_admin'] = True          # you cannot take your own Admin away from yourself here
                role = request.POST.get(f'dept_{sp.pk}', sp.role)
                if role in StaffProfile.Role.values or role == '':
                    want['role'] = role
                if any(getattr(sp, k) != v for k, v in want.items()):
                    for k, v in want.items():
                        setattr(sp, k, v)
                    sp.save(update_fields=list(want))
                    changed += 1
            messages.success(request, f'Saved. {changed} person(s) changed.' if changed else 'Nothing changed.')
            return redirect('ws_people')
    everyone = list(StaffProfile.objects.select_related('user').order_by('user__first_name', 'user__username'))
    people_list = [p for p in everyone if p.user.is_active]
    removed_list = [p for p in everyone if not p.user.is_active]
    return render(request, 'workspace/people.html', {'staff_list': people_list, 'removed_list': removed_list, 'departments': StaffProfile.Role.choices, 'me': me, 'form': form,
                                                      'timezones': StaffProfile.Timezone.choices})


@require_POST
@new_face_only
def request_decide(request):
    """Answer a guest time-change request from the On-Site prompt: accept, deny, or skip it for now (a skipped request is not asked about again this session)."""
    from onsite.models import GuestRequest
    from onsite.services import str_board

    gr = get_object_or_404(GuestRequest, pk=request.POST.get('request_id'))
    action = request.POST.get('action')
    if action == 'skip':
        request.session['ws_skipped_requests'] = sorted(set(request.session.get('ws_skipped_requests', [])) | {gr.pk})
    elif action in ('accept', 'deny'):
        if gr.status != GuestRequest.Status.PENDING:
            messages.info(request, 'Someone already answered that one.')
        else:
            str_board.decide_request(gr, 'approve' if action == 'accept' else 'decline', request.user)
            messages.success(request, f'{gr.get_kind_display()} {"accepted" if action == "accept" else "declined"}.')
    target = request.POST.get('next', '')
    if not url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        target = reverse('onsite_dashboard')
    return redirect(target)


@new_face_only
def week(request):
    """On-Site > Properties: every short-term rental by city, seven days across, with the check-out and check-in times you can change right there."""
    return render(request, 'workspace/week.html', {'w': week_service.week(parse_date(request.GET.get('start', '') or ''))})


@require_POST
@new_face_only
def week_time(request):
    """Change one stay's check-out or check-in time (or put it back to normal). A change that helps the cleaning is simply agreed and the cleaner told; one that takes time away waits for a yes (see str_board.record_request)."""
    from onsite.models import Booking, GuestRequest
    from onsite.services import str_board

    booking = get_object_or_404(Booking, pk=request.POST.get('booking_id'))
    kind = request.POST.get('kind')
    start = request.POST.get('start', '')
    if kind in ('checkout', 'checkin'):
        kinds = GuestRequest.CHECKOUT_KINDS if kind == 'checkout' else GuestRequest.CHECKIN_KINDS
        word = 'Check-out' if kind == 'checkout' else 'Check-in'
        try:
            with transaction.atomic():
                live = GuestRequest.objects.filter(booking=booking, kind__in=kinds).exclude(status=GuestRequest.Status.DECLINED)
                if request.POST.get('action') == 'remove':
                    live.delete()
                    messages.success(request, f'{word} time is back to normal.')
                else:
                    requested = parse_time((request.POST.get('time') or '').strip())
                    live.delete()           # changing a change: the old one is replaced, and put back if the new one is refused
                    made = str_board.record_request(booking, kind, requested, request.POST.get('note', ''), request.user)
                    messages.success(request, f'{word} time changed.' + (' It helps the cleaning, so it is agreed and the cleaner has been told.' if made.status == GuestRequest.Status.APPROVED
                                                                      else ' It waits for a yes or no (the ! on the board).'))
        except str_board.RequestError as e:
            messages.error(request, str(e))
    target = reverse('ws_week')
    return redirect(f'{target}?start={start}' if parse_date(start) else target)


@require_POST
@new_face_only
def week_clean(request):
    """From the round mark on a checkout: schedule the cleaning if there is none, and put a cleaner on it if one was picked. Handled the way the visit screen does it (the cleaner is sent their link)."""
    from core.models import Contact
    from onsite.models import Booking, Visit
    from onsite.services import notify as notify_service
    from onsite.services import reservations as reservation_service

    booking = get_object_or_404(Booking, pk=request.POST.get('booking_id'))
    start = request.POST.get('start', '')
    visit = booking.visits.exclude(status__in=[Visit.Status.CANCELLED, Visit.Status.SKIPPED]).order_by('-pk').first()
    if visit is None:
        reservation_service._visit_for(booking)
        visit = booking.visits.exclude(status__in=[Visit.Status.CANCELLED, Visit.Status.SKIPPED]).order_by('-pk').first()
        if visit is None:
            messages.error(request, 'A cleaning could not be scheduled: there is no turnover visit type set up for it.')
        else:
            messages.success(request, 'Cleaning scheduled.')
    kind, _, raw_id = request.POST.get('assignee', '').partition('-')
    if visit is not None and kind in ('staff', 'contact') and raw_id.isdigit():
        if kind == 'staff' and StaffProfile.objects.filter(pk=raw_id).exists():
            visit.assigned_staff_id, visit.assigned_contact = int(raw_id), None
        elif kind == 'contact' and Contact.objects.filter(pk=raw_id).exists():
            visit.assigned_contact_id, visit.assigned_staff = int(raw_id), None
        else:
            visit = None
        if visit is not None:
            if visit.status == Visit.Status.UNASSIGNED:
                visit.status = Visit.Status.SCHEDULED
            visit.full_clean()
            visit.save()
            link_note = ''
            if notify_service.dispatch_link(visit, request) == notify_service.SCHEDULED:
                link_note = f' The cleaner will be sent the link {notify_service.link_send_description(visit)}.'
            messages.success(request, f'Cleaning is with {visit.assignee_label()}.{link_note}')
    target = reverse('ws_week')
    return redirect(f'{target}?start={start}' if parse_date(start) else target)


def _int(raw):
    return int(raw) if str(raw or '').isdigit() else 0


@require_POST
@new_face_only
def make_project(request, pk):
    """Turn a ticket into a project: pick the project template and who it goes to. It leaves Tickets, lives under Projects, and links back here."""
    from processes.models import ProcessTemplate
    from processes.projects import start_project
    from tickets.models import Ticket

    ticket = get_object_or_404(Ticket, pk=pk)
    template = ProcessTemplate.objects.filter(pk=_int(request.POST.get('template'))).first()
    assignee = StaffProfile.objects.filter(pk=_int(request.POST.get('assignee'))).first()
    try:
        if template is None:
            raise ValueError('Choose a project template.')
        run = start_project(template, user=request.user, assigned_to=assignee, ticket=ticket)
    except ValueError as e:
        messages.error(request, str(e))
        return redirect('ticket_detail', pk=ticket.pk)
    messages.success(request, f'It is a project now{f", with {assignee}" if assignee else ""}. You will find it under Projects.')
    return redirect('ws_run_detail', pk=run.pk)


@new_face_only
def run_detail(request, pk):
    """One run (a project, or a process done for one property): its steps, its notes, and a way back to the ticket or compliance item it belongs to."""
    from processes.models import ProcessRun, ProcessTemplate

    run = get_object_or_404(ProcessRun.objects.select_related('process_template', 'ticket__property', 'assigned_to__user', 'session_line__session__template', 'session_line__property', 'property', 'contact')
                            .prefetch_related('steps__attachments', 'process_template__attachments', 'process_template__links'), pk=pk)
    is_project = run.process_template.kind == ProcessTemplate.Kind.PROJECT
    if run.ticket_id:
        origin_url, origin_label = reverse('ticket_detail', args=[run.ticket_id]), f'Ticket: {run.ticket.title}'
    elif run.session_line_id:
        origin_url, origin_label = reverse('session_detail', args=[run.session_line.session_id]), run.origin_text()
    else:
        origin_url, origin_label = '', run.origin_text()
    back = (reverse('ws_projects'), 'Projects') if is_project else ((origin_url, 'Compliance item') if run.session_line_id else (origin_url, 'Ticket'))
    # A project goes up to the wide screen it was opened from (Projects, Home); a process run belongs to the ticket or compliance item it was started for, so Up is that.
    up = nav.up(request, default=back) if is_project else nav.Up(*back)
    return render(request, 'workspace/run_detail.html', {'run': run, 'is_project': is_project, 'origin_url': origin_url, 'origin_label': origin_label, 'back': back, 'up': up,
                                                         'process_runs': [run]})


@require_POST
@new_face_only
def run_note(request, pk):
    from processes.models import ProcessRun
    run = get_object_or_404(ProcessRun, pk=pk)
    run.notes = (request.POST.get('notes') or '').strip()[:2000]
    run.save(update_fields=['notes'])
    messages.success(request, 'Note saved.')
    return redirect('ws_run_detail', pk=run.pk)


@require_POST
@new_face_only
def step_note(request, pk):
    """A note on one step of one run. Goes back to wherever the person was (the run screen, a ticket, the compliance grid)."""
    from processes.models import ProcessRunStep
    step = get_object_or_404(ProcessRunStep, pk=pk)
    step.note = (request.POST.get('note') or '').strip()[:500]
    step.save(update_fields=['note'])
    target = request.POST.get('next') or request.META.get('HTTP_REFERER', '')
    if not url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        target = reverse('ws_run_detail', args=[step.run_id])
    return redirect(target)
