"""What the new screens read. Everything here is a view over data the app already keeps (tickets, recurring sessions, process runs, on-site visits); nothing new is stored."""
from datetime import timedelta

from django.db.models import Count, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce
from django.urls import reverse
from django.utils import timezone
from django.utils.dateformat import format as format_date

from core.models import Property, StaffProfile
from onsite.models import Visit
from processes.models import ProcessRun, ProcessTemplate
from tickets.models import Ticket, TicketPush, TicketStatusNote
from worksessions.models import Frequency, Session, SessionTemplate

NOW_DAYS = 15                        # a ticket due further out than this is a "later" ticket
HORIZONS = {'today': 0, 'week': 6, 'month': 30}
HORIZON_LABELS = {'today': 'Today', 'week': 'This week', 'month': 'This month'}
KIND_ORDER = [('ticket', 'Tickets'), ('compliance', 'Compliance'), ('project', 'Projects'), ('visit', 'On-site visits')]
FREQ_ORDER = [Frequency.YEARLY, Frequency.QUARTERLY, Frequency.MONTHLY, Frequency.MONTHLY_WORKDAY, Frequency.BIWEEKLY, Frequency.WEEKLY, Frequency.WEEKDAYS, Frequency.DAILY, Frequency.ONCE]


def open_ticket_statuses():
    from tickets.views import OPEN_STATUSES
    return OPEN_STATUSES


def due_label(d, today):
    if not d:
        return ''
    if d < today:
        return f'{(today - d).days}d late'
    if d == today:
        return 'today'
    if d <= today + timedelta(days=6):
        return format_date(d, 'D')
    return format_date(d, 'M j')


def _person(staff):
    return (staff.user.get_full_name() or staff.user.username) if staff else 'Nobody yet'


def _local_date(dt):
    return timezone.localtime(dt).date() if dt else None


def promoted_ticket_ids():
    """Tickets that have become a running project. They leave the ticket lists (they live under Projects now); a ticket that only has a process attached stays where it is."""
    return ProcessRun.objects.filter(status=ProcessRun.Status.ACTIVE, process_template__kind=ProcessTemplate.Kind.PROJECT, ticket__isnull=False).values('ticket_id')


def ticket_rows(staff, end, today, everyone):
    note = TicketStatusNote.objects.filter(ticket=OuterRef('pk')).order_by('-created_at')
    qs = (Ticket.objects.filter(status__in=open_ticket_statuses())
          .exclude(pk__in=promoted_ticket_ids())
          .filter(Q(due_date__isnull=True) | Q(due_date__date__lte=end))
          .select_related('property', 'assigned_staff__user')
          .annotate(latest_note=Subquery(note.values('body')[:1]),
                    push_n=Coalesce(Subquery(TicketPush.objects.filter(ticket=OuterRef('pk')).order_by().values('ticket').annotate(c=Count('pk')).values('c')[:1]), 0)))
    if not everyone:
        qs = qs.filter(assigned_staff=staff)
    rows = []
    for t in qs.order_by('due_date')[:300]:
        d = _local_date(t.due_date)
        rows.append({'kind': 'ticket', 'title': t.title, 'where': t.property.name if t.property_id else '', 'note': (t.latest_note or t.description or '')[:140],
                     'due': due_label(d, today) if d else 'now', 'late': bool(d and d < today), 'sort': d or today, 'who': _person(t.assigned_staff), 'pushes': t.push_n,
                     'url': reverse('ticket_detail', args=[t.pk])})
    return rows


def session_rows(staff, end, today, everyone):
    qs = (Session.objects.filter(status=Session.Status.OPEN)
          .filter(Q(due_at__isnull=True, opens_at__lte=end) | Q(due_at__lte=end))
          .select_related('template', 'owner__user').prefetch_related('lines'))
    if not everyone:
        qs = qs.filter(owner=staff)
    rows = []
    for s in qs.order_by('due_at')[:300]:
        done, total = s.progress()
        rows.append({'kind': 'compliance', 'title': s.template.name, 'where': s.period_label or '', 'note': f'{done} of {total} done' if total else 'Nothing listed yet',
                     'due': due_label(s.due_at, today) if s.due_at else '', 'late': bool(s.due_at and s.due_at < today), 'sort': s.due_at or today,
                     'who': _person(s.owner), 'url': reverse('session_detail', args=[s.pk])})
    return rows


