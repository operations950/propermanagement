"""What the new screens read. Everything here is a view over data the app already keeps (tickets, recurring sessions, process runs, on-site visits); nothing new is stored."""
from datetime import timedelta

from django.db.models import OuterRef, Q, Subquery
from django.urls import reverse
from django.utils import timezone
from django.utils.dateformat import format as format_date

from core.models import Property, StaffProfile
from onsite.models import Visit
from processes.models import ProcessRun, ProcessTemplate
from tickets.models import Ticket, TicketStatusNote
from worksessions.models import Frequency, Session, SessionTemplate

NOW_DAYS = 15                        # a ticket due further out than this is a "later" ticket
HORIZONS = {'today': 0, 'week': 6, 'month': 30}
HORIZON_LABELS = {'today': 'Today', 'week': 'This week', 'month': 'This month'}
KIND_ORDER = [('ticket', 'Tickets'), ('compliance', 'Compliance'), ('project', 'Projects'), ('visit', 'On-site visits')]
FREQ_ORDER = [Frequency.DAILY, Frequency.WEEKLY, Frequency.BIWEEKLY, Frequency.MONTHLY, Frequency.MONTHLY_WORKDAY, Frequency.QUARTERLY, Frequency.YEARLY]


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


def ticket_rows(staff, end, today, everyone):
    note = TicketStatusNote.objects.filter(ticket=OuterRef('pk')).order_by('-created_at')
    qs = (Ticket.objects.filter(status__in=open_ticket_statuses())
          .exclude(process_runs__status=ProcessRun.Status.ACTIVE)
          .filter(Q(due_date__isnull=True) | Q(due_date__date__lte=end))
          .select_related('property', 'assigned_staff__user')
          .annotate(latest_note=Subquery(note.values('body')[:1])))
    if not everyone:
        qs = qs.filter(assigned_staff=staff)
    rows = []
    for t in qs.order_by('due_date')[:300]:
        d = _local_date(t.due_date)
        rows.append({'kind': 'ticket', 'title': t.title, 'where': t.property.name if t.property_id else '', 'note': (t.latest_note or t.description or '')[:140],
                     'due': due_label(d, today) if d else 'now', 'late': bool(d and d < today), 'sort': d or today, 'who': _person(t.assigned_staff),
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
    runs = (ProcessRun.objects.filter(status=ProcessRun.Status.ACTIVE)
            .select_related('process_template', 'ticket__property', 'ticket__assigned_staff__user', 'property', 'contact')
            .prefetch_related('steps'))
    rows = []
    for run in runs:
        steps = list(run.steps.all())
        cur = next((s for s in steps if not s.is_complete), None)
        if not cur:
            continue
        who_id = cur.assignee_staff_id or (run.ticket.assigned_staff_id if run.ticket_id else None)
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
        rows.append({'kind': 'project', 'title': run.ticket.title if run.ticket_id else run.process_template.name,
                     'where': (run.ticket.property.name if run.ticket_id and run.ticket.property_id else run.property.name if run.property_id else run.contact.name if run.contact_id else ''),
                     'note': f'Step {done + 1} of {len(steps)}: {cur.label}', 'due': due_label(due, today) if due else '', 'late': bool(due and due < today),
                     'sort': due or today, 'who': _person(staff_by_id.get(who_id)), 'url': run_url(run)})
    return rows


def run_url(run):
    if run.ticket_id:
        return reverse('ticket_detail', args=[run.ticket_id])
    if run.property_id:
        return reverse('property_detail', args=[run.property_id])
    return reverse('contact_edit', args=[run.contact_id])


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


def compliance_items(params):
    today = timezone.localdate()
    qs = (Session.objects.filter(status=Session.Status.OPEN).select_related('template', 'owner__user').prefetch_related('lines__property'))
    q = (params.get('q') or '').strip()
    if q:
        qs = qs.filter(Q(template__name__icontains=q) | Q(period_label__icontains=q))
    if params.get('owner'):
        qs = qs.filter(owner_id=params['owner'])
    if params.get('frequency'):
        qs = qs.filter(template__frequency=params['frequency'])
    if params.get('property'):
        qs = qs.filter(lines__property_id=params['property']).distinct()
    if params.get('due') == 'overdue':
        qs = qs.filter(due_at__lt=today)
    elif params.get('due') == 'week':
        qs = qs.filter(due_at__lte=today + timedelta(days=6))
    groups = []
    for f in FREQ_ORDER:
        items = []
        for s in qs.filter(template__frequency=f).order_by('due_at'):
            done, total = s.progress()
            items.append({'title': s.template.name, 'period': s.period_label, 'owner': _person(s.owner), 'done': done, 'total': total,
                          'due': due_label(s.due_at, today) if s.due_at else '', 'late': bool(s.due_at and s.due_at < today), 'url': reverse('session_detail', args=[s.pk])})
        if items:
            groups.append({'label': Frequency(f).label, 'items': items})
    return groups


def compliance_rules(params):
    qs = SessionTemplate.objects.select_related('owner__user')
    if params.get('owner'):
        qs = qs.filter(owner_id=params['owner'])
    if params.get('frequency'):
        qs = qs.filter(frequency=params['frequency'])
    q = (params.get('q') or '').strip()
    if q:
        qs = qs.filter(name__icontains=q)
    groups = []
    for f in FREQ_ORDER:
        rules = [{'sentence': rule_sentence(t), 'next': t.next_open_date, 'active': t.is_active, 'url': reverse('session_template_detail', args=[t.pk])} for t in qs.filter(frequency=f).order_by('name')]
        if rules:
            groups.append({'label': Frequency(f).label, 'rules': rules})
    return groups


# ---- Projects: process runs and templates ----
def project_runs(params):
    kind = 'checklist' if params.get('kind') == 'checklist' else 'project'
    today = timezone.localdate()
    qs = (ProcessRun.objects.filter(status=ProcessRun.Status.ACTIVE, process_template__kind=kind)
          .select_related('process_template', 'ticket__property', 'ticket__assigned_staff__user', 'property', 'contact').prefetch_related('steps'))
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
        rows.append({'title': run.ticket.title if run.ticket_id else run.process_template.name, 'template': run.process_template.name,
                     'where': (run.ticket.property.name if run.ticket_id and run.ticket.property_id else run.property.name if run.property_id else run.contact.name if run.contact_id else ''),
                     'done': done, 'total': len(steps), 'pct': int(100 * done / len(steps)) if steps else 0, 'next': cur.label if cur else 'All steps done',
                     'due': due_label(due, today) if due else '', 'late': bool(due and due < today), 'from_ticket': run.ticket_id is not None, 'url': run_url(run)})
    return kind, rows


def project_templates(params):
    kind = 'checklist' if params.get('kind') == 'checklist' else 'project'
    out = []
    for t in ProcessTemplate.objects.filter(kind=kind).prefetch_related('steps', 'runs'):
        runs = list(t.runs.all())
        out.append({'t': t, 'steps': t.steps.count(), 'used': len(runs), 'active': sum(1 for r in runs if r.status == ProcessRun.Status.ACTIVE)})
    return kind, out


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
