"""Reading vendor invoices out of the shared Gmail mailbox.

Emails carrying a PDF under a Gmail label (default "Invoices") are read: Claude pulls out the vendor, invoice number, dates and total and says which association it is
for; each becomes a draft bill under "To review". Nothing is ever approved or paid by this: a person approves every draft. An invoice whose association cannot be told is
listed so a person can say which. Without the mailbox connected or the Claude connection, this quietly does nothing.
"""
import base64
import logging
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.files.base import ContentFile
from django.utils import timezone

from core.models import Property

from . import ledger, payables
from . import standard_chart as sc
from .models import Account, AssociationProfile, Bill, InvoiceInbox, SeenEmail, VendorTerms

logger = logging.getLogger(__name__)
MODEL = 'claude-sonnet-5'
MAX_PER_RUN = 15


def mailbox():
    from intake.models import GmailInboxToken
    return GmailInboxToken.objects.order_by('-is_send_from', 'pk').first()


def is_configured():
    from intake import gmail_auth
    return bool(gmail_auth.is_configured() and mailbox() is not None)


def _service():
    from googleapiclient.discovery import build
    from intake import gmail_auth
    creds = gmail_auth.credentials_for(mailbox())
    return build('gmail', 'v1', credentials=creds, cache_discovery=False)


def _walk(parts):
    for p in parts or []:
        yield p
        yield from _walk(p.get('parts'))


def _load(svc, message_id):
    msg = svc.users().messages().get(userId='me', id=message_id, format='full').execute()
    headers = {h['name'].lower(): h['value'] for h in msg.get('payload', {}).get('headers', [])}
    pdfs = []
    for part in _walk([msg.get('payload', {})]):
        name = part.get('filename') or ''
        if name.lower().endswith('.pdf') and part.get('body', {}).get('attachmentId'):
            att = svc.users().messages().attachments().get(userId='me', messageId=message_id, id=part['body']['attachmentId']).execute()
            pdfs.append((name, base64.urlsafe_b64decode(att['data'])))
    return {'id': message_id, 'subject': headers.get('subject', ''), 'sender': headers.get('from', ''), 'pdfs': pdfs}


def fetch_new(label, limit=MAX_PER_RUN):
    """New messages under the label that carry a PDF: [{'id', 'subject', 'sender', 'pdfs': [(filename, bytes)]}]."""
    svc = _service()
    found = svc.users().messages().list(userId='me', q=f'label:{label} has:attachment filename:pdf -in:trash', maxResults=limit * 2).execute().get('messages', [])
    seen = set(SeenEmail.objects.filter(message_id__in=[m['id'] for m in found]).values_list('message_id', flat=True))
    return [_load(svc, m['id']) for m in [m for m in found if m['id'] not in seen][:limit]]


def fetch_one(message_id):
    return _load(_service(), message_id)


INVOICE_TOOL = {
    'name': 'report_invoice',
    'description': 'Report what an invoice says.',
    'input_schema': {'type': 'object', 'properties': {
        'is_invoice': {'type': 'boolean', 'description': 'True if this is a bill asking us to pay. False for a statement of account with nothing due, a receipt, or something else.'},
        'vendor': {'type': 'string'}, 'invoice_number': {'type': 'string'},
        'invoice_date': {'type': ['string', 'null'], 'description': 'YYYY-MM-DD'}, 'due_date': {'type': ['string', 'null'], 'description': 'YYYY-MM-DD'},
        'total': {'type': 'number', 'description': 'The total amount due.'},
        'association': {'type': 'string', 'description': 'The condominium or homeowners association, or the property address, the work or bill is for. Empty if it does not say.'},
        'description': {'type': 'string', 'description': 'One short line saying what the bill is for.'}},
        'required': ['is_invoice', 'vendor', 'total']},
}


def extract_invoice(data, filename, association_names):
    if not settings.ANTHROPIC_API_KEY:
        raise RuntimeError('no claude connection')
    import anthropic
    from core.app_settings import sanitized_setting
    client = anthropic.Anthropic(api_key=sanitized_setting('ANTHROPIC_API_KEY'))
    message = client.messages.create(
        model=MODEL, max_tokens=1200, tools=[INVOICE_TOOL], tool_choice={'type': 'tool', 'name': 'report_invoice'},
        messages=[{'role': 'user', 'content': [
            {'type': 'document', 'source': {'type': 'base64', 'media_type': 'application/pdf', 'data': base64.b64encode(data).decode()}},
            {'type': 'text', 'text': 'Read this vendor invoice for a property management company. The associations we manage: ' + '; '.join(association_names) + '. Say which one it is for.'}]}])
    block = next((b for b in message.content if b.type == 'tool_use'), None)
    if block is None:
        raise RuntimeError('nothing came back')
    return block.input