def project_rows(staff, end, today, everyone):
    staff_by_id = {sp.pk: sp for sp in StaffProfile.objects.select_related('user')}
    runs = (ProcessRun.objects.filter(status=ProcessRun.Status.ACTIVE, process_template__kind=ProcessTemplate.Kind.PROJECT)
            .select_related('process_template', 'ticket__property', 'ticket__assigned_staff__user', 'property', 'contact', 'session_line__session__template')
            .prefetch_related('steps'))
    rows = []
    for run in runs:
        steps = list(run.steps.all())
        cur = next((s for s in steps if not s.is_complete), None)
        if not cur:
            continue
        who_id = run.assigned_to_id or cur.assignee_staff_id or (run.ticket.assigned_staff_id if run.ticket_id else None)
        if not everyone and who_id != staff.pk:
            continue
        due = None
        if cur.deadline_days_after_start is not None:
            due = _local_date(run.created_at) + timedelta(days=cur.deadline_days_after_start)
        elif run.ticket_id and run.ticket.due_date:
            due = _local_date(run.ticket.due_date)
        if due and due > end:
            continue
        done = sum(1 for s in steps if s.is_complete)
        rows.append({'kind': 'project', 'title': run.ticket.title if run.ticket_id else (f'{run.session_line.session.template.name}' if run.session_line_id else run.process_template.name),
                     'where': (run.ticket.property.name if run.ticket_id and run.ticket.property_id else run.property.name if run.property_id else run.contact.name if run.contact_id else ''),
                     'note': f'Step {done + 1} of {len(steps)}: {cur.label}', 'due': due_label(due, today) if due else '', 'late': bool(due and due < today),
                     'sort': due or today, 'who': _person(staff_by_id.get(who_id)), 'url': run_url(run)})
    return rows


def run_url(run):
    """Every run (project or process) opens on its own screen, which links back to the ticket or compliance item it belongs to."""
    return reverse('ws_run_detail', args=[run.pk])


def visit_rows(staff, end, today, everyone):
    qs = (Visit.objects.filter(status__in=[Visit.Status.SCHEDULED, Visit.Status.IN_PROGRESS], scheduled_date__lte=end)
          .select_related('property', 'visit_type', 'assigned_staff__user', 'assigned_contact'))
    if not everyone:
        qs = qs.filter(assigned_staff=staff)
    rows = []
    for v in qs.order_by('scheduled_date')[:300]:
        d = v.scheduled_date
        who = _person(v.assigned_staff) if v.assigned_staff_id else (v.assigned_contact.name if v.assigned_contact_id else 'Nobody yet')
        rows.append({'kind': 'visit', 'title': v.visit_type.name, 'where': v.property.name, 'note': 'In progress' if v.status == Visit.Status.IN_PROGRESS else 'Scheduled',
                     'due': due_label(d, today), 'late': bool(d and d < today), 'sort': d or today, 'who': who, 'url': reverse('onsite_visit_detail', args=[v.pk])})
    return rows


def home(staff, horizon, everyone=False):
    """The Home list: {'groups': [{'person': name or '', 'kinds': [{'label', 'kind', 'rows'}]}], 'count': n}. `everyone` lists every person's work, grouped by person."""
    if horizon not in HORIZONS:
        horizon = 'today'
    today = timezone.localdate()
    end = today + timedelta(days=HORIZONS[horizon])
    rows = (ticket_rows(staff, end, today, everyone) + session_rows(staff, end, today, everyone)
            + project_rows(staff, end, today, everyone) + visit_rows(staff, end, today, everyone))
    rows.sort(key=lambda r: (not r['late'], r['sort']))

    def kinds(rs):
        return [{'label': label, 'kind': k, 'rows': [r for r in rs if r['kind'] == k]} for k, label in KIND_ORDER if any(r['kind'] == k for r in rs)]
    if everyone:
        people = sorted({r['who'] for r in rows}, key=lambda n: (n == 'Nobody yet', n.lower()))
        groups = [{'person': p, 'kinds': kinds([r for r in rows if r['who'] == p])} for p in people]
    else:
        groups = [{'person': '', 'kinds': kinds(rows)}] if rows else []
    return {'groups': groups, 'count': len(rows), 'horizon': horizon}


