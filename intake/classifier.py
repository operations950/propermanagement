import logging
from datetime import date, datetime

from django.utils import timezone

from core.models import Contact, Property, StaffProfile
from supplies.models import SupplyRequest
from tickets.models import Ticket, TicketContact

from .adapters.base import RawEvent
from .models import GmailThreadState, QuoThreadState, Reservation

logger = logging.getLogger(__name__)

SHORTAGE_KEYWORDS = [
    'toilet paper', 'paper towels', 'trash bags', 'coffee', 'dish soap',
    'light bulbs', 'laundry detergent', 'shampoo', 'hand soap', 'soap',
]


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except ValueError:
        return None


def _get_property(name):
    if not name:
        return None
    prop, _ = Property.objects.get_or_create(name=name)
    return prop


def _get_reporter_contact(event: RawEvent):
    if not (event.reporter_email or event.reporter_phone):
        return None
    lookup = {'email': event.reporter_email} if event.reporter_email else {'phone': event.reporter_phone}
    company = event.extra.get('contact_company', '')
    # is_known_contact is only ever set by adapters that actually look up
    # caller identity against a contacts API (currently just Quo) — its
    # absence means "this source doesn't know," not "unverified," so the
    # unverified-sender note below only applies when the key is present.
    knows_contact_status = 'is_known_contact' in event.extra
    is_known = event.extra.get('is_known_contact', False)
    # A saved-with-a-company Quo contact reads as a business relationship
    # (vendor), not a random guest — a heuristic, not certainty; staff can
    # correct the type via admin if it's wrong. An unrecognized number gets
    # a note so its lack of verification stays visible on the ticket, since
    # the shared line has no access control — anyone can text it.
    defaults = {
        'name': event.reporter_name or event.reporter_email or event.reporter_phone,
        'contact_type': Contact.ContactType.VENDOR if company else Contact.ContactType.GUEST,
        'phone': event.reporter_phone,
    }
    if company:
        defaults['notes'] = f'Company (from Quo contact): {company}'
    elif knows_contact_status and not is_known:
        defaults['notes'] = 'Not a saved Quo contact as of first contact — unverified sender.'
    contact, _ = Contact.objects.get_or_create(**lookup, defaults=defaults)
    return contact


def _extract_shortage_items(text):
    text_lower = text.lower()
    matches = [kw for kw in SHORTAGE_KEYWORDS if kw in text_lower]
    # Drop any match that's just a substring of another match (e.g. don't
    # report both "soap" and "dish soap" for the same mention).
    matches = [kw for kw in matches if not any(kw != other and kw in other for other in matches)]
    return matches or None


def process_event(event: RawEvent):
    """Turn one normalized RawEvent into the right database effect. Safe to
    call repeatedly with the same event (get_or_create on stable external
    ids) — adapters are pull-based, so re-delivery is expected."""
    handler = {
        'booking': _handle_booking,
        'cancellation': _handle_cancellation,
        'maintenance': _handle_maintenance,
        'shortage': _handle_shortage,
        'quo_thread': _handle_quo_thread,
        'gmail_thread': _handle_gmail_thread,
    }.get(event.event_type, _handle_generic)
    return handler(event)


def _handle_booking(event: RawEvent):
    prop = _get_property(event.property_name)
    if prop is None:
        logger.warning('Booking event %s has no property_name, skipping', event.external_id)
        return None
    guest = _get_reporter_contact(event)
    reservation, _ = Reservation.objects.update_or_create(
        source=event.source, external_reservation_id=event.external_id,
        defaults={
            'property': prop, 'guest': guest,
            'check_in': _parse_date(event.check_in), 'check_out': _parse_date(event.check_out),
            'status': Reservation.Status.BOOKED,
        },
    )
    due = reservation.check_out
    ticket, created = Ticket.objects.get_or_create(
        source=event.source, source_reference=event.external_id, kind='cleaning',
        defaults={
            'title': f'Clean {prop.name} after checkout',
            'description': f'Check-out {reservation.check_out}, reservation {event.external_id}.',
            'property': prop,
            'priority': 'medium',
            'due_date': timezone.make_aware(datetime.combine(due, datetime.min.time())) if due else None,
            'assigned_role': StaffProfile.Role.CLEANER,
        },
    )
    return ticket


