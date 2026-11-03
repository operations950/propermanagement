"""Idempotent, runs on every deploy (Procfile) like the seed commands. Reshapes the compliance rules the owner agreed to:

- "Daily Admin Checklist" becomes two process rules, "Daily Admin" (weekdays, 6 tasks) and "Daily Short-Term Rental Admin"
  (every day, 7 tasks), both general (no property) and owned by Alexis.
- "Weekly Property Visits" is split into "Weekly Association Visits" (a line per association, new ones join automatically)
  and "Weekly Owner Mail Check" (the two owner mailboxes).
- The yearly Annual Meeting and Budget Meeting rules become project rules (a "Start project" button per association).
- The unused "Short-Term Rental Cleaning" process template is deleted.

Every step looks for what it needs first and does nothing when it is already done or the rule is not there, so it is safe to
run again and again, and never touches a rule it cannot recognize."""
from django.core.management.base import BaseCommand
from django.db.models import ProtectedError, Q
from django.utils import timezone

from core.models import StaffProfile
from processes.models import ProcessTemplate, ProcessTemplateStep, StepType
from worksessions.models import Frequency, SessionTemplate, SessionTemplateLine
from worksessions.services.generation import next_weekday

DAILY_ADMIN = [
    '50 Handwritten Letters',
    'Open and organize all mail from previous day',
    'Record all incoming association dues payments carefully',
    'Put all checks into the proper bank folder',
    'Read and organize admin inbox; respond; set up tickets',
    'Return any unanswered texts or calls for both phone lines',
]
DAILY_STR = [
    'Update cleaning calendar',
    'Change pricing as needed',
    'Answer all messages',
    'Review past guests',
    'PATRICK - Answer all messages',
    'PATRICK - Change pricing as needed',
    'Communicate with cleaner; early check out, same day, etc.',
]
OWNER_MAIL_LINES = ['324 NE 3rd', '325 NE 3rd']


def _alexis():
    return StaffProfile.objects.filter(
        Q(user__first_name__icontains='alexis') | Q(user__username__icontains='alexis') | Q(user__email__icontains='alexis'),
    ).select_related('user').first()


def _checklist(name, steps, description):
    template, created = ProcessTemplate.objects.get_or_create(
        name=name, defaults={'category': 'Daily', 'kind': ProcessTemplate.Kind.CHECKLIST, 'description': description},
    )
    if created:
        for order, label in enumerate(steps, start=1):
            ProcessTemplateStep.objects.create(process_template=template, label=label, step_type=StepType.CHECKBOX, sequence_order=order)
    return template


class Command(BaseCommand):
    help = 'Reshapes the compliance rules (daily admin split, visits split, project rules). Safe to run repeatedly.'

    def handle(self, *args, **options):
        self.today = timezone.localdate()
        self._daily()
        self._visits()
        self._yearly_projects()
        self._drop_cleaning_template()
        self.stdout.write(self.style.SUCCESS('Compliance rules reshaped.'))

    # ------------------------------------------------------------------ daily admin
    def _daily(self):
        old = SessionTemplate.objects.filter(name__icontains='daily admin checklist').first()
        if old is None:
            self.stdout.write('Daily Admin Checklist: not found (already reshaped, or never existed) — skipped.')
            return
        alexis = _alexis() or old.owner
        if not _alexis():
            self.stdout.write(self.style.WARNING('No staff profile for Alexis was found: the daily rules stay with the old rule\'s owner.'))
        admin_t = _checklist('Daily Admin', DAILY_ADMIN, 'The weekday office checklist.')
        str_t = _checklist('Daily Short-Term Rental Admin', DAILY_STR, 'The every-day short-term rental checklist.')
        specs = [
            ('Daily Admin', admin_t, Frequency.WEEKDAYS, self.today if self.today.weekday() < 5 else next_weekday(self.today)),
            ('Daily Short-Term Rental Admin', str_t, Frequency.DAILY, self.today),
        ]
        for name, template, frequency, opens in specs:
            SessionTemplate.objects.get_or_create(
                name=name,
                defaults=dict(
                    owner=alexis, department=old.department, frequency=frequency, next_open_date=opens,
                    line_source=SessionTemplate.LineSource.GENERAL, work_type=SessionTemplate.WorkType.PROCESS, process_template=template,
                ),
            )
        try:
            old.delete()
            self.stdout.write('Daily Admin Checklist replaced by Daily Admin and Daily Short-Term Rental Admin.')
        except ProtectedError:
            old.is_active = False
            old.save(update_fields=['is_active'])
            self.stdout.write(self.style.WARNING('Daily Admin Checklist still has items, so it was switched off instead of deleted.'))

    # ------------------------------------------------------------------ weekly visits split
    def _visits(self):
        visits = SessionTemplate.objects.filter(name__icontains='weekly property visits').first()
        if visits is None:
            self.stdout.write('Weekly Property Visits: not found — skipped.')
            return
        if not SessionTemplate.objects.filter(name='Weekly Owner Mail Check').exists():
            found = [line.label for line in visits.static_lines.all() if 'ne 3rd' in line.label.lower()]
            mail = SessionTemplate.objects.create(
                name='Weekly Owner Mail Check', owner=visits.owner, department=visits.department, frequency=visits.frequency,
                workday_of_month=visits.workday_of_month, next_open_date=visits.next_open_date, due_offset_days=visits.due_offset_days,
                line_source=SessionTemplate.LineSource.STATIC, work_type=SessionTemplate.WorkType.REMINDER,
            )
            for order, label in enumerate(found or OWNER_MAIL_LINES):
                SessionTemplateLine.objects.create(template=mail, label=label, display_order=order)
        visits.name = 'Weekly Association Visits'
        visits.line_source = SessionTemplate.LineSource.QUERY
        visits.property_types = ['association']
        visits.save(update_fields=['name', 'line_source', 'property_types'])
        visits.static_lines.all().delete()
        self.stdout.write('Weekly Property Visits is now Weekly Association Visits, with Weekly Owner Mail Check split off.')

    # ------------------------------------------------------------------ yearly meetings become projects
    def _yearly_projects(self):
        for rule_text, template_text in (('annual meeting', 'annual meeting'), ('budget meeting', 'budget meeting')):
            rule = SessionTemplate.objects.filter(name__icontains='yearly').filter(name__icontains=rule_text).first()
            if rule is None or rule.work_type == SessionTemplate.WorkType.PROJECT:
                continue
            template = ProcessTemplate.objects.filter(
                name__icontains=template_text, kind=ProcessTemplate.Kind.PROJECT, is_active=True,
            ).order_by('id').first()
            if template is None:
                self.stdout.write(self.style.WARNING(f'No project template found for "{rule.name}" — left as it is.'))
                continue
            rule.work_type = SessionTemplate.WorkType.PROJECT
            rule.process_template = template
            rule.save(update_fields=['work_type', 'process_template'])
            self.stdout.write(f'"{rule.name}" is now a project rule using "{template.name}".')

    # ------------------------------------------------------------------ the unused cleaning template
    def _drop_cleaning_template(self):
        for template in ProcessTemplate.objects.filter(name='Short-Term Rental Cleaning'):
            try:
                template.delete()
                self.stdout.write('Deleted the unused "Short-Term Rental Cleaning" process template.')
            except ProtectedError:
                template.is_active = False
                template.save(update_fields=['is_active'])
                self.stdout.write(self.style.WARNING('"Short-Term Rental Cleaning" has runs, so it was switched off instead of deleted.'))
