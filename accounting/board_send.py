"""The last step of the month-end close: send the month's financial package to the board. The package is the one the person has just reviewed (package.build); the email is composed
for them, they see who it goes to, and once it is sent the step is complete."""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage
from django.core.validators import validate_email

from . import labels, ledger, package
from .models import BoardPackageSend, BoardTerm, MonthLock

# Who the board email comes from and who is copied. Change them here.
FROM_EMAIL = 'Proper Realty Co <admin@proper-realty.com>'          # the name people see in their inbox, then the address it is sent from
CC_EMAILS = ['ana@proper-realty.com', 'justin@proper-realty.com']
SIGN_OFF = 'Proper Realty team'

_NO_DELIVERY = ('django.core.mail.backends.console.EmailBackend', 'django.core.mail.backends.dummy.EmailBackend')


class BoardSendError(Exception):
    pass


def valid_email(text):
    """The address trimmed, or '' when it is blank or not an address."""
    text = (text or '').strip()
    if not text:
        return ''
    try:
        validate_email(text)
    except ValidationError:
        return ''
    return text


def board_rows(association):
    """The current board, one row each: key (the board term), name, position, email ('' when there is none we can send to) and the raw email as recorded."""
    rows = []
    for t in BoardTerm.objects.filter(association=association, end_date__isnull=True).select_related('person').order_by('position', 'person__name'):
        rows.append({'key': t.pk, 'name': t.person.name, 'position': t.position, 'email': valid_email(t.person.email), 'recorded': (t.person.email or '').strip()})
    return rows


def default_subject(association, month_first):
    return f'{association.name} financial package for {month_first:%B %Y}'


def default_body(association, month_first):
    if labels.is_commercial(association):
        return (f'Hi all,\n\nAttached is the financial package for {association.name} for {month_first:%B %Y}. It has the financial statements, the tenant aging and the bank '
                f'statements for the month.\n\nPlease let me know if you have any questions or would like to go over any of it. I am happy to help.\n\nThank you,\n{SIGN_OFF}')
    return (f'Hi all,\n\nAttached is the financial package for {association.name} for {month_first:%B %Y}. It has the financial statements, the budget comparison, the owner aging and the bank '
            f'statements for the month.\n\nPlease let me know if you have any questions or would like to go over any of it. I am happy to help.\n\nThank you,\n{SIGN_OFF}')


def last_send(association, month_first):
    return BoardPackageSend.objects.filter(association=association, month=ledger.month_start(month_first)).first()


def is_locked(association, month_first):
    return MonthLock.objects.filter(association=association, month=ledger.month_start(month_first)).exists()


def status(association, month_first):
    """(status, detail) for the "Send financials to the board" step: not started until the month is closed, ready once it is, done once it has been sent."""
    sent = last_send(association, month_first)
    if sent is not None:
        who = 'owner' if labels.is_commercial(association) else 'board member'
        return 'done', f'Sent {sent.sent_at:%b} {sent.sent_at.day} to {len(sent.recipients)} {who}{"s" if len(sent.recipients) != 1 else ""}.'
    if not is_locked(association, month_first):
        return 'not_started', 'Close the month first.'
    return 'attention', f'Review the package and send it to {labels.label(association, "package_to")}.'


def delivery_available():
    return settings.EMAIL_BACKEND not in _NO_DELIVERY


def send(association, month_first, rows, subject, body, user=None, basis='accrual'):
    """Send the package to the chosen board rows (as given by board_rows). Nothing is sent, and nothing recorded, unless every address is a real one and the month is closed."""
    month_first = ledger.month_start(month_first)
    if not is_locked(association, month_first):
        raise BoardSendError(f'Close {month_first:%B %Y} first: {labels.label(association, "package_gets")} the package once the month is closed.')
    emails = []
    for r in rows:
        email = valid_email(r.get('email'))
        if not email:
            raise BoardSendError(f'{r.get("name") or "A " + labels.label(association, "package_person")} has no email address on file, so nothing can be sent to them.')
        if email.lower() not in [e.lower() for e in emails]:
            emails.append(email)
    if not emails:
        raise BoardSendError(f'Choose at least one {labels.label(association, "package_person")} who has an email address.')
    subject = ' '.join((subject or '').split())[:200]
    body = (body or '').strip()
    if not subject or not body:
        raise BoardSendError('The email needs a subject and a message.')
    if not delivery_available():
        raise BoardSendError('No email delivery is set up yet: connect a Gmail mailbox in Admin Tools. Nothing was sent.')
    cc = [c for c in CC_EMAILS if c.lower() not in [e.lower() for e in emails]]
    filename = package.file_name(association, month_first)
    mail = EmailMessage(subject, body, FROM_EMAIL, to=emails, cc=cc)
    mail.attach(filename, package.build(association, month_first, basis), 'application/pdf')
    if mail.send(fail_silently=False) != 1:
        raise BoardSendError('The email could not be sent. Nothing was recorded; try again.')
    return BoardPackageSend.objects.create(association=association, month=month_first, sent_by=user, subject=subject, body=body, filename=filename, cc=cc,
                                           recipients=[{'name': r.get('name', ''), 'position': r.get('position', ''), 'email': valid_email(r.get('email'))} for r in rows])
