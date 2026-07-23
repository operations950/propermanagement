import logging
import uuid

from django.conf import settings
from django.core.mail import send_mail

from tickets.models import FollowUpLog

logger = logging.getLogger(__name__)


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
