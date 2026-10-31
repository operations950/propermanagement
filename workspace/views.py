from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from core.models import Property, StaffProfile
from worksessions.models import Frequency

from . import menu, services


def new_face_only(view=None, *, admins_too=False):
    """The new screens are for people the new layout is switched on for. Anyone else who somehow reaches one is sent to the original dashboard.
    `admins_too` lets a company admin in even before they are switched on (how the first people get switched on from the screen)."""
    def deco(fn):
        @wraps(fn)
        @login_required
        def wrapper(request, *args, **kwargs):
            profile = getattr(request.user, 'staff_profile', None)
            allowed = bool(profile and (profile.new_face or (admins_too and profile.is_company_admin))) or request.user.is_superuser
            if not allowed:
                return redirect('dashboard')
            return fn(request, *args, **kwargs)
        return wrapper
    return deco(view) if view else deco


def _profile(request):
    return getattr(request.user, 'staff_profile', None)


def _is_boss(request):
    p = _profile(request)
    return bool(p and (p.is_company_admin or menu.role_for(p) in ('owner', 'manager'))) or request.user.is_superuser


def _calendar(request, horizon):
    """Your Google Calendar for the range on screen, built the way the owner dashboard builds it."""
    from tickets.views import _calendar_color, _format_calendar_events
    from core.google_calendar import get_upcoming_events, is_configured
    staff = _profile(request)
    if not staff:
        return {}
    token = getattr(staff, 'google_calendar_token', None)
    ctx = {'calendar_configured': is_configured(), 'calendar_token': token, 'calendar_days': [], 'available_calendars': [], 'enabled_calendar_ids': '',
           'current_timezone': getattr(staff, 'timezone', ''), 'timezone_choices': StaffProfile.Timezone.choices}
    if token:
        try:
            days_ahead = {'today': 0, 'week': 6, 'month': 13}[horizon]
            raw, available = get_upcoming_events(token, days_ahead=days_ahead)
            ctx['calendar_days'] = _format_calendar_events(raw, days_ahead=days_ahead)
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
    tab = 'rules' if request.GET.get('tab') == 'rules' else 'items'
    params = request.GET
    ctx = {'tab': tab, 'params': params, 'staff_list': StaffProfile.objects.select_related('user').order_by('user__first_name'),
           'properties': Property.objects.filter(is_active=True).order_by('name'), 'frequencies': Frequency.choices}
    ctx['groups'] = services.compliance_rules(params) if tab == 'rules' else services.compliance_items(params)
    return render(request, 'workspace/compliance.html', ctx)


@new_face_only
def projects(request):
    tab = 'templates' if request.GET.get('tab') == 'templates' else 'active'
    kind = 'checklist' if request.GET.get('kind') == 'checklist' else 'project'
    params = request.GET
    ctx = {'tab': tab, 'kind': kind, 'params': params, 'properties': Property.objects.filter(is_active=True).order_by('name')}
    if tab == 'active':
        _, ctx['rows'] = services.project_runs(params)
    else:
        _, ctx['templates'] = services.project_templates(params)
    return render(request, 'workspace/projects.html', ctx)


@new_face_only
def financials(request):
    return render(request, 'workspace/financials.html', {})


@new_face_only
def line(request, slug):
    if slug not in services.LINE_TYPES:
        raise Http404
    label, by_city = services.line_properties(slug)
    return render(request, 'workspace/line.html', {'slug': slug, 'label': label, 'by_city': by_city})


@new_face_only(admins_too=True)
def admin_home(request):
    from core.views import _is_admin
    if not _is_admin(request.user):
        return redirect('ws_home')
    return render(request, 'workspace/admin.html', {})


@new_face_only(admins_too=True)
def people(request):
    """Who sees the new layout, and which menu each person gets. Switching someone on here is the only thing that changes what they see."""
    from core.views import _is_admin
    if not _is_admin(request.user):
        return redirect('ws_home')
    if request.method == 'POST':
        changed = 0
        for sp in StaffProfile.objects.all():
            if f'row_{sp.pk}' not in request.POST:
                continue            # only the rows that were on the form: a person added since it loaded is left alone
            on = request.POST.get(f'face_{sp.pk}') == 'on'
            role = request.POST.get(f'role_{sp.pk}', '')
            role = role if role in StaffProfile.MenuRole.values else ''
            if sp.pk == _profile(request).pk:
                on = True            # you can't lock yourself out of the layout you are editing from
            if (sp.new_face, sp.menu_role) != (on, role):
                sp.new_face, sp.menu_role = on, role
                sp.save(update_fields=['new_face', 'menu_role'])
                changed += 1
        messages.success(request, f'Saved. {changed} person(s) changed.' if changed else 'Nothing changed.')
        return redirect('ws_people')
    return render(request, 'workspace/people.html', {'staff_list': StaffProfile.objects.select_related('user').order_by('user__first_name', 'user__username'),
                                                      'roles': StaffProfile.MenuRole.choices, 'me': _profile(request)})