def _handle_cancellation(event: RawEvent):
    try:
        reservation = Reservation.objects.get(source=event.source, external_reservation_id=event.external_id)
    except Reservation.DoesNotExist:
        logger.warning('Cancellation for unknown reservation %s/%s', event.source, event.external_id)
        return None
    reservation.status = Reservation.Status.CANCELLED
    reservation.save()

    ticket = Ticket.objects.filter(
        source=event.source, source_reference=event.external_id, kind='cleaning',
    ).exclude(status=Ticket.Status.CANCELLED).first()
    if ticket:
        ticket.status = Ticket.Status.CANCELLED
        ticket.cancelled_at = timezone.now()
        ticket.cancelled_reason = 'Linked booking was cancelled'
        ticket.save()
    return ticket


def _handle_maintenance(event: RawEvent):
    prop = _get_property(event.property_name)
    if prop is None:
        logger.warning('Maintenance event %s has no property_name, skipping', event.external_id)
        return None
    reporter = _get_reporter_contact(event)
    ticket, created = Ticket.objects.get_or_create(
        source=event.source, source_reference=event.external_id, kind='maintenance',
        defaults={
            'title': event.title or 'Maintenance issue reported',
            'description': event.body[:140],
            'raw_context': event.body,
            'property': prop,
            'priority': 'high',
            'assigned_role': StaffProfile.Role.MAINTENANCE,
        },
    )
    if created and reporter:
        TicketContact.objects.get_or_create(ticket=ticket, contact=reporter, role=TicketContact.Role.REPORTER)
    return ticket


def _handle_shortage(event: RawEvent):
    prop = _get_property(event.property_name)
    if prop is None:
        logger.warning('Shortage event %s has no property_name, skipping', event.external_id)
        return None
    items = _extract_shortage_items(event.body or event.title) or [None]
    created_requests = []
    for item in items:
        req, _ = SupplyRequest.objects.get_or_create(
            property=prop, source_reference=event.external_id, item_guess=item or '',
            defaults={'raw_text': event.body or event.title},
        )
        created_requests.append(req)
    return created_requests


def _reconcile_thread_ticket(event: RawEvent, conversation_id: str, verdict):
    """Shared by every whole-thread-classification source (Quo, Gmail, ...):
    turn a ThreadVerdict into the right Ticket/SupplyRequest effect.

    Because a thread gets reclassified every time it has new activity (not
    just once), this reconciles against whatever ticket already exists for
    it — a ticket created from an early, partial snapshot of a conversation
    needs to track that conversation as it develops, not freeze at first
    sight. Two safety rules: an untouched ticket (nobody's claimed it or
    added notes) can be auto-updated or auto-cancelled as new verdicts come
    in; once a human has engaged with it, this only adds a note for them to
    review — it never silently rewrites or closes their work.
    """
    role = verdict.role if verdict.role in StaffProfile.Role.values else ''
    kind = 'maintenance' if role == StaffProfile.Role.MAINTENANCE else 'generic'

    # Supply requests aren't reconciled the same way — they're idempotent by
    # (property, source_reference, item) already and lower-stakes than a
    # ticket, so there's nothing to "walk back" the way there is for a
    # ticket someone might already be working.
    existing = None
    if not verdict.is_supply_request:
        existing = (
            Ticket.objects.filter(source=event.source, source_reference=conversation_id, kind=kind)
            .exclude(status=Ticket.Status.CANCELLED).first()
        )
    untouched = bool(existing) and (
        existing.status == Ticket.Status.OPEN
        and not existing.assigned_staff_id
        and not existing.assigned_contact_id
        and not existing.resolution_notes
    )

    if not verdict.actionable or verdict.already_resolved:
        logger.info(
            '%s thread %s: not actionable (already_resolved=%s) — %s',
            event.source, conversation_id, verdict.already_resolved, verdict.reasoning,
        )
        if existing and untouched:
            # Nobody's touched it yet and the thread has since shown this
            # wasn't (or is no longer) a real issue — safe to stand down.
            existing.status = Ticket.Status.CANCELLED
            existing.cancelled_at = timezone.now()
            existing.cancelled_reason = f'Later thread activity: {verdict.reasoning}'[:300]
            existing.save()
            logger.info('%s thread %s: auto-cancelled untouched ticket %s', event.source, conversation_id, existing.pk)
        elif existing:
            # Staff already engaged — don't close it out from under them,
            # just flag it so a human confirms before it's marked done.
            existing.description += (
                f'\n\n[Auto-check {timezone.now():%Y-%m-%d %H:%M}] Later thread activity suggests this may '
                f'already be resolved: {verdict.reasoning}'
            )
            existing.save(update_fields=['description'])
            logger.info(
                '%s thread %s: flagged in-progress ticket %s for review', event.source, conversation_id, existing.pk,
            )
        return existing

    # Don't get_or_create by name — an unrecognized/hallucinated property
    # name should leave the ticket unassigned for staff, not create a new
    # Property row (unlike _get_property, used by sources that report real
    # property identifiers directly).
    prop = Property.objects.filter(name=verdict.property_name).first() if verdict.property_name else None

    if verdict.is_supply_request:
        req, _ = SupplyRequest.objects.get_or_create(
            property=prop, source_reference=conversation_id, item_guess='',
            defaults={'raw_text': verdict.summary},
        )
        return req

    if existing:
        if untouched:
            # Keep it current as the conversation develops — the first
            # classification is often a partial snapshot of an ongoing chat.
            existing.title = verdict.title
            existing.description = verdict.summary
            existing.raw_context = event.body
            existing.priority = verdict.priority
            existing.property = prop or existing.property  # don't clobber a property someone already set
            existing.assigned_role = role or existing.assigned_role
            existing.save()
        # else: a human already engaged — leave their work alone even though
        # the thread has more activity now.
        return existing

    reporter = _get_reporter_contact(event)
    ticket = Ticket.objects.create(
        source=event.source, source_reference=conversation_id, kind=kind,
        title=verdict.title, description=verdict.summary, raw_context=event.body,
        property=prop, priority=verdict.priority, assigned_role=role,
    )
    if reporter:
        TicketContact.objects.get_or_create(ticket=ticket, contact=reporter, role=TicketContact.Role.REPORTER)
    return ticket


