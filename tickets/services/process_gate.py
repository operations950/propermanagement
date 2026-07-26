"""Ticket-close gating for attached Process instances — see
tickets/views.py's ticket_set_status and ticket_close_no_followup, the two
places a ticket reaches Completed/Verified. A ticket cannot close while any
attached ProcessInstance still has an unchecked required item, e.g. an
attached "Board Meeting Checklist" that hasn't been fully run yet."""


def incomplete_process_instances(ticket):
    """Attached, still-active ProcessRuns (see processes/models.py) with at
    least one incomplete required step — or an empty list if every
    attached run is fully complete (or none are attached). Cancelled runs
    don't gate closing — a run someone stood down deliberately shouldn't
    block the ticket."""
    return [
        run for run in ticket.process_runs.exclude(status='cancelled').prefetch_related('steps')
        if not run.is_complete()
    ]


def process_gate_error_message(ticket):
    """None if the ticket is free to close; otherwise a staff-facing
    message naming the incomplete process(es)."""
    incomplete = incomplete_process_instances(ticket)
    if not incomplete:
        return None
    names = ', '.join(run.process_template.name for run in incomplete)
    return f'Finish the attached checklist first: {names} — nothing was changed.'
