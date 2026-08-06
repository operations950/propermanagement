"""Notifies whoever an on-site visit gets assigned to. A visit isn't a
Ticket or Property, so messaging.services.send_followup_bulk (which
requires exactly one of those) doesn't fit here — this goes straight to
the same underlying primitives it uses (send_via_quo/get_sms_backend for
SMS, send_mail for email) instead of adding a second messaging
abstraction.

No FollowUpLog row gets written — that model is ticket-or-property scoped
too (see its CheckConstraint), and a visit assignment ping is closer to
the vendor-completion-link SMS (a one-off notice, not a tracked
conversation) than to the Follow-Up modal's logged correspondence."""
import logging

from django.conf import settings
from django.core.mail import send_mail
from django.urls import reverse

logger = logging.getLogger(__name__)


def _assignee_contact_info(visit):
    """(name, phone, email) for whoever visit is currently assigned to, or
    (None, None, None) if unassigned. Staff get their phone from
    StaffProfile and email from the underlying User; a contact (external
    cleaner) carries both directly."""
    if visit.assigned_staff_id:
        staff = visit.assigned_staff
        name = staff.user.get_full_name() or staff.user.username
        return name, staff.phone, staff.user.email
    if visit.assigned_contact_id:
        contact = visit.assigned_contact
        return contact.name, contact.phone, contact.email
    return None, None, None


def notify_assignee(visit, request):
    """Texts and emails whoever `visit` is now assigned to, with the
    property/date and their token link. Best-effort and silent per
    channel: no phone on file just skips the text, no email just skips
    the email, and an actual send failure is logged, never raised —
    assigning a visit must never fail (or look like it failed) because a
    text couldn't go out. Call this right after the assignment is saved,
    while `request` (for build_absolute_uri) is still in scope."""
    name, phone, email = _assignee_contact_info(visit)
    if not name:
        return

    link = request.build_absolute_uri(reverse('onsite_visit_public', args=[visit.access_token]))
    when = visit.scheduled_date.strftime('%A, %b %d') if visit.scheduled_date else 'a date to be confirmed'
    if visit.scheduled_start:
        when += f' at {visit.scheduled_start.strftime("%I:%M %p").lstrip("0")}'

    first_name = name.split()[0] if name else ''
    deep_clean_note = ' (deep clean)' if visit.is_deep_clean else ''
    body = (
        f'Hi {first_name}, you\'re scheduled for a {visit.visit_type}{deep_clean_note} at '
        f'{visit.property.name} on {when}. Details: {link}'
    ).strip()

    if phone:
        try:
            from messaging.services import get_sms_backend, send_via_quo
            if not send_via_quo(phone, body):
                get_sms_backend().send(phone, body)
        except Exception:
            logger.exception('Failed to text visit assignment (visit %s) to %s', visit.pk, phone)

    if email:
        try:
            send_mail(
                f'On-site visit scheduled — {visit.property.name}', body,
                settings.DEFAULT_FROM_EMAIL, [email],
            )
        except Exception:
            logger.exception('Failed to email visit assignment (visit %s) to %s', visit.pk, email)
