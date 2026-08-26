import logging
import re
import uuid

from django.conf import settings
from django.core.mail import EmailMessage, send_mail

from tickets.models import FollowUpLog

logger = logging.getLogger(__name__)


def _send_email(subject, body, from_email, to_list, attachments=None):
    """send_mail() has no way to carry attachments — this is the one extra
    step needed to reuse it here: build the same message via EmailMessage
    instead, which every configured backend (console/SMTP/GmailAPIBackend)
    already knows how to send identically. attachments is a list of
    TicketAttachment (photos only — see ticket_followup_email's caller)."""
    if not attachments:
        send_mail(subject, body, from_email, to_list)
        return
    email = EmailMessage(subject, body, from_email, to_list)
    for attachment in attachments:
        name = attachment.file.name.rsplit('/', 1)[-1]
        attachment.file.open('rb')
        try:
            email.attach(name, attachment.file.read())
        finally:
            attachment.file.close()
    email.send()


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


def _to_dash_format(phone):
    """Best-effort conversion to core.models.phone_validator's XXX-XXX-XXXX
    shape, tolerant of E.164 (Quo's API always returns this) or any other
    digit-bearing shape. Returns '' when it can't confidently normalize —
    used when STAGING a phone number (e.g. sync_quo_contacts) so the value
    already matches what the review form's is_valid_phone check requires,
    instead of leaving Quo's raw +1XXXXXXXXXX in place and having approval
    silently fail until a human manually retypes it."""
    if not phone:
        return ''
    digits = re.sub(r'\D', '', phone)
    if len(digits) == 11 and digits.startswith('1'):
        digits = digits[1:]
    if len(digits) != 10:
        return ''
    return f'{digits[0:3]}-{digits[3:6]}-{digits[6:10]}'


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


def _parse_iso(ts):
    from django.utils import timezone as dj_timezone
    from django.utils.dateparse import parse_datetime

    if not ts:
        return None
    dt = parse_datetime(ts)
    if dt and dj_timezone.is_naive(dt):
        dt = dj_timezone.make_aware(dt)
    return dt


def _record_sent_message(data, ticket):
    """Echo our own outbound send into QuoMessage immediately, rather than
    waiting on the message.delivered webhook to (redundantly) tell us what
    we already know we just sent. More importantly: the first time a
    ticket sends through Quo, lock it to that specific conversation_id
    (Ticket.source_reference) — see tickets/views.py's _contractor_thread
    for why matching by contact phone number alone isn't precise enough
    once a contact has more than one conversation going."""
    from intake.models import QuoMessage

    conversation_id = data.get('conversationId', '')
    message_id = data.get('id', '')
    if message_id:
        QuoMessage.objects.get_or_create(message_id=message_id, defaults={
            'conversation_id': conversation_id,
            'phone_number_id': data.get('phoneNumberId', ''),
            'direction': QuoMessage.Direction.OUT,
            'from_number': data.get('from', ''),
            'to_number': ','.join(data.get('to') or []),
            'body': data.get('text', ''),
            'quo_created_at': _parse_iso(data.get('createdAt', '')),
        })
    if ticket and conversation_id and not ticket.source_reference:
        ticket.source_reference = conversation_id
        ticket.save(update_fields=['source_reference'])