# ---- Compliance: the recurring sessions, shown as items and rules ----
def rule_sentence(t):
    freq = t.get_frequency_display().lower()
    if t.frequency == Frequency.MONTHLY_WORKDAY and t.workday_of_month:
        freq = f'monthly, on working day {t.workday_of_month}'
    due = 'due the same day' if not t.due_offset_days else f'due {t.due_offset_days} days after it opens'
    scope = 'one line for each matching property' if t.line_source == SessionTemplate.LineSource.QUERY else 'a fixed list'
    return f'{t.name}: {freq}, {due}, {scope}, owned by {_person(t.owner)}'


COMPLIANCE_PER_PAGE = 50
# Column key -> label, in the order the table draws them: the same five columns as the Tickets list (how often takes the place of priority).
COMPLIANCE_COLUMNS = [('how', 'How often'), ('property', 'Property'), ('item', 'Item'), ('due', 'Due'), ('assigned', 'Assigned to')]
COMPLIANCE_STATUS = [('open', 'Open items'), ('done', 'Done'), ('all', 'All')]
RULE_COLUMNS = [('how', 'How often'), ('where', 'Applies to'), ('item', 'Rule'), ('next', 'Next opens'), ('owner', 'Owner')]


def sort_columns(columns, sort_param):
    """The header row of a sortable table: each column's own click-to-sort value (toggles when it is already the sort), the same way the Tickets list builds it."""
    key, is_desc = (sort_param or '').lstrip('-'), (sort_param or '').startswith('-')
    out = []
    for k, label in columns:
        active = key == k
        out.append({'key': k, 'label': label, 'next_sort': f'-{k}' if (active and not is_desc) else k, 'is_active': active, 'is_desc': active and is_desc})
    return out


def _property_text(session):
    names = []
    for line in session.lines.all():
        if line.property_id and line.property.name not in names:
            names.append(line.property.name)
    if not names:
        return 'General'
    return names[0] if len(names) == 1 else f'{len(names)} properties'


def _due_filter(qs, field, params, today):
    """The Tickets list's due-date bubbles, applied to any date field."""
    from tickets.views import DUE_WINDOWS
    due = params.get('due')
    if due == 'overdue':
        return qs.filter(**{f'{field}__lt': today})
    if due == 'today':
        return qs.filter(**{field: today})
    if due == 'tomorrow':
        return qs.filter(**{field: today + timedelta(days=1)})
    if due in DUE_WINDOWS:
        return qs.filter(**{f'{field}__gte': today, f'{field}__lte': today + timedelta(days=DUE_WINDOWS[due])})
    if due == 'none':
        return qs.filter(**{f'{field}__isnull': True})
    if due == 'custom':
        from django.utils.dateparse import parse_date
        d = parse_date(params.get('due_on') or '')
        return qs.filter(**{field: d}) if d else qs
    return qs


def _freq_rank(prefix=''):
    from django.db.models import Case, IntegerField, When
    return Case(*[When(**{f'{prefix}frequency': f, 'then': i}) for i, f in enumerate(FREQ_ORDER)], default=len(FREQ_ORDER), output_field=IntegerField())


