import logging

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