def _norm(s):
    return re.sub(r'[^a-z0-9 ]', '', (s or '').lower().replace('st.', 'st')).strip()


def match_association(hint, extra=''):
    """The association (that we keep books for) a hint such as 'St. Andrews Glen Condominium' or an address points at."""
    candidates = [a.property for a in AssociationProfile.objects.filter(we_do_financials=True).select_related('property')]
    text = _norm(f'{hint} {extra}')
    best, best_len = None, 0
    for prop in candidates:
        for name in {_norm(prop.name), _norm(ledger.profile_for(prop).legal_name)}:
            if name and name in text and len(name) > best_len:
                best, best_len = prop, len(name)
        street = _norm(prop.street)
        if street and street in text and len(street) > best_len:
            best, best_len = prop, len(street)
    if best is None and len(candidates) == 1 and not (hint or '').strip():
        return None
    return best


def _date(value):
    try:
        return datetime.strptime(value, '%Y-%m-%d').date() if value else None
    except (ValueError, TypeError):
        return None


def draft_from(association, parsed, data, filename, message_id='', user=None):
    """Make one draft bill from an extracted invoice. Returns the bill, or None if it is a duplicate."""
    vendor = payables.vendor_for_name(parsed.get('vendor'), create=False)
    number = (parsed.get('invoice_number') or '').strip()
    if number and Bill.objects.filter(association=association, number=number).exclude(status=Bill.Status.VOID).filter(vendor=vendor if vendor else None, vendor_text='' if vendor else parsed.get('vendor', '')[:200]).exists():
        return None
    try:
        total = Decimal(str(parsed['total'])).quantize(Decimal('0.01'))
    except (InvalidOperation, KeyError):
        raise ValueError('no total')
    terms = VendorTerms.objects.filter(association=association, vendor=vendor).first() if vendor else None
    account = (terms.default_account if terms and terms.default_account_id else None) or ledger.account(association, sc.UNCATEGORIZED)
    bill = payables.create_bill(association, _date(parsed.get('invoice_date')) or timezone.localdate(), [(account, total, parsed.get('description') or '')], vendor=vendor,
                                vendor_text='' if vendor else (parsed.get('vendor') or '')[:200], number=number, due=_date(parsed.get('due_date')), kind=Bill.Kind.EXCEPTION,
                                source=Bill.Source.EMAIL, user=user, memo=(parsed.get('description') or '')[:300], email_message_id=message_id)
    bill.file.save(filename, ContentFile(data), save=True)
    return bill


def process_message(msg, association=None, user=None):
    """Handle one fetched message. Returns a short outcome for the log."""
    seen, _ = SeenEmail.objects.get_or_create(message_id=msg['id'], defaults={'subject': msg['subject'][:300], 'sender': msg['sender'][:200]})
    names = [a.property.name for a in AssociationProfile.objects.filter(we_do_financials=True).select_related('property')]
    made, notes = 0, []
    for filename, data in msg['pdfs']:
        try:
            parsed = extract_invoice(data, filename, names)
        except Exception:
            logger.warning('Could not read an invoice PDF', exc_info=True)
            notes.append('could not read a PDF')
            continue
        if not parsed.get('is_invoice'):
            notes.append('not an invoice')
            continue
        home = association or match_association(parsed.get('association'), msg['subject'])
        if home is None:
            seen.needs_home = True
            notes.append('association unknown')
            continue
        try:
            bill = draft_from(home, parsed, data, filename, message_id=msg['id'], user=user)
        except Exception:
            logger.warning('Could not make a draft bill', exc_info=True)
            notes.append('could not make a bill')
            continue
        if bill is None:
            notes.append('duplicate')
            continue
        seen.bill = bill
        made += 1
    seen.outcome = (f'{made} draft{"s" if made != 1 else ""}' + (f'; {", ".join(notes)}' if notes else ''))[:200]
    if made:
        seen.needs_home = False
    seen.save()
    return made


def run(user=None):
    """Check the mailbox once. Returns (drafts made, message). Never raises: a problem is reported in the message."""
    box = InvoiceInbox.get()
    if not box.enabled:
        return 0, 'The invoice inbox is switched off.'
    if not is_configured():
        return 0, 'The mailbox is not connected.'
    try:
        messages = fetch_new(box.label)
    except Exception:
        logger.warning('Could not read the invoice mailbox', exc_info=True)
        box.last_checked_at, box.last_result = timezone.now(), 'The mailbox could not be read.'
        box.save(update_fields=['last_checked_at', 'last_result'])
        return 0, box.last_result
    made = sum(process_message(m, user=user) for m in messages)
    box.last_checked_at, box.last_result = timezone.now(), f'{len(messages)} email{"s" if len(messages) != 1 else ""} read, {made} draft bill{"s" if made != 1 else ""} made.'
    box.save(update_fields=['last_checked_at', 'last_result'])
    return made, box.last_result
