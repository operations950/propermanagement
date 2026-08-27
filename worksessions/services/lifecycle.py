"""Session/SessionLine state transitions — the sessions equivalent of
onsite/services/checklist.py's submit_visit/create_visit pair, built
independently (no shared code) per this app's own build brief.

Submitting a Session with pending lines is allowed and deliberate: "a
session submitted with gaps is data, not an error" — there is no
mandatory-completion gate here the way onsite.submit_visit has one.
"""
from django.utils import timezone

from .. import models as sessions_models


def set_line_state(line, state, *, skip_reason='', notes=None):
    """Moves a SessionLine to a new state. Raises django.core.exceptions.
    ValidationError (via full_clean, see SessionLine.clean) if state is
    Skipped/Not applicable and no reason is given — the form-layer half of
    the enforcement; SessionLine.Meta's CheckConstraint is the other half,
    at the database level, so no code path can bypass it."""
    line.state = state
    if state == sessions_models.SessionLine.State.DONE:
        line.completed_at = timezone.now()
        line.skip_reason = ''
    elif state in sessions_models.SessionLine.REASON_REQUIRED_STATES:
        line.completed_at = timezone.now()
        line.skip_reason = skip_reason
    else:  # back to pending
        line.completed_at = None
        line.skip_reason = ''
    if notes is not None:
        line.notes = notes
    line.full_clean()
    line.save()
    return line


def submit_session(session):
    session.status = sessions_models.Session.Status.SUBMITTED
    session.submitted_at = timezone.now()
    session.save(update_fields=['status', 'submitted_at'])
    return session


def reopen_session(session):
    session.status = sessions_models.Session.Status.OPEN
    session.submitted_at = None
    session.save(update_fields=['status', 'submitted_at'])
    return session


def promote_to_ticket(line, *, description='', created_by=None):
    """A line that went wrong becomes a real Ticket, carrying the line's
    property (if it has one) — mirrors onsite's VisitIssue -> Ticket bridge
    on submit. Idempotent: calling this again on an already-promoted line
    just returns the existing ticket rather than creating a second one.

    created_by is the staff user who clicked Promote, when there is one —
    source stays SESSION either way (that's the workflow this came from);
    created_by is the separate, orthogonal "which human was at the
    keyboard" question."""
    from tickets.models import Ticket

    if line.promoted_ticket_id:
        return line.promoted_ticket

    session = line.session
    ticket = Ticket.objects.create(
        title=line.label[:200],
        description=description or f'Promoted from session line "{line.label}" ({session}).',
        property=line.property,
        assigned_role=session.department,
        assigned_staff=session.owner if session.owner_id else None,
        source=Ticket.Source.SESSION,
        created_by=created_by,
    )
    line.promoted_ticket = ticket
    line.save(update_fields=['promoted_ticket'])
    return ticket
