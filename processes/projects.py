"""Starting and finishing projects and processes. A project is a process run of a project-kind template; it is what a ticket or a compliance line is promoted into when the work runs over days.

The ticket (or line) a project came from keeps a link to it; the project lives and is counted under Projects."""
from django.core.mail import send_mail
from django.utils import timezone

from .models import ProcessRun, ProcessRunStep, ProcessTemplate


def copy_steps(run, template):
    """Snapshot a template's steps onto a run (editing the template later never changes a run in progress)."""
    for step in template.steps.all():
        ProcessRunStep.objects.create(
            run=run, sequence_order=step.sequence_order, step_key=step.step_key, step_type=step.step_type,
            label=step.label, help_text=step.help_text, config=step.config, is_required=step.is_required,
            requires_upload=step.requires_upload, assignee_role=step.assignee_role,
            assignee_staff=step.assignee_staff, deadline_days_after_start=step.deadline_days_after_start,
        )
    return run


def start_run(template, *, user=None, assigned_to=None, ticket=None, session_line=None, property=None, contact=None):
    """A new run of `template` against exactly one target (a ticket, a compliance line, a property or a contact)."""
    kwargs = {'ticket': ticket} if ticket else {'session_line': session_line, 'property': property} if session_line else {'property': property} if property else {'contact': contact}
    run = ProcessRun.objects.create(process_template=template, created_by=user if getattr(user, 'pk', None) else None, assigned_to=assigned_to, **kwargs)
    return copy_steps(run, template)


def start_project(template, *, user=None, assigned_to=None, ticket=None, session_line=None):
    """Promote a ticket or a compliance line into a project. Raises ValueError for anything that is not an active project template, or when the ticket/line is already running one."""
    if template.kind != ProcessTemplate.Kind.PROJECT or not template.is_active:
        raise ValueError('Choose a project template.')
    if ticket is not None and ticket.process_runs.filter(process_template__kind=ProcessTemplate.Kind.PROJECT, status=ProcessRun.Status.ACTIVE).exists():
        raise ValueError('That ticket is already a project.')
    if session_line is not None and session_line.process_runs.filter(process_template__kind=ProcessTemplate.Kind.PROJECT).exclude(status=ProcessRun.Status.CANCELLED).exists():
        raise ValueError('A project was already started for that property.')
    run = start_run(template, user=user, assigned_to=assigned_to, ticket=ticket, session_line=session_line, property=session_line.property if session_line else None)
    if session_line is not None:
        from worksessions.models import SessionLine
        session_line.state = SessionLine.State.IN_PROJECT
        session_line.save(update_fields=['state'])
    notify_assignee(run)
    return run


def notify_assignee(run):
    """Tell the person a project was handed to. Best-effort: it also shows on their Home, which is what they are meant to rely on."""
    who = run.assigned_to
    if not who or not who.user.email:
        return False
    where = run.origin_text()
    try:
        send_mail(f'Project for you: {run.process_template.name}', f'{where}\n\nIt is on your Home under Projects.', None, [who.user.email], fail_silently=True)
    except Exception:
        return False
    return True


def run_finished(run):
    """A project is done: close the ticket it came from, and mark the compliance line it came from done (the item closes itself once all its lines are resolved). A process done for one property of a compliance item marks that property's line done."""
    if run.process_template.kind != ProcessTemplate.Kind.PROJECT:
        if run.session_line_id:
            _sync_process_line(run, done=True)
        return
    if run.ticket_id:
        _close_ticket(run)
    if run.session_line_id:
        from worksessions.models import SessionLine
        from worksessions.services import lifecycle
        line = run.session_line
        if line.state == SessionLine.State.IN_PROJECT:
            lifecycle.set_line_state(line, SessionLine.State.DONE)
            lifecycle.close_if_resolved(line.session)


def _sync_process_line(run, *, done):
    from worksessions.models import Session, SessionLine
    from worksessions.services import lifecycle
    line = run.session_line
    if done and line.state == SessionLine.State.PENDING:
        lifecycle.set_line_state(line, SessionLine.State.DONE)
        lifecycle.close_if_resolved(line.session)
    elif not done and line.state == SessionLine.State.DONE:
        lifecycle.set_line_state(line, SessionLine.State.PENDING)
        if line.session.status == Session.Status.SUBMITTED:
            lifecycle.reopen_session(line.session)


def run_reopened(run):
    """A step of a finished project was unticked: the line is back in the project (or, for a process, back to pending)."""
    if run.session_line_id and run.process_template.kind != ProcessTemplate.Kind.PROJECT:
        _sync_process_line(run, done=False)
        return
    if run.session_line_id and run.process_template.kind == ProcessTemplate.Kind.PROJECT:
        from worksessions.models import Session, SessionLine
        line = run.session_line
        if line.state == SessionLine.State.DONE:
            line.state, line.completed_at = SessionLine.State.IN_PROJECT, None
            line.save(update_fields=['state', 'completed_at'])
            if line.session.status == Session.Status.SUBMITTED:
                from worksessions.services import lifecycle
                lifecycle.reopen_session(line.session)


def _close_ticket(run):
    from tickets.models import Ticket, TicketClosingNote
    ticket = run.ticket
    if ticket.status in (Ticket.Status.COMPLETED, Ticket.Status.VERIFIED, Ticket.Status.CANCELLED):
        return
    ticket.status = Ticket.Status.COMPLETED
    ticket.completed_at = timezone.now()
    ticket.save()
    TicketClosingNote.objects.create(ticket=ticket, status=Ticket.Status.COMPLETED, body=f'Finished as a project: {run.process_template.name}.', created_by=run.created_by)


# ------------------------------------------------------------------------------ what a run knows (feeds the generated documents)
def _done_step(run, step_type):
    return run.steps.filter(step_type=step_type, is_complete=True).order_by('sequence_order').first()


def run_property(run):
    """The association a run is about: the one picked in its "Select the association" step, else the run's own property, else its ticket's."""
    from core.models import Property
    for step in run.steps.filter(step_type='record_selector', is_complete=True).order_by('sequence_order'):
        r = step.response or {}
        if r.get('model') == 'property' and str(r.get('id', '')).isdigit():
            prop = Property.objects.filter(pk=int(r['id'])).first()
            if prop:
                return prop
    if run.property_id:
        return run.property
    if run.ticket_id and run.ticket.property_id:
        return run.ticket.property
    return None


def run_unit(run, prop=None):
    """The unit named in the run's "Enter the unit number" step, if the association has one by that label."""
    prop = prop or run_property(run)
    if prop is None:
        return None
    step = run.steps.filter(step_type='short_text', is_complete=True, label__icontains='unit').order_by('sequence_order').first()
    text = ((step.response or {}).get('text') or '').strip() if step else ''
    return prop.units.filter(label__iexact=text).first() if text else None


def run_person(run):
    """The person entered in the run's "name, phone and email" step (a Contact), or None."""
    from core.models import Contact
    step = _done_step(run, 'create_contact')
    cid = (step.response or {}).get('contact_id') if step else None
    return Contact.objects.filter(pk=cid).first() if cid else None


def run_meeting(run):
    """What was typed into the run's meeting-details step: date, time, place, video_link (empty strings when not done yet)."""
    step = _done_step(run, 'meeting_details')
    r = (step.response or {}) if step else {}
    return {k: r.get(k, '') for k in ('date', 'time', 'place', 'video_link')}
