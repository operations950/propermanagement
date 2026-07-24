import logging
import re
import uuid

from django.conf import settings
from django.core.mail import send_mail

from tickets.models import FollowUpLog

logger = logging.getLogger(__name__)


def _to_e164(phone):
    """Best-effort US E.164 normalization, tolerant of the several shapes
    Contact.phone data is actually in — some already E.164 (contacts
    created straight from Quo's own caller-id lookup), some XXX-XXX-XXXX
    per core.models.phone_validator, some raw digits or short/malformed
    strings that predate either. Returns '' when it can't confidently
    normalize (e.g. a placeholder like "555-0120") rather than guessing."""
    if not phone:
        return ''
    if phone.startswith('+'):
        return phone
    digits = re.sub(r'\D', '', phone)
    if len(digits) == 10:
        return f'+1{digits}'
    if len(digits) == 11 and digits.startswith('1'):
        return f'+{digits}'
    return ''


def fetch_quo_conversation(contact):
    """Recent Quo messages with this contact, live from Quo's API — or
    None if no Quo conversation has ever been linked to their phone number
    (they've never texted the shared Quo line, or it hasn't been polled
    yet). Read-only: only calls QuoAdapter._list_messages (a fetch, not
    part of the poll loop) and never touches PollCursor/QuoThreadState, so
    it can't interfere with the scheduled poller. Returns a list of
    {'direction': 'out'|'in', 'body': str, 'at': iso datetime str} dicts,
    chronological — structured, not the flattened transcript text
    Ticket.raw_context stores, so the caller can render separate bubbles
    by direction."""
    if not contact or not contact.phone:
        return None
    participant = _to_e164(contact.phone)
    if not participant:
        return None

    from intake.models import QuoThreadState

    thread = QuoThreadState.objects.filter(participant=participant).order_by('-updated_at').first()
    if not thread:
        return None

    from intake.adapters.quo import QuoAdapter, QuoAPIError
    import requests

    try:
        # _list_messages is "private" only by naming convention — it's the
        # adapter's own paginated-fetch-plus-sort logic, reused here rather
        # than duplicated so a live re-fetch can't drift from what the
        # poller itself does.
        messages = QuoAdapter()._list_messages(thread.phone_number_id, thread.participant)
    except (requests.RequestException, QuoAPIError):
        logger.exception('Quo: live message fetch failed for contact %s', contact.pk)
        return None

    return [
        {
            'direction': 'out' if m.get('direction') == 'outgoing' else 'in',
            'body': m.get('text', ''),
            'at': m.get('createdAt', ''),
        }
        for m in messages
    ]


def _quo_from_number(thread):
    """The E.164 number our own shared line uses in this thread. Quo's
    conversation/message-list endpoints only give an opaque phoneNumberId,
    never the line's own E.164 number directly — so this derives it from
    the thread's own message history instead (an outgoing message's `from`,
    or an incoming message's `to`), which is already fetched data, not a
    guess. None if the thread has no messages to derive it from."""
    from intake.adapters.quo import QuoAdapter, QuoAPIError
    import requests

    try:
        messages = QuoAdapter()._list_messages(thread.phone_number_id, thread.participant)
    except (requests.RequestException, QuoAPIError):
        logger.exception('Quo: could not resolve our own number for thread %s', thread.pk)
        return None
    for m in reversed(messages):
        if m.get('direction') == 'outgoing' and m.get('from'):
            return m['from']
        if m.get('direction') == 'incoming':
            to = m.get('to') or []
            if to:
                return to[0]
    return None


def send_via_quo(to_number, body):
    """Send `body` to `to_number` through whichever Quo line is already
    talking to them, so the reply lands in the same thread
    fetch_quo_conversation reads from (real two-way texting, not a stub).

    Returns False if `to_number` has no known Quo thread yet — the caller
    should fall back to get_sms_backend() in that case, since we have no
    way to know which of our several Quo lines a brand-new contact should
    be texted from. Raises on an actual Quo API failure (a thread DID
    exist, we DID try, Quo rejected it) — that must surface as a real
    failure to the caller's audit trail, not be swallowed into a fake
    stub "success"."""
    participant = _to_e164(to_number)
    if not participant:
        return False

    from intake.models import QuoThreadState

    thread = QuoThreadState.objects.filter(participant=participant).order_by('-updated_at').first()
    if not thread:
        return False

    from_number = _quo_from_number(thread)
    if not from_number:
        return False

    from intake.adapters.quo import QUO_API_BASE
    import requests

    resp = requests.post(
        f'{QUO_API_BASE}/v1/messages',
        headers={'Authorization': settings.QUO_API_KEY, 'Content-Type': 'application/json'},
        json={'content': body, 'from': from_number, 'to': [participant]},
        timeout=15,
    )
    resp.raise_for_status()
    return True