def compliance_items(params):
    """The open (or done) compliance items, filtered and sorted from the query string the same way the Tickets list is. Returns (queryset, the selected filters)."""
    from django.db.models import F
    from worksessions.models import SessionLine
    today = timezone.localdate()
    status = params.get('status') if params.get('status') in ('open', 'done', 'all') else 'open'
    qs = Session.objects.select_related('template', 'owner__user').prefetch_related('lines__property')
    if status != 'all':
        qs = qs.filter(status=Session.Status.OPEN if status == 'open' else Session.Status.SUBMITTED)
    freq = params.get('frequency') if params.get('frequency') in Frequency.values else ''
    if freq:
        qs = qs.filter(template__frequency=freq)
    owner = params.get('owner') or ''
    if owner == 'none':
        qs = qs.filter(owner__isnull=True)
    elif owner.isdigit():
        qs = qs.filter(owner_id=owner)
    prop = Property.objects.filter(pk=params['property']).first() if (params.get('property') or '').isdigit() else None
    if prop:
        qs = qs.filter(pk__in=SessionLine.objects.filter(property_id=prop.pk).values('session_id'))
    q = (params.get('q') or '').strip()
    if q:
        qs = qs.filter(Q(template__name__icontains=q) | Q(period_label__icontains=q) | Q(pk__in=SessionLine.objects.filter(property__name__icontains=q).values('session_id')))
    qs = _due_filter(qs, 'due_at', params, today)
    first_property = Subquery(SessionLine.objects.filter(session=OuterRef('pk')).order_by('display_order', 'pk').values('property__name')[:1])
    qs = qs.annotate(freq_rank=_freq_rank('template__'), first_property=first_property)
    sort = params.get('sort') or ''
    key, desc = sort.lstrip('-'), sort.startswith('-')
    fields = {'how': 'freq_rank', 'property': 'first_property', 'item': 'template__name', 'due': 'due_at', 'assigned': 'owner__user__first_name'}
    if key in fields:
        f = F(fields[key])
        qs = qs.order_by(f.desc(nulls_last=True) if desc else f.asc(nulls_last=True), 'template__name')
    else:
        qs = qs.order_by(F('due_at').asc(nulls_last=True), 'template__name')
    return qs, {'status': status, 'frequency': freq, 'owner': owner, 'property': prop, 'q': q, 'due': params.get('due') or '', 'due_on': params.get('due_on') or ''}


def compliance_row(s, today=None):
    today = today or timezone.localdate()
    done, total = s.progress()
    return {'s': s, 'how': s.template.get_frequency_display(), 'where': _property_text(s), 'title': s.template.name, 'period': s.period_label,
            'progress': f'{done} of {total} done' if total else '', 'due': s.due_at, 'late': bool(s.status == Session.Status.OPEN and s.due_at and s.due_at < today),
            'who': _person(s.owner) if s.owner_id else '', 'done': s.status == Session.Status.SUBMITTED, 'url': reverse('session_detail', args=[s.pk])}


def rule_blocks():
    """One block per frequency for the Rules landing screen: how many rules are on, how many are off, and the soonest next opening."""
    from django.db.models import Min
    rules = SessionTemplate.objects.values('frequency').annotate(on=Count('pk', filter=Q(is_active=True)), off=Count('pk', filter=Q(is_active=False)), nxt=Min('next_open_date', filter=Q(is_active=True)))
    by = {r['frequency']: r for r in rules}
    out = []
    for f in FREQ_ORDER:
        r = by.get(f, {})
        out.append({'value': f, 'label': Frequency(f).label, 'on': r.get('on', 0), 'off': r.get('off', 0), 'next': r.get('nxt')})
    return out


def rule_scope_text(t):
    if t.line_source == SessionTemplate.LineSource.QUERY:
        types = ', '.join(dict(Property.Type.choices).get(x, x) for x in (t.property_types or []))
        return f'All {types.lower()}' if types else 'Every matching property'
    n = len(t.static_lines.all())
    return f'{n} fixed line{"s" if n != 1 else ""}'


def compliance_rules(params):
    """The rules of one frequency, for the Rules list. Switched-off rules only show when asked for (state=off or all)."""
    from django.db.models import F
    freq = params.get('frequency') if params.get('frequency') in Frequency.values else ''
    state = params.get('state') if params.get('state') in ('on', 'off', 'all') else 'on'
    qs = SessionTemplate.objects.select_related('owner__user').prefetch_related('static_lines')
    if freq:
        qs = qs.filter(frequency=freq)
    if state != 'all':
        qs = qs.filter(is_active=(state == 'on'))
    owner = params.get('owner') or ''
    if owner == 'none':
        qs = qs.filter(owner__isnull=True)
    elif owner.isdigit():
        qs = qs.filter(owner_id=owner)
    q = (params.get('q') or '').strip()
    if q:
        qs = qs.filter(name__icontains=q)
    sort = params.get('sort') or ''
    key, desc = sort.lstrip('-'), sort.startswith('-')
    fields = {'how': 'frequency', 'where': 'property_types', 'item': 'name', 'next': 'next_open_date', 'owner': 'owner__user__first_name'}
    order = F(fields.get(key, 'name'))
    qs = qs.order_by(order.desc(nulls_last=True) if desc else order.asc(nulls_last=True), 'name')
    rows = [{'t': t, 'how': t.get_frequency_display(), 'where': rule_scope_text(t), 'who': _person(t.owner) if t.owner_id else '', 'next': t.next_open_date, 'active': t.is_active,
             'url': reverse('session_template_detail', args=[t.pk])} for t in qs]
    return rows, {'frequency': freq, 'state': state, 'owner': owner, 'q': q}


