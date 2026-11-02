"""READ-ONLY inventory of the work-model pieces in the live system: process templates and runs, the old recurring system (ticket templates, task packages, functions, tickets they made, and which of those are still open), compliance rules, visit rules, portfolio rules, and the accounting schedules.

It changes nothing. Run it and read the output:  manage.py inspect_work_model
No owner names, emails or phone numbers are printed."""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db.models import Count

DONE = ['completed', 'verified', 'cancelled', 'skipped', 'not_applicable']


def sect(out, title):
    out.write('\n' + '=' * 8 + ' ' + title + ' ' + '=' * 8)


def t(s, n=70):
    return ' '.join(str(s or '').split())[:n]


class Command(BaseCommand):
    help = 'Read-only inventory of processes, the old recurring system, compliance rules, visit rules and accounting schedules.'

    def handle(self, *args, **options):
        out = self.stdout
        out.write('INVENTORY OF THE WORK MODEL (read only)')
        for label, fn in (('processes', self.processes), ('old recurring', self.old_recurring), ('open old tickets', self.open_old_tickets), ('packages', self.packages),
                          ('compliance', self.compliance), ('on-site', self.onsite), ('portfolio', self.portfolio), ('accounting', self.accounting)):
            try:
                fn(out)
            except Exception as exc:       # a table or field missing in this version is reported, never fatal
                out.write(f'  [{label}: could not read: {type(exc).__name__}: {str(exc)[:120]}]')
        out.write('\nEND OF INVENTORY')

    def processes(self, out):
        from processes.models import ProcessRun, ProcessTemplate
        sect(out, 'PROCESS TEMPLATES')
        for tpl in ProcessTemplate.objects.prefetch_related('steps', 'runs', 'attachments').order_by('kind', 'name'):
            steps = list(tpl.steps.all())
            types = Counter(s.step_type for s in steps)
            runs = Counter(r.status for r in tpl.runs.all())
            out.write(f'  #{tpl.pk} [{tpl.kind}] {t(tpl.name, 60)} | active={tpl.is_active} | cat={t(tpl.category, 24)} | steps={len(steps)} | types={dict(types)} | runs={dict(runs)} | docs={tpl.attachments.count()}')
            for s in steps:
                out.write(f'       {s.sequence_order:>2}. [{s.step_type}] {t(s.label, 80)}{" (required)" if s.is_required else ""}')
        sect(out, 'PROCESS RUNS')
        out.write('  by status: %s' % dict(Counter(ProcessRun.objects.values_list('status', flat=True))))
        out.write('  attached to: tickets=%d properties=%d contacts=%d compliance lines=%d' % (
            ProcessRun.objects.filter(ticket__isnull=False).count(), ProcessRun.objects.filter(property__isnull=False, ticket__isnull=True, session_line__isnull=True).count(),
            ProcessRun.objects.filter(contact__isnull=False).count(), ProcessRun.objects.filter(session_line__isnull=False).count()))

    def old_recurring(self, out):
        from tickets.models import Ticket, TicketTemplate
        sect(out, 'OLD RECURRING TICKET RULES (TicketTemplate)')
        qs = TicketTemplate.objects.order_by('-is_active', 'title')
        out.write(f'  total: {qs.count()} | active: {qs.filter(is_active=True).count()}')
        for tpl in qs:
            made = Ticket.objects.filter(created_from_template=tpl)
            open_n = made.exclude(status__in=DONE).count()
            out.write(f'  #{tpl.pk} {t(tpl.title, 60)} | {tpl.frequency} | active={tpl.is_active} | dept={tpl.default_assigned_role} | target={tpl.target_type} | docs={tpl.documents.count()} | tickets made={made.count()} open={open_n}')
        sect(out, 'TICKETS BY SOURCE AND STATUS')
        out.write('  by source: %s' % dict(Counter(Ticket.objects.values_list('source', flat=True))))
        for src in ('recurring', 'session'):
            rows = Ticket.objects.filter(source=src)
            out.write(f'  source={src}: total {rows.count()}, by status {dict(Counter(rows.values_list("status", flat=True)))}')
        out.write('  tickets with created_from_template set: %d' % Ticket.objects.filter(created_from_template__isnull=False).count())

    def open_old_tickets(self, out):
        from tickets.models import Ticket
        sect(out, 'OPEN TICKETS THAT CAME FROM THE OLD RECURRING RULES')
        qs = (Ticket.objects.filter(source='recurring').exclude(status__in=DONE).select_related('property', 'created_from_template', 'assigned_staff__user').order_by('due_date'))
        out.write(f'  open recurring-source tickets: {qs.count()}')
        for tk in qs[:120]:
            who = tk.assigned_staff.user.first_name if tk.assigned_staff_id else '-'
            out.write(f'    #{tk.pk} [{tk.status}] {t(tk.title, 56)} | {t(tk.property, 30)} | due {tk.due_date:%Y-%m-%d} | {who}' if tk.due_date else
                      f'    #{tk.pk} [{tk.status}] {t(tk.title, 56)} | {t(tk.property, 30)} | no due date | {who}')
        started = Ticket.objects.filter(source='recurring').exclude(status__in=['open', 'upcoming']).exclude(status__in=DONE).count()
        out.write(f'  of those, already worked on (not just open or upcoming): {started}')

    def packages(self, out):
        from tickets import models as tm
        sect(out, 'TASK PACKAGES, TASK GROUPS, FUNCTIONS')
        for name in ('TaskPackageTemplate', 'TaskPackage', 'PackageRun', 'TaskGroup', 'Function', 'TemplateOccurrence', 'PropertyTemplateOverride'):
            model = getattr(tm, name, None)
            if model is not None:
                out.write(f'  {name}: {model.objects.count()}')

    def compliance(self, out):
        from worksessions.models import Session, SessionTemplate
        sect(out, 'COMPLIANCE RULES (SessionTemplate)')
        qs = SessionTemplate.objects.select_related('owner__user').order_by('-is_active', 'frequency', 'name')
        out.write(f'  total: {qs.count()} | active: {qs.filter(is_active=True).count()}')
        for r in qs:
            sessions = r.sessions.all()
            out.write(f'  #{r.pk} {t(r.name, 56)} | {r.frequency} | active={r.is_active} | scope={r.line_source} types={r.property_types} | type={r.work_type} tpl={r.process_template_id} | static lines={r.static_lines.count()} | items total={sessions.count()} open={sessions.filter(status="open").count()} | owner={r.owner.user.first_name if r.owner_id else "-"}')
        out.write('  items by status: %s' % dict(Counter(Session.objects.values_list('status', flat=True))))

    def onsite(self, out):
        from onsite.models import Visit, VisitRule, VisitType
        sect(out, 'ON-SITE: VISIT TYPES AND VISIT RULES')
        for v in VisitType.objects.all():
            out.write(f'  visit type #{v.pk} {t(v.name, 40)} | slug={v.slug} | active={v.is_active} | visits={Visit.objects.filter(visit_type=v).count()}')
        out.write('  visit rules: %d' % VisitRule.objects.count())

    def portfolio(self, out):
        from portfolio.models import BizRecurringRule
        sect(out, 'PORTFOLIO RECURRING RULES')
        out.write('  total: %d' % BizRecurringRule.objects.count())

    def accounting(self, out):
        from accounting.models import ChargeRule, RecurringBill
        sect(out, 'ACCOUNTING SCHEDULES (counts per association)')
        for label, model in (('recurring bills', RecurringBill), ('owner charge rules', ChargeRule)):
            rows = model.objects.values('association__name').annotate(n=Count('pk')).order_by('association__name')
            out.write(f'  {label}: {({r["association__name"]: r["n"] for r in rows}) or "none"}')
