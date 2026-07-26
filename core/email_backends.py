"""Sends real mail through Gmail's own HTTPS API instead of SMTP.

Railway blocks outbound SMTP entirely — confirmed by testing both 587 and
465 with valid, correct credentials, both timing out identically. The
Gmail API sidesteps this completely since it's plain HTTPS (same protocol
as every other outbound call this app already makes to Quo/Anthropic/USPS).

Reuses the same shared-mailbox OAuth token intake/gmail_auth.py already
manages for reading the inbox (see that module for why there's exactly one
of these) — sending just needs gmail.send in its scope list too, which
means the mailbox must be (re)connected once after that scope was added,
for the consent screen to grant it.
"""
import base64
import logging
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from django.core.mail.backends.base import BaseEmailBackend

logger = logging.getLogger(__name__)


class GmailAPIBackend(BaseEmailBackend):
    def send_messages(self, email_messages):
        if not email_messages:
            return 0

        from googleapiclient.discovery import build

        from intake.gmail_auth import credentials_for
        from intake.models import GmailInboxToken

        # Multiple mailboxes may be connected (each polled independently for
        # new tickets — see intake/adapters/gmail.py) but exactly one sends
        # outgoing mail, chosen in Admin Tools (GmailInboxToken.is_send_from).
        # .first() as a fallback only matters for an install that connected
        # a mailbox before is_send_from existed and hasn't touched Admin
        # Tools' "Send from" picker since.
        token = GmailInboxToken.objects.filter(is_send_from=True).first() or GmailInboxToken.objects.first()
        if not token:
            if not self.fail_silently:
                raise RuntimeError('No Gmail account connected — connect one in Admin Tools first.')
            return 0

        try:
            creds = credentials_for(token)
            service = build('gmail', 'v1', credentials=creds, cache_discovery=False)
        except Exception:
            logger.exception('Gmail API: could not build an authenticated client')
            if not self.fail_silently:
                raise
            return 0

        sent_count = 0
        for message in email_messages:
            try:
                raw = self._build_raw_message(message, token.mailbox_email)
                service.users().messages().send(userId='me', body={'raw': raw}).execute()
                sent_count += 1
            except Exception:
                logger.exception('Gmail API: send failed for %r', message.to)
                if not self.fail_silently:
                    raise
        return sent_count

    def _build_raw_message(self, message, from_address):
        alternatives = getattr(message, 'alternatives', None) or []
        attachments = getattr(message, 'attachments', None) or []

        if alternatives or attachments:
            body_part = MIMEMultipart('alternative')
            body_part.attach(MIMEText(message.body, 'plain'))
            for content, mimetype in alternatives:
                subtype = mimetype.split('/')[-1] if mimetype else 'html'
                body_part.attach(MIMEText(content, subtype))
            # A bare MIMEText can't carry attachments (only MIMEMultipart
            # supports .attach()) — wrap in an outer multipart/mixed only
            # when there's actually something to attach, so a plain
            # attachment-free send still produces the same message as before.
            mime = MIMEMultipart('mixed') if attachments else body_part
            if attachments:
                mime.attach(body_part)
        else:
            mime = MIMEText(message.body, 'plain')

        mime['Subject'] = message.subject
        mime['From'] = message.from_email or from_address
        mime['To'] = ', '.join(message.to)
        if message.cc:
            mime['Cc'] = ', '.join(message.cc)

        for filename, content, mimetype in attachments:
            maintype, _, subtype = (mimetype or 'application/octet-stream').partition('/')
            part = MIMEBase(maintype or 'application', subtype or 'octet-stream')
            part.set_payload(content.encode('utf-8') if isinstance(content, str) else content)
            encoders.encode_base64(part)
            part.add_header('Content-Disposition', 'attachment', filename=filename)
            mime.attach(part)

        return base64.urlsafe_b64encode(mime.as_bytes()).decode('ascii')