def _handle_quo_thread(event: RawEvent):
    """Receives a bounded chunk of a conversation transcript (see
    intake/adapters/quo.py) and defers the "is this actionable" judgment to
    Claude (intake/thread_classifier.py) rather than keyword-matching a
    single message — a problem mentioned mid-thread may be resolved or
    retracted by the end of the same conversation. See
    _reconcile_thread_ticket for how a verdict becomes a Ticket."""
    from .thread_classifier import classify_thread

    conversation_id = event.external_id
    verdict = classify_thread(event.body)

    if verdict is None:
        # Don't advance last_message_id here — a missing key or a failed API
        # call (bad credentials, no credit balance, rate limit, ...) means
        # this thread was never actually classified. Leaving the state
        # untouched means the adapter will offer it again next poll instead
        # of silently skipping it forever once the underlying issue is
        # fixed. thread_classifier.py already logged the specific reason.
        QuoThreadState.objects.get_or_create(
            conversation_id=conversation_id,
            defaults={'phone_number_id': event.extra.get('phone_number_id', ''), 'participant': event.reporter_phone},
        )
        logger.info('Quo thread %s: not classified this run, will retry next poll', conversation_id)
        return None

    QuoThreadState.objects.update_or_create(
        conversation_id=conversation_id,
        defaults={
            'phone_number_id': event.extra.get('phone_number_id', ''),
            'participant': event.reporter_phone,
            'last_message_id': event.extra.get('latest_message_id', ''),
            'last_classified_at': timezone.now(),
        },
    )
    return _reconcile_thread_ticket(event, conversation_id, verdict)


def _handle_gmail_thread(event: RawEvent):
    """Same whole-thread classify-then-reconcile flow as Quo (see
    _handle_quo_thread, intake/adapters/gmail.py), applied to Gmail email
    threads instead of Quo SMS conversations."""
    from .thread_classifier import classify_thread

    thread_id = event.external_id
    verdict = classify_thread(event.body, source_label='email thread')

    if verdict is None:
        GmailThreadState.objects.get_or_create(thread_id=thread_id)
        logger.info('Gmail thread %s: not classified this run, will retry next poll', thread_id)
        return None

    GmailThreadState.objects.update_or_create(
        thread_id=thread_id,
        defaults={
            'last_message_id': event.extra.get('latest_message_id', ''),
            'last_classified_at': timezone.now(),
        },
    )
    return _reconcile_thread_ticket(event, thread_id, verdict)


def _handle_generic(event: RawEvent):
    prop = _get_property(event.property_name)
    if prop is None:
        logger.warning('Generic event %s has no property_name, skipping', event.external_id)
        return None
    reporter = _get_reporter_contact(event)
    ticket, created = Ticket.objects.get_or_create(
        source=event.source, source_reference=event.external_id, kind='generic',
        defaults={
            'title': event.title or 'New request',
            'description': event.body[:140],
            'raw_context': event.body,
            'property': prop,
            'assigned_role': StaffProfile.Role.PROPERTY_MANAGER,
        },
    )
    if created and reporter:
        TicketContact.objects.get_or_create(ticket=ticket, contact=reporter, role=TicketContact.Role.REPORTER)
    return ticket