# ---- Projects: process runs and templates ----
def project_runs(params):
    """The running projects, and only projects: a ticket or compliance line that just has a process attached is not listed here."""
    kind = 'project'
    today = timezone.localdate()
    qs = (ProcessRun.objects.filter(status=ProcessRun.Status.ACTIVE, process_template__kind=kind)
          .select_related('process_template', 'ticket__property', 'ticket__assigned_staff__user', 'property', 'contact', 'assigned_to__user', 'session_line__session__template').prefetch_related('steps'))
    q = (params.get('q') or '').strip()
    if q:
        qs = qs.filter(Q(process_template__name__icontains=q) | Q(ticket__title__icontains=q) | Q(property__name__icontains=q) | Q(ticket__property__name__icontains=q))
    if params.get('property'):
        qs = qs.filter(Q(property_id=params['property']) | Q(ticket__property_id=params['property']))
    rows = []
    for run in qs:
        steps = list(run.steps.all())
        done = sum(1 for s in steps if s.is_complete)
        cur = next((s for s in steps if not s.is_complete), None)
        due = _local_date(run.ticket.due_date) if run.ticket_id and run.ticket.due_date else None
        rows.append({'origin': run.origin_text(), 'who': _person(run.assigned_to) if run.assigned_to_id else '',
                     'title': run.ticket.title if run.ticket_id else (run.session_line.session.template.name if run.session_line_id else run.process_template.name), 'template': run.process_template.name,
                     'where': (run.ticket.property.name if run.ticket_id and run.ticket.property_id else run.property.name if run.property_id else run.contact.name if run.contact_id else ''),
                     'done': done, 'total': len(steps), 'pct': int(100 * done / len(steps)) if steps else 0, 'next': cur.label if cur else 'All steps done',
                     'due': due_label(due, today) if due else '', 'late': bool(due and due < today), 'from_ticket': run.ticket_id is not None, 'url': run_url(run)})
    return kind, rows


def project_templates(params):
    """Both kinds of template, in two groups: (projects, processes)."""
    groups = {ProcessTemplate.Kind.PROJECT: [], ProcessTemplate.Kind.CHECKLIST: []}
    for t in ProcessTemplate.objects.prefetch_related('steps', 'runs'):
        runs = list(t.runs.all())
        groups.setdefault(t.kind, []).append({'t': t, 'steps': len(t.steps.all()), 'used': len(runs), 'active': sum(1 for r in runs if r.status == ProcessRun.Status.ACTIVE)})
    return groups[ProcessTemplate.Kind.PROJECT], groups[ProcessTemplate.Kind.CHECKLIST]


# ---- Lines of business ----
LINE_TYPES = {
    'associations': (Property.Type.ASSOCIATION, 'Associations'),
    'short-term-rentals': (Property.Type.SHORT_TERM_RENTAL, 'Short-term rentals'),
    'long-term-rentals': (Property.Type.LONG_TERM_RENTAL, 'Long-term rentals'),
    'commercial': (Property.Type.COMMERCIAL, 'Commercial'),
}


def line_properties(slug):
    ptype, label = LINE_TYPES[slug]
    props = Property.objects.filter(property_type=ptype, is_active=True).order_by('city', 'name')
    by_city = {}
    for p in props:
        by_city.setdefault(p.city or 'No city set', []).append(p)
    return label, sorted(by_city.items(), key=lambda kv: (kv[0] == 'No city set', kv[0].lower()))


# --- guest time-change requests, for the On-Site arrival prompt -------------------------------

def pending_requests():
    """Guest check-in / checkout time changes still waiting for a yes or no, for stays that have not ended."""
    from datetime import datetime, time

    from onsite.models import Booking, GuestRequest
    start = timezone.make_aware(datetime.combine(timezone.localdate(), time.min))
    return list(GuestRequest.objects.filter(status=GuestRequest.Status.PENDING, booking__status=Booking.Status.ACTIVE, booking__check_out__gte=start)
                .select_related('booking__property', 'booking__unit').order_by('booking__check_out', 'created_at'))


