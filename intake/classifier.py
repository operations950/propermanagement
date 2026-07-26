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
        'airbnb_email_booking': _handle_airbnb_email_booking,
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
    # Email-sourced tickets need a human to actually read the thread before
    # committing to a department or due date — Claude's property guess still
    # applies (this is what lands them in the "AI Property Match" section of
    # the pending mailbox), but assigned_role stays blank until staff sets it
    # there. Quo/other sources are trusted enough to auto-assign a department
    # outright.
    assigned_role = role if event.source != 'email' else ''

    # Supply requests aren't reconciled the same way — they're idempotent by
    # (property, source_reference, item) already and lower-stakes than a
    # ticket, so there's nothing to "walk back" the way there is for a
    # ticket someone might already be working.
    #
    # Looked up by (source, source_reference) ONLY — NOT also by kind. A
    # thread gets reclassified from scratch every time it has new activity
    # (or on a manual look-back), and Claude's guessed role/kind can shift
    # between runs (e.g. "generic" then "maintenance") for the exact same
    # conversation. Filtering on kind here made that shift invisible to this
    # lookup, so a re-run silently created a SECOND ticket for the same
    # email instead of updating the first — one thread ending up with
    # multiple tickets. kind is allowed to drift on the existing ticket
    # below instead of ever forking a new record.
    existing = None
    if not verdict.is_supply_request:
        existing = (
            Ticket.objects.filter(source=event.source, source_reference=conversation_id)
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
        if existing and untouched:
            # The same thread was previously read as an actionable ticket
            # and is now reclassified as a supply request instead — stand
            # the old ticket down rather than leaving both records live.
            existing.status = Ticket.Status.CANCELLED
            existing.cancelled_at = timezone.now()
            existing.cancelled_reason = 'Later thread activity reclassified this as a supply request'
            existing.save()
        return req

    if existing:
        if untouched:
            # Keep it current as the conversation develops — the first
            # classification is often a partial snapshot of an ongoing chat.
            existing.title = verdict.title
            existing.description = verdict.summary
            existing.raw_context = event.body
            existing.priority = verdict.priority
            existing.kind = kind
            existing.property = prop or existing.property  # don't clobber a property someone already set
            existing.assigned_role = assigned_role or existing.assigned_role
            existing.save()
        # else: a human already engaged — leave their work alone even though
        # the thread has more activity now.
        return existing

    reporter = _get_reporter_contact(event)
    if prop is None and reporter:
        # Claude's per-conversation guess is the primary signal (it's reading
        # THIS specific thread), but if it couldn't tell, an already-approved
        # contact's own property association (set by a human during contact
        # review — see core/models.py's ContactImportCandidate.suggested_property
        # and intake/contact_classifier.py) is a reasonable prefill. Only
        # applied when unambiguous — a contact tied to several properties
        # (e.g. a vendor who serves the whole portfolio) gives no single
        # right answer, so it's left for staff to set instead of guessing.
        contact_properties = list(reporter.properties.all())
        if len(contact_properties) == 1:
            prop = contact_properties[0]

    ticket = Ticket.objects.create(
        source=event.source, source_reference=conversation_id, kind=kind,
        title=verdict.title, description=verdict.summary, raw_context=event.body,
        property=prop, priority=verdict.priority, assigned_role=assigned_role,
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
    mailbox_email = event.extra.get('mailbox_email', '')
    verdict = classify_thread(event.body, source_label='email thread')

    if verdict is None:
        GmailThreadState.objects.get_or_create(mailbox_email=mailbox_email, thread_id=thread_id)
        logger.info('Gmail thread %s (%s): not classified this run, will retry next poll', thread_id, mailbox_email)
        return None

    GmailThreadState.objects.update_or_create(
        mailbox_email=mailbox_email, thread_id=thread_id,
        defaults={
            'last_message_id': event.extra.get('latest_message_id', ''),
            'last_classified_at': timezone.now(),
        },
    )
    return _reconcile_thread_ticket(event, thread_id, verdict)


def _handle_airbnb_email_booking(event: RawEvent):
    """A single Airbnb automated email (not a back-and-forth conversation),
    flagged by GmailAdapter._build_event via its sender domain. Claude
    decides whether it's actually a NEW booking confirmation (as opposed to
    Airbnb's other automated mail — cancellations, payouts, review
    requests, ...) and extracts the reservation details (see
    intake/booking_classifier.py::extract_airbnb_booking).

    Deliberately NOT routed through _handle_booking: that path trusts
    event.property_name enough to auto-create a Property row for it
    (get_or_create — fine for a real booking-platform API integration that
    reports its own already-correct identifiers). Claude's property guess
    here is a best-effort match against existing properties, exactly like
    _reconcile_thread_ticket's — a parsing mistake must never silently
    create a junk duplicate Property, so an unmatched guess leaves the
    ticket's property blank for a human to set via the Pending screen
    instead. The GmailThreadState bookkeeping mirrors _handle_gmail_thread
    so this thread isn't reclassified every poll either way."""
    from .booking_classifier import extract_airbnb_booking

    thread_id = event.external_id
    mailbox_email = event.extra.get('mailbox_email', '')
    # event.title is the latest message's Subject (see GmailAdapter._build_event)
    # — Airbnb's clearest signal for which of its automated email types this
    # is; previously never reached Claude at all.
    extract = extract_airbnb_booking(event.body, subject=event.title)

    if extract is None:
        GmailThreadState.objects.get_or_create(mailbox_email=mailbox_email, thread_id=thread_id)
        logger.info('Airbnb email %s (%s): not classified this run, will retry next poll', thread_id, mailbox_email)
        return None

    GmailThreadState.objects.update_or_create(
        mailbox_email=mailbox_email, thread_id=thread_id,
        defaults={
            'last_message_id': event.extra.get('latest_message_id', ''),
            'last_classified_at': timezone.now(),
        },
    )

    # The stable Gmail thread id, not Claude's extracted confirmation_code —
    # the exact confirmation_code text Claude reads back can vary slightly
    # between repeated reads of the SAME thread (a normal poll retry, or a
    # manual admin look-back rerun), which previously let one physical email
    # spawn a second ticket under a different source_reference. This is the
    # same class of bug already fixed for the generic thread-classification
    # path in _reconcile_thread_ticket — key on the one thing guaranteed
    # stable and unique, not an AI-derived value. A genuinely separate Gmail
    # thread for the same reservation (e.g. a later reminder Airbnb didn't
    # thread together with the original) isn't covered by this — Gmail's own
    # threading keeps related Airbnb notifications together in practice.
    reservation_id = thread_id

    if not extract.is_booking_confirmation:
        logger.info('Airbnb email %s: not a new booking confirmation, skipping', thread_id)
        # If an earlier message in this same thread already created a
        # ticket (e.g. Claude read a real confirmation, and this later
        # message is Airbnb's cancellation notice for the same thread),
        # stand it down — mirrors _reconcile_thread_ticket's own
        # untouched-ticket auto-cancel rule.
        existing = Ticket.objects.filter(
            source='airbnb', source_reference=reservation_id, kind='cleaning',
        ).exclude(status=Ticket.Status.CANCELLED).first()
        if (
            existing and existing.status == Ticket.Status.OPEN
            and not existing.assigned_staff_id and not existing.assigned_contact_id
            and not existing.resolution_notes
        ):
            existing.status = Ticket.Status.CANCELLED
            existing.cancelled_at = timezone.now()
            existing.cancelled_reason = 'Later Airbnb thread activity is not a booking confirmation (e.g. a cancellation)'
            existing.save()
            logger.info('Airbnb thread %s: auto-cancelled untouched ticket %s', thread_id, existing.pk)
        return None

    prop = Property.objects.filter(name=extract.property_name).first() if extract.property_name else None
    # No Contact is created for the guest here — event.reporter_email/name
    # come from the email's From header, which for an Airbnb automated
    # notification is Airbnb's own system address, not the guest's real
    # contact info (Airbnb never exposes that in these emails). Claude's
    # guest_name is descriptive text only.
    check_out = _parse_date(extract.check_out)
    guest_label = f' for {extract.guest_name}' if extract.guest_name else ''
    confirmation_note = f' (confirmation {extract.confirmation_code})' if extract.confirmation_code else ''

    if prop:
        Reservation.objects.update_or_create(
            source='airbnb', external_reservation_id=reservation_id,
            defaults={
                'property': prop,
                'check_in': _parse_date(extract.check_in), 'check_out': check_out,
                'status': Reservation.Status.BOOKED,
            },
        )

    ticket, _created = Ticket.objects.get_or_create(
        source='airbnb', source_reference=reservation_id, kind='cleaning',
        defaults={
            'title': f'Clean {prop.name} after checkout' if prop else 'Clean after Airbnb checkout',
            'description': f'Airbnb reservation{confirmation_note}{guest_label}' + (f', check-out {check_out}' if check_out else '') + '.',
            'raw_context': event.body,
            'property': prop,
            'priority': 'medium',
            'due_date': timezone.make_aware(datetime.combine(check_out, datetime.min.time())) if check_out else None,
            'assigned_role': StaffProfile.Role.CLEANER,
        },
    )
    return ticket


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