class LogSMSBackend:
    """Stub backend: logs the message instead of sending it for real.
    Swap in a real provider (e.g. Twilio) once credentials exist — same
    `.send(to_number, body)` interface, wired via SMS_PROVIDER in settings.
    """

    def send(self, to_number, body):
        logger.info('SMS (stub, not actually sent) to %s: %s', to_number, body)


def get_sms_backend():
    if settings.SMS_PROVIDER == 'log':
        return LogSMSBackend()
    raise NotImplementedError(f'SMS provider "{settings.SMS_PROVIDER}" is not configured yet.')


def build_followup_message(ticket):
    subject = f'Update on your request: {ticket.title}'
    body = (
        f'Hi,\n\n'
        f'Following up on "{ticket.title}" at {ticket.property.name}.\n\n'
        f'Status: {ticket.get_status_display()}\n'
    )
    if ticket.resolution_notes:
        body += f'\nNotes: {ticket.resolution_notes}\n'
    body += '\nThanks,\nProperty Management Team'
    return subject, body


def get_reporter_contact(ticket):
    link = ticket.ticket_contacts.filter(role='reporter').select_related('contact').first()
    return link.contact if link else None


def send_followup(ticket, channel, to_override=None, user=None, custom_body=None):
    """Send a one-click resolution follow-up to the ticket's original
    reporter (or `to_override`). Always writes a FollowUpLog row, even on
    failure, so there's a complete audit trail of what was attempted."""
    reporter = get_reporter_contact(ticket)
    subject, body = build_followup_message(ticket)
    if custom_body:
        body = custom_body

    log = FollowUpLog(ticket=ticket, channel=channel, subject=subject, body=body, sent_by=user, sent_to='')

    try:
        if channel == FollowUpLog.Channel.EMAIL:
            to_address = to_override or (reporter.email if reporter else '')
            if not to_address:
                raise ValueError("No email address available for this ticket's reporter.")
            send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [to_address])
            log.sent_to = to_address
        elif channel == FollowUpLog.Channel.SMS:
            to_number = to_override or (reporter.phone if reporter else '')
            if not to_number:
                raise ValueError("No phone number available for this ticket's reporter.")
            if not send_via_quo(to_number, body):
                get_sms_backend().send(to_number, body)
            log.sent_to = to_number
        else:
            raise ValueError(f'Unknown channel: {channel}')
        log.success = True
    except Exception as exc:
        log.success = False
        log.error_message = str(exc)[:300]
        log.sent_to = log.sent_to or (to_override or '')
        logger.exception('Follow-up send failed for ticket %s', ticket.pk)

    log.save()
    return log


def send_followup_bulk(ticket, channel, contact_ids, body, subject='', group=False, user=None):
    """The Follow-Up modal's send action — any number of recipients, one
    FollowUpLog row per contact (even for a combined group email) so "who
    did I message and when" stays per-contact, all sharing one batch_id so
    the audit trail can render one line per Send click. Recipients missing
    the relevant channel's field are silently dropped (defensive — the UI
    only ever offers eligible bubbles to begin with)."""
    from core.models import Contact

    contacts = list(Contact.objects.filter(pk__in=contact_ids))
    if channel == FollowUpLog.Channel.SMS:
        contacts = [c for c in contacts if c.phone]
    else:
        contacts = [c for c in contacts if c.email]
    if not contacts:
        return []

    batch_id = uuid.uuid4()
    logs = []

    if channel == FollowUpLog.Channel.EMAIL and group:
        try:
            send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [c.email for c in contacts])
            success, error = True, ''
        except Exception as exc:
            success, error = False, str(exc)[:300]
            logger.exception('Follow-up group email failed for ticket %s', ticket.pk)
        for contact in contacts:
            logs.append(FollowUpLog(
                ticket=ticket, contact=contact, channel=channel, sent_to=contact.email,
                subject=subject, body=body, batch_id=batch_id, is_group=True,
                sent_by=user, success=success, error_message=error,
            ))
    else:
        for contact in contacts:
            sent_to, success, error = '', True, ''
            try:
                if channel == FollowUpLog.Channel.SMS:
                    sent_to = contact.phone
                    if not send_via_quo(sent_to, body):
                        get_sms_backend().send(sent_to, body)
                else:
                    sent_to = contact.email
                    send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [sent_to])
            except Exception as exc:
                success, error = False, str(exc)[:300]
                logger.exception('Follow-up send failed for ticket %s contact %s', ticket.pk, contact.pk)
            logs.append(FollowUpLog(
                ticket=ticket, contact=contact, channel=channel, sent_to=sent_to,
                subject=subject, body=body, batch_id=batch_id, is_group=False,
                sent_by=user, success=success, error_message=error,
            ))

    for log in logs:
        log.save()

    if any(log.success for log in logs) and not ticket.followup_done:
        ticket.followup_done = True
        ticket.save(update_fields=['followup_done'])

    return logs