def request_card(r):
    """Everything the prompt shows for one request: what was asked, for which stay, and the board's own read of whether it is safe to say yes."""
    from onsite.models import GuestRequest
    from onsite.services import str_board

    b = r.booking
    is_out = r.kind in GuestRequest.CHECKOUT_KINDS
    normal = timezone.localtime(b.check_out if is_out else b.check_in)
    verdict, tone = '', 'info'
    try:
        for item in str_board.build_board(normal.date(), include_tomorrow=False)['items']:
            if item['request'] is not None and item['request'].pk == r.pk:
                tone = item['tone']
                verdict = next((e['text'] for e in item['row']['requests'] if e['request'].pk == r.pk), '')
                break
    except Exception:
        pass            # the prompt still works without the board's read
    return {
        'id': r.pk, 'kind': r.get_kind_display(), 'where': str_board._label(b.property, b.unit), 'guest': b.guest_name or 'Guest',
        'day': f'{normal:%a %b} {normal.day}', 'asked': str_board._fmt(str_board._at(normal.date(), r.requested_time)),
        'normal': str_board._fmt(normal), 'what': 'checkout' if is_out else 'check-in', 'note': r.note, 'verdict': verdict, 'tone': tone,
    }


def request_state(session, want_prompt):
    """(how many requests are waiting, the next one to ask about or None). A request the person skipped this session is not asked about again, but still counts."""
    pending = pending_requests()
    skipped = set(session.get('ws_skipped_requests', []))
    card = None
    if want_prompt:
        nxt = next((r for r in pending if r.pk not in skipped), None)
        card = request_card(nxt) if nxt else None
    return len(pending), card


# ---- A compliance item, type by type ----
def item_context(session, view=''):
    """What the new-layout item screen draws for one compliance item: its rule, its lines (with the process or project each one carries), and the grid for a process spread over several properties."""
    from worksessions.models import SessionLine
    from processes.models import ProcessTemplate

    rule = session.template
    kind = rule.work_type
    lines = list(session.lines.select_related('property').prefetch_related('process_runs__steps', 'process_runs__process_template'))
    rows = []
    for line in lines:
        runs = [r for r in line.process_runs.all() if r.status != ProcessRun.Status.CANCELLED]
        run = runs[0] if runs else None
        prog = run.progress() if run else None
        rows.append({'line': line, 'run': run, 'done': prog[0] if prog else 0, 'total': prog[1] if prog else 0, 'pct': int(100 * prog[0] / prog[1]) if prog and prog[1] else 0,
                     'run_url': reverse('ws_run_detail', args=[run.pk]) if run else '', 'who': _person(run.assigned_to) if run and run.assigned_to_id else '',
                     'is_done': line.state == SessionLine.State.DONE, 'in_project': line.state == SessionLine.State.IN_PROJECT,
                     'closed': line.state in (SessionLine.State.SKIPPED, SessionLine.State.NOT_APPLICABLE)})
    single = len(rows) == 1 and rows[0]['line'].property_id is None
    grid = None
    view = view if view in ('list', 'grid') else ('grid' if kind == 'process' and len(rows) > 1 else 'list')
    if kind == 'process' and rows and view == 'grid':
        first = next((r['run'] for r in rows if r['run']), None)
        cols = [(s.step_key, s.label) for s in first.steps.all()] if first else []
        grid = {'cols': cols, 'rows': []}
        for r in rows:
            by_key = {s.step_key: s for s in r['run'].steps.all()} if r['run'] else {}
            cells = []
            for key, _label in cols:
                s = by_key.get(key)
                state = 'none' if s is None else ('na' if s.is_complete and (s.response or {}).get('not_applicable') else ('done' if s.is_complete else 'pending'))
                cells.append({'step': s, 'state': state})
            grid['rows'].append({**r, 'cells': cells})
    done, total = session.progress()
    return {'rule': rule, 'kind': kind, 'rows': rows, 'single': single, 'view': view, 'grid': grid, 'done': done, 'total': total, 'pct': int(100 * done / total) if total else 0,
            'staff_choices': StaffProfile.objects.filter(user__is_active=True).select_related('user').order_by('user__first_name'),
            'project_template': rule.process_template if kind == 'project' else None}
