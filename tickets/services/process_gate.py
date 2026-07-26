"""Ticket-close gating for attached Process instances — see
tickets/views.py's ticket_set_status and ticket_close_no_followup, the two
places a ticket reaches Completed/Verified. A ticket cannot close while any
attached ProcessInstance still has an unchecked required item, e.g. an
attached "Board Meeting Checklist" that hasn't been fully run yet."""


def incomplete_process_instances(ticket):
    """Attached ProcessInstances (see processes/models.py) with at least
    one unchecked required item — or an empty list if every attached
    instance is fully checked (or none are attached)."""
    return [
        instance for instance in ticket.process_instances.prefetch_related('items')
        if not instance.is_complete()
    ]


def process_gate_error_message(ticket):
    """None if the ticket is free to close; otherwise a staff-facing
    message naming the incomplete process(es)."""
    incomplete = incomplete_process_instances(ticket)
    if not incomplete:
        return None
    names = ', '.join(instance.process_template.name for instance in incomplete)
    return f'Finish the attached checklist first: {names} — nothing was changed.'