def send_via_quo(to_number, body, ticket=None, media_urls=None):
    """Send `body` to `to_number` through Quo — whichever line is already
    talking to them if a thread exists (so the reply lands in the same
    thread fetch_quo_conversation reads from), or settings.QUO_DEFAULT_FROM_NUMBER
    if this is the first message to them (initiating, not replying — Quo's
    own poller picks up the new thread afterward same as any inbound one).

    Returns False only if `to_number` doesn't normalize to a real phone
    number, or no default line is configured for a first-contact send —
    the caller should fall back to get_sms_backend() in that case. Raises
    on an actual Quo API failure (we did try to send, Quo rejected it) —
    that must surface as a real failure to the caller's audit trail, not
    be swallowed into a fake stub "success".

    `ticket`, when given, gets bound to whichever Quo conversation this
    send lands in (see _record_sent_message) — the first Quo send tied to
    a ticket is what establishes that binding, since a manually-created
    ticket has no conversation_id of its own until it actually talks to
    someone through Quo.

    `media_urls`, when given, sends as MMS — Quo (OpenPhone's API) expects
    publicly-fetchable URLs, not uploaded bytes, so the caller must already
    have absolute URLs (see ticket_followup_sms building these from
    TicketAttachment.file.url via request.build_absolute_uri)."""
    participant = _to_e164(to_number)
    if not participant:
        return False

    from intake.models import QuoThreadState

    thread = QuoThreadState.objects.filter(participant=participant).order_by('-updated_at').first()
    if thread:
        from_number = _quo_from_number(thread)
    else:
        from_number = settings.QUO_DEFAULT_FROM_NUMBER

    if not from_number:
        return False

    from intake.adapters.quo import QUO_API_BASE
    import requests

    payload = {'content': body, 'from': from_number, 'to': [participant]}
    if media_urls:
        payload['mediaUrls'] = media_urls
    resp = requests.post(
        f'{QUO_API_BASE}/v1/messages',
        headers={'Authorization': settings.QUO_API_KEY, 'Content-Type': 'application/json'},
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()
    # The send itself is already done and irreversible by this point — a
    # problem recording it locally (malformed/empty response body, a DB
    # hiccup writing QuoMessage) must never turn an actually-successful
    # send into a reported failure. Log it for whoever needs to notice the
    # bookkeeping gap, but still report True.
    try:
        _record_sent_message(resp.json().get('data', {}), ticket)
    except Exception:
        logger.exception('Quo: message sent but recording it locally failed')
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
            if not send_via_quo(to_number, body, ticket=ticket):
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


def send_followup_bulk(
    channel, contact_ids, body, ticket=None, property=None, subject='', group=False, user=None,
    attachments=None, media_urls=None,
):
    """The Follow-Up modal's send action — and the property dashboard's
    Communication card — any number of recipients, one FollowUpLog row per
    contact (even for a combined group email) so "who did I message and
    when" stays per-contact, all sharing one batch_id so the audit trail
    can render one line per Send click. Recipients missing the relevant
    channel's field are silently dropped (defensive — the UI only ever
    offers eligible bubbles to begin with).

    Exactly one of `ticket`/`property` must be set — matches
    FollowUpLog's own CheckConstraint, this is just where that invariant
    first gets enforced rather than failing at .save().

    `attachments` (TicketAttachment objects, for EMAIL) and `media_urls`
    (absolute URL strings, for SMS/MMS) are both optional and only ever
    populated by the ticket Follow-Up modal's photo picker — the property
    Communication card has no ticket attachments to draw from."""
    if bool(ticket) == bool(property):
        raise ValueError('send_followup_bulk requires exactly one of ticket or property.')
    context = ticket or property
    context_kwargs = {'ticket': ticket, 'property': property}

    from core.models import Contact

    # Defensive against a malformed id reaching here (e.g. a group-tier
    # bubble-picker header that isn't itself a real contact, or any other
    # future non-numeric value) — pk__in would otherwise raise ValueError
    # on the first bad entry and 500 the whole send, silently dropping
    # every legitimately-selected recipient in the same submission along
    # with it. Matches this function's own "silently dropped" philosophy
    # for recipients missing the channel's field, just one step earlier.
    contact_ids = [cid for cid in contact_ids if str(cid).isdigit()]
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
            _send_email(subject, body, settings.DEFAULT_FROM_EMAIL, [c.email for c in contacts], attachments)
            success, error = True, ''
        except Exception as exc:
            success, error = False, str(exc)[:300]
            logger.exception('Follow-up group email failed for %s', context)
        for contact in contacts:
            logs.append(FollowUpLog(
                **context_kwargs, contact=contact, channel=channel, sent_to=contact.email,
                subject=subject, body=body, batch_id=batch_id, is_group=True,
                sent_by=user, success=success, error_message=error,
            ))
    else:
        for contact in contacts:
            sent_to, success, error = '', True, ''
            try:
                if channel == FollowUpLog.Channel.SMS:
                    sent_to = contact.phone
                    if not send_via_quo(sent_to, body, ticket=ticket, media_urls=media_urls):
                        get_sms_backend().send(sent_to, body)
                else:
                    sent_to = contact.email
                    _send_email(subject, body, settings.DEFAULT_FROM_EMAIL, [sent_to], attachments)
            except Exception as exc:
                success, error = False, str(exc)[:300]
                logger.exception('Follow-up send failed for %s contact %s', context, contact.pk)
            logs.append(FollowUpLog(
                **context_kwargs, contact=contact, channel=channel, sent_to=sent_to,
                subject=subject, body=body, batch_id=batch_id, is_group=False,
                sent_by=user, success=success, error_message=error,
            ))

    for log in logs:
        log.save()

    if ticket and any(log.success for log in logs) and not ticket.followup_done:
        ticket.followup_done = True
        ticket.save(update_fields=['followup_done'])

    return logs


def _followup_result_message(request, logs, recipient_noun):
    from django.contrib import messages

    succeeded = sum(1 for log in logs if log.success)
    failed = len(logs) - succeeded
    if not logs:
        messages.error(request, 'Nothing sent — no eligible recipient was selected.')
    elif failed == 0:
        messages.success(request, f'Sent to {succeeded} {recipient_noun}.')
    elif succeeded == 0:
        messages.error(request, f'Failed to send to all {failed} {recipient_noun}.')
    else:
        messages.warning(request, f'Sent to {succeeded} {recipient_noun}, failed for {failed}.')


def _group_followups(followups):
    """One entry per batch_id (everything created by a single Send click,
    whether from a ticket's Follow-Up modal or a property's Communication
    card) — followups is already ordered -sent_at, and every row in one
    batch is created back-to-back in the same request, so rows for a batch
    are always contiguous in that ordering."""
    batches, order = {}, []
    for log in followups:
        if log.batch_id not in batches:
            batches[log.batch_id] = []
            order.append(log.batch_id)
        batches[log.batch_id].append(log)
    result = []
    for batch_id in order:
        logs = batches[batch_id]
        first = logs[0]
        result.append({
            'logs': logs,
            'channel': first.channel,
            'sent_at': first.sent_at,
            'sent_by': first.sent_by,
            'subject': first.subject,
            'body': first.body,
            'is_group': first.is_group,
            'recipients': [log.contact.name if log.contact else log.sent_to for log in logs],
            'all_success': all(log.success for log in logs),
            'any_success': any(log.success for log in logs),
        })
    return result
