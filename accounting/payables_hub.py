"""The central Payables page: one place, for every association we keep the books for, where vendor invoices are dropped in and become payables.

An invoice file is stored (PayableUpload), READ (vendor, address, invoice number, dates, total, which property it is for), and MATCHED (the property, the vendor, a suggested account). A person
then refines a few things on a review card and presses Create: the property must be one we can write checks for (an association whose books we keep and that has a bank account), the bank
account that will pay is named, a duplicate or an unbudgeted line is warned about (never blocked), and the payable goes down one of two channels: management approved or board approval.
Nothing here prints checks (accounting/check_runs.py does); a payable that is APPROVED and carries its bank account is what the check run picks up.

A future feeder (for example the shared inbox, picking up emails a person labels "for approval") only has to call `ingest()` with the attachment: it makes the same card.
"""
import base64
import difflib
import logging
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from core.models import Contact, Property

from . import invoice_inbox, ledger, payables
from . import standard_chart as sc
from .models import ZERO, Account, AssociationProfile, BankAccount, Bill, BillLine, Budget, PayableUpload, VendorProfile, VendorTerms

logger = logging.getLogger(__name__)

# The kinds of property a payable may be set up for: the ones whose books we keep (an association, a commercial building). It still needs we_do_financials and a bank account.
ELIGIBLE_TYPES = (Property.Type.ASSOCIATION, Property.Type.COMMERCIAL)
# ... and Proper Realty's own office books (ledger.is_office), which pay the company's own bills from its operating account.
ELIGIBLE_Q = Q(property_type__in=ELIGIBLE_TYPES) | Q(property_type=Property.Type.GENERAL, name__istartswith=ledger.OFFICE_NAME)

ALLOWED = {'pdf': 'application/pdf', 'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg'}
MAX_BYTES = 15 * 1024 * 1024
MODEL = invoice_inbox.MODEL


class IntakeError(Exception):
    pass


# ---------------------------------------------------------------- which properties a payable may be set up for

def eligibility(prop):
    """(code, message, fix_url): code 'ok' when a payable may be set up for this property, else why not and where to fix it."""
    if prop.property_type not in ELIGIBLE_TYPES and not ledger.is_office(prop):
        return 'type', 'Payables are only set up for associations and commercial buildings so far.', None
    profile = ledger.profile_for(prop)
    if not profile.we_do_financials:
        return 'books', f'We do not keep the books for {prop.name} yet.', reverse('accounting:settings', args=[prop.pk])
    if not BankAccount.objects.filter(association=prop, is_active=True).exists():
        return 'bank', f'{prop.name} has no bank account set up here, so we cannot write a check for it.', reverse('accounting:settings', args=[prop.pk]) + '#banks'
    return 'ok', '', None


def candidate_properties():
    """[(property, (code, message, fix_url))] for the picker: every active property of an eligible type, the ones we can pay for first."""
    props = Property.objects.filter(ELIGIBLE_Q, is_active=True).order_by('name')
    rows = [(p, eligibility(p)) for p in props if '(general)' not in p.name.lower()]
    return sorted(rows, key=lambda r: (r[1][0] != 'ok', r[0].name.lower()))


def picker_rows():
    """The property picker as dicts for the template: {'p', 'ok', 'msg', 'fix'}."""
    return [{'p': p, 'ok': e[0] == 'ok', 'msg': e[1], 'fix': e[2]} for p, e in candidate_properties()]


# ---------------------------------------------------------------- reading an invoice

INVOICE_TOOL = {
    'name': 'report_invoice',
    'description': 'Report what a vendor invoice says.',
    'input_schema': {'type': 'object', 'properties': {
        'is_invoice': {'type': 'boolean', 'description': 'True if this is a bill asking us to pay. False for a statement with nothing due, a receipt, or something else.'},
        'vendor': {'type': 'string', 'description': 'The company or person the invoice is FROM (who gets paid).'},
        'vendor_street': {'type': 'string'}, 'vendor_city': {'type': 'string'}, 'vendor_state': {'type': 'string', 'description': 'Two letters.'}, 'vendor_zip': {'type': 'string'},
        'vendor_phone': {'type': 'string'}, 'vendor_email': {'type': 'string'}, 'vendor_website': {'type': 'string'}, 'vendor_tax_id': {'type': 'string', 'description': 'EIN or tax id if printed.'},
        'invoice_number': {'type': 'string'},
        'invoice_date': {'type': ['string', 'null'], 'description': 'YYYY-MM-DD'}, 'due_date': {'type': ['string', 'null'], 'description': 'YYYY-MM-DD'},
        'total': {'type': 'number', 'description': 'The total amount due.'},
        'lines': {'type': 'array', 'items': {'type': 'object', 'properties': {'description': {'type': 'string'}, 'amount': {'type': 'number'}}}},
        'bill_to': {'type': 'string', 'description': 'The condominium or homeowners association, or the property, the invoice is billed to or the work is for. Empty if it does not say.'},
        'property_address': {'type': 'string', 'description': 'The street address of the property the work or bill is for, if shown.'},
        'unit': {'type': 'string', 'description': 'A unit or apartment number the work is for, if shown.'},
        'account_number': {'type': 'string', 'description': 'The customer or account number on the invoice, if shown.'},
        'description': {'type': 'string', 'description': 'One short line saying what the bill is for.'}},
        'required': ['is_invoice', 'vendor', 'total']},
}


def read_invoice(data, filename, association_names):
    """Ask Claude to read an invoice (a PDF or an image). Raises RuntimeError (with a short reason a person can read) when it cannot."""
    if not settings.ANTHROPIC_API_KEY:
        raise RuntimeError('The Claude connection is not set up, so the invoice could not be read: fill it in by hand.')
    import anthropic
    from core.app_settings import sanitized_setting
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else 'pdf'
    media = ALLOWED.get(ext, 'application/pdf')
    block = ({'type': 'document', 'source': {'type': 'base64', 'media_type': media, 'data': base64.b64encode(data).decode()}} if media == 'application/pdf'
             else {'type': 'image', 'source': {'type': 'base64', 'media_type': media, 'data': base64.b64encode(data).decode()}})
    client = anthropic.Anthropic(api_key=sanitized_setting('ANTHROPIC_API_KEY'))
    try:
        message = client.messages.create(
            model=MODEL, max_tokens=2000, tools=[INVOICE_TOOL], tool_choice={'type': 'tool', 'name': 'report_invoice'},
            messages=[{'role': 'user', 'content': [block, {'type': 'text', 'text': 'Read this vendor invoice for a property management company. The associations we manage: ' + '; '.join(association_names)
                                                         + '. Say which one it is billed to or for. Report the vendor exactly as printed, with its address, phone, email and tax id when shown.'}]}])
    except Exception:
        logger.warning('Could not read an invoice', exc_info=True)
        raise RuntimeError('The invoice could not be read right now: fill it in by hand, or try again.')
    found = next((b for b in message.content if b.type == 'tool_use'), None)
    if found is None:
        raise RuntimeError('Nothing could be read from that file: fill it in by hand.')
    return dict(found.input)


def _money(value):
    try:
        return Decimal(str(value)).quantize(Decimal('0.01'))
    except (InvalidOperation, TypeError, ValueError):
        return None


def clean_read(raw):
    """The reader's answer as plain strings and numbers we can keep as JSON."""
    raw = raw or {}
    text = lambda k: ' '.join(str(raw.get(k) or '').split())
    total = _money(raw.get('total'))
    lines = []
    for l in raw.get('lines') or []:
        amount = _money((l or {}).get('amount'))
        if amount is not None:
            lines.append({'description': ' '.join(str((l or {}).get('description') or '').split())[:200], 'amount': str(amount)})
    return {'is_invoice': bool(raw.get('is_invoice', True)), 'vendor': text('vendor')[:200], 'vendor_street': text('vendor_street')[:200], 'vendor_city': text('vendor_city')[:100],
            'vendor_state': text('vendor_state')[:2].upper(), 'vendor_zip': text('vendor_zip')[:10], 'vendor_phone': text('vendor_phone')[:30], 'vendor_email': text('vendor_email')[:120],
            'vendor_website': text('vendor_website')[:200], 'vendor_tax_id': text('vendor_tax_id')[:20], 'invoice_number': text('invoice_number')[:60],
            'invoice_date': text('invoice_date') or '', 'due_date': text('due_date') or '', 'total': str(total) if total is not None else '', 'lines': lines, 'bill_to': text('bill_to')[:200],
            'property_address': text('property_address')[:200], 'unit': text('unit')[:30], 'account_number': text('account_number')[:40], 'description': text('description')[:200]}


def association_names():
    return [p.name for p in Property.objects.filter(ELIGIBLE_Q, is_active=True) if '(general)' not in p.name.lower()]


def ingest(upload_file, user=None, source=PayableUpload.Source.UPLOAD):
    """Store one invoice file and read it. Always returns the PayableUpload (a card for it): when the file cannot be read the card says so and is filled in by hand."""
    name = getattr(upload_file, 'name', '') or 'invoice.pdf'
    ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    if ext not in ALLOWED:
        raise IntakeError(f'{name}: only PDF, PNG and JPG invoices can be added.')
    data = upload_file.read()
    if not data:
        raise IntakeError(f'{name} is empty.')
    if len(data) > MAX_BYTES:
        raise IntakeError(f'{name} is too big (the limit is 15 MB).')
    from django.core.files.base import ContentFile
    up = PayableUpload(filename=name[:200], source=source, uploaded_by=user)
    up.file.save(name, ContentFile(data), save=False)
    try:
        up.read = clean_read(read_invoice(data, name, association_names()))
    except RuntimeError as e:
        up.read, up.read_error = {}, str(e)[:200]
    except Exception:
        logger.warning('Invoice reading failed', exc_info=True)
        up.read, up.read_error = {}, 'The invoice could not be read: fill it in by hand.'
    if up.read and not up.read.get('is_invoice', True):
        up.read_error = 'This does not look like an invoice asking us to pay (a statement or a receipt?). Check it, or discard it.'
    up.save()
    return up


def adopt_inbox_drafts():
    """The draft bills the invoice inbox made show up as cards in To review too, so there is one queue. Returns how many were taken in."""
    taken = 0
    for bill in Bill.objects.filter(status=Bill.Status.DRAFT, source=Bill.Source.EMAIL).select_related('vendor', 'association').prefetch_related('lines'):
        if PayableUpload.objects.filter(bill=bill).exists():
            continue
        first = next(iter(bill.lines.all()), None)
        draft = {'property_id': bill.association_id, 'number': bill.number, 'date': bill.date.isoformat(), 'due': bill.due_date.isoformat() if bill.due_date else '', 'amount': str(bill.amount),
                 'memo': bill.memo or (first.memo if first else ''), 'account_id': first.account_id if first and first.account.code != sc.UNCATEGORIZED else None}
        if bill.vendor_id:
            draft['vendor_id'] = bill.vendor_id
        up = PayableUpload(filename=(bill.file.name.rsplit('/', 1)[-1] if bill.file else 'invoice from the inbox')[:200], source=PayableUpload.Source.INBOX, bill=bill,
                           read={'is_invoice': True, 'vendor': bill.payee, 'invoice_number': bill.number, 'invoice_date': bill.date.isoformat(), 'total': str(bill.amount), 'description': bill.memo}, draft=draft)
        if bill.file:
            up.file.name = bill.file.name
        up.save()
        taken += 1
    return taken


# ---------------------------------------------------------------- matching

def _norm(text):
    return invoice_inbox._norm(text)


def _domain(email):
    d = (email or '').split('@')[-1].lower().strip()
    return '' if d in ('gmail.com', 'yahoo.com', 'hotmail.com', 'outlook.com', 'aol.com', 'icloud.com', 'comcast.net', 'bellsouth.net', '') else d


def match_vendor(read):
    """(vendor Contact or None, how it was matched)."""
    name = _norm(read.get('vendor'))
    vendors = list(Contact.objects.filter(contact_type=Contact.ContactType.VENDOR))
    if name:
        for v in vendors:
            if _norm(v.name) == name:
                return v, 'the same name'
    tax = re.sub(r'\D', '', read.get('vendor_tax_id') or '')
    if tax:
        found = VendorProfile.objects.filter(contact__contact_type=Contact.ContactType.VENDOR).select_related('contact')
        for p in found:
            if re.sub(r'\D', '', p.tax_id) == tax:
                return p.contact, 'the same tax id'
    domain = _domain(read.get('vendor_email'))
    if domain:
        for v in vendors:
            if _domain(v.email) == domain:
                return v, 'the same email address'
    if len(name) >= 5:
        best, score = None, 0
        for v in vendors:
            other = _norm(v.name)
            if len(other) < 5:
                continue
            ratio = difflib.SequenceMatcher(None, name, other).ratio()
            if other in name or name in other:
                ratio = max(ratio, 0.92)
            if ratio > score:
                best, score = v, ratio
        if best is not None and score >= 0.9:
            return best, 'a very similar name'
    return None, ''


def match_property(read, vendor=None):
    """(property or None, how). The invoice says who it is billed to or where the work was; failing that, the one association this vendor has been paid by before."""
    hint = _norm(' '.join(x for x in (read.get('bill_to'), read.get('property_address')) if x))
    best, best_len = None, 0
    if hint:
        for prop in Property.objects.filter(ELIGIBLE_Q, is_active=True):
            if '(general)' in prop.name.lower():
                continue
            names = {_norm(prop.name)}
            try:
                names.add(_norm(ledger.profile_for(prop).legal_name))
            except Exception:
                pass
            for n in names:
                if n and (n in hint or (len(hint) >= 6 and hint in n)) and len(n) > best_len:
                    best, best_len = prop, len(n)
            street = _norm(prop.street)
            if street and street in hint and len(street) > best_len:
                best, best_len = prop, len(street)
    if best is not None:
        return best, 'the invoice says so'
    if vendor is not None:
        homes = list(VendorTerms.objects.filter(vendor=vendor).values_list('association_id', flat=True).distinct())
        if len(homes) == 1:
            prop = Property.objects.filter(ELIGIBLE_Q, pk=homes[0], is_active=True).first()
            if prop is not None:
                return prop, 'the only association this vendor has been paid by'
    return None, ''


def expense_accounts(prop):
    return list(Account.objects.filter(association=prop, kind='expense', is_active=True).order_by('code'))


def suggest_account(prop, vendor, read):
    """(Account or None, why): the vendor's usual account here, else what we last paid them for, else the best word match with what the invoice says."""
    if vendor is not None:
        terms = VendorTerms.objects.filter(association=prop, vendor=vendor).select_related('default_account').first()
        if terms and terms.default_account_id and terms.default_account.kind == 'expense':
            return terms.default_account, 'this vendor\'s usual account'
        last = BillLine.objects.filter(bill__association=prop, bill__vendor=vendor, account__kind='expense').exclude(bill__status=Bill.Status.VOID).exclude(account__code=sc.UNCATEGORIZED).order_by('-bill__date', '-pk').first()
        if last is not None:
            return last.account, 'what we last paid this vendor for'
    words = {w for w in re.findall(r'[a-z]{4,}', ' '.join([read.get('description') or '', read.get('vendor') or ''] + [l.get('description', '') for l in read.get('lines') or []]).lower())}
    best, hits = None, 0
    for a in expense_accounts(prop):
        if a.code == sc.UNCATEGORIZED:
            continue
        n = len(words & set(re.findall(r'[a-z]{4,}', a.name.lower())))
        if n > hits:
            best, hits = a, n
    if best is not None:
        return best, 'it matches what the invoice says'
    return None, ''


def budget_warning(prop, account, day):
    """A warning (never a block) when the association has a budget for the year and this account has nothing budgeted in it. An association with no budget loaded gets no warning at all."""
    if prop is None or account is None:
        return ''
    fy = ledger.profile_for(prop).fiscal_year_start(day)
    budget = Budget.objects.filter(association=prop, year=fy.year).first()
    if budget is None or not budget.lines.exists():
        return ''
    line = budget.lines.filter(account=account).first()
    if line is None or line.annual_amount <= 0:
        return f'This line is not budgeted for {fy.year}: {account.code} {account.name} has no money in the budget.'
    return ''


def banks_for(prop, reserve=False):
    """The bank accounts a check for this payable could be written from: the operating accounts, or the reserve accounts for reserve spending."""
    want = 'reserve' if reserve else 'operating'
    return [b for b in BankAccount.objects.filter(association=prop, is_active=True).order_by('pk') if b.kind == want]


def trust_banks_for(prop):
    """Proper Realty's own trust accounts (Property Management, General Escrow), offered on any other property's payable: the check is Proper's, the bill stays this property's. None for the
    office itself, whose own bills are never paid from trust money."""
    if ledger.is_office(prop):
        return []
    return list(BankAccount.objects.filter(kind=BankAccount.Kind.TRUST, is_active=True).exclude(association=prop).order_by('pk'))


def find_duplicates(prop, vendor, vendor_text, number, amount, exclude=None):
    """[(bill, reason)]: payables already entered for the same vendor with the same invoice number, or for the same amount."""
    if prop is None or (vendor is None and not vendor_text):
        return []
    qs = Bill.objects.filter(association=prop).exclude(status=Bill.Status.VOID)
    qs = qs.filter(vendor=vendor) if vendor is not None else qs.filter(vendor__isnull=True, vendor_text__iexact=vendor_text)
    if exclude is not None:
        qs = qs.exclude(pk=exclude.pk)
    found = []
    for b in qs.order_by('-date')[:50]:
        if number and b.number and _norm(b.number) == _norm(number):
            found.append((b, 'the same invoice number'))
        elif amount is not None and b.amount == amount:
            found.append((b, 'the same amount'))
    return found[:6]


def default_channel(prop, amount, reserve=False):
    """'board' or 'management': reserve spending and anything over the association's spending limit starts as board approval; no limit set means management."""
    if reserve:
        return 'board'
    limit = ledger.profile_for(prop).spending_limit
    if limit is None or amount is None:
        return 'management'
    return 'management' if amount <= limit else 'board'


# ---------------------------------------------------------------- the review card

def _parse_date(text):
    for fmt in ('%Y-%m-%d', '%m/%d/%Y'):
        try:
            return datetime.strptime((text or '').strip(), fmt).date()
        except ValueError:
            continue
    return None


def _amount(text):
    s = str(text or '').replace('$', '').replace(',', '').strip()
    return _money(s) if s else None


def build_card(upload):
    """Everything the review card shows for one waiting invoice: the choices (the person's, else what was matched), the flags, and whether Create is possible."""
    read, d = upload.read or {}, upload.draft or {}
    flags, blocks = [], []
    # vendor
    vendor, vendor_how = None, ''
    if d.get('vendor_id'):
        vendor = Contact.objects.filter(pk=d['vendor_id'], contact_type=Contact.ContactType.VENDOR).first()
        vendor_how = 'you chose it'
    if vendor is None and d.get('vendor_text'):
        vendor = Contact.objects.filter(name__iexact=d['vendor_text'].strip(), contact_type=Contact.ContactType.VENDOR).first()
        vendor_how = 'you chose it' if vendor else ''
    if vendor is None and not d.get('vendor_none'):
        vendor, vendor_how = match_vendor(read)
    # property
    prop, prop_how = None, ''
    if d.get('property_id'):
        prop = Property.objects.filter(ELIGIBLE_Q, pk=d['property_id']).first()
        prop_how = 'you chose it'
    if prop is None:
        prop, prop_how = match_property(read, vendor)
    elig = eligibility(prop) if prop is not None else None
    if prop is None:
        flags.append(('warn', 'Which property is this for? The invoice does not say, or it is not one of ours.'))
        blocks.append('Pick the property.')
    elif elig[0] != 'ok':
        flags.append(('bad', elig[1]))
        blocks.append(elig[1])
    # values
    number = d.get('number') if 'number' in d else (read.get('invoice_number') or '')
    day_text = d.get('date') if 'date' in d else (read.get('invoice_date') or '')
    due_text = d.get('due') if 'due' in d else (read.get('due_date') or '')
    amount_text = d.get('amount') if 'amount' in d else (read.get('total') or '')
    memo = d.get('memo') if 'memo' in d else (read.get('description') or '')
    day = _parse_date(day_text) or timezone.localdate()
    amount = _amount(amount_text)
    reserve = bool(d.get('reserve'))
    # account
    account, account_why, accounts = None, '', []
    if prop is not None and elig and elig[0] == 'ok':
        accounts = expense_accounts(prop)
        if d.get('account_id'):
            account = next((a for a in accounts if a.pk == int(d['account_id'])), None)
            account_why = 'you chose it' if account else ''
        if account is None:
            account, account_why = suggest_account(prop, vendor, read)
        if account is None:
            flags.append(('warn', 'Pick the account this goes against in the books.'))
    warn_budget = budget_warning(prop, account, day) if (prop is not None and elig and elig[0] == 'ok') else ''
    # bank account
    bank_state = {'options': [], 'selected': None, 'only': False, 'needs_confirm': False, 'confirmed': False, 'message': '', 'fix_url': None}
    if prop is not None and elig and elig[0] == 'ok':
        options = banks_for(prop, reserve)
        trust = [] if reserve else trust_banks_for(prop)              # a trust account can be picked for an ordinary (not reserve) payable
        bank_state['options'] = options + trust
        if not options:
            kind = 'reserve' if reserve else 'operating'
            bank_state['message'] = f'{prop.name} has no active {kind} bank account to write this check from.'
            bank_state['fix_url'] = reverse('accounting:settings', args=[prop.pk]) + '#banks'
            blocks.append(bank_state['message'])
        else:
            chosen = next((b for b in options + trust if str(b.pk) == str(d.get('bank_id'))), None)
            bank_state['only'] = len(options) + len(trust) == 1
            bank_state['selected'] = chosen or options[0]
            if len(options) > 1 and not (chosen is not None and chosen in trust):
                bank_state['needs_confirm'] = True
                bank_state['confirmed'] = bool(d.get('bank_confirmed')) and chosen is not None
                if not bank_state['confirmed']:
                    blocks.append('This property has more than one bank account: confirm which one pays.')
    # duplicates
    vendor_text = (d.get('vendor_text') or read.get('vendor') or '').strip() if vendor is None else ''
    dups = find_duplicates(prop, vendor, vendor_text, number, amount, exclude=upload.bill) if (prop is not None and elig and elig[0] == 'ok') else []
    dup_ok = bool(d.get('dup_ok'))
    if dups and not dup_ok:
        blocks.append('This looks like a payable we already have: tick "Create it anyway" or discard.')
    # approval channel
    default = default_channel(prop, amount, reserve) if prop is not None else 'management'
    channel = 'board' if reserve else (d.get('channel') if d.get('channel') in ('management', 'board') else default)
    limit = ledger.profile_for(prop).spending_limit if prop is not None and elig and elig[0] == 'ok' else None
    # what is still missing
    if vendor is None:
        blocks.append('Create the vendor (one click) or pick an existing one.' if (d.get('vendor_text') or read.get('vendor')) else 'Say who the invoice is from.')
    if amount is None or amount <= 0:
        blocks.append('Type the amount.')
    new_vendor = None
    if vendor is None and (read.get('vendor') or d.get('vendor_text')):
        new_vendor = {'name': (d.get('vendor_text') or read.get('vendor') or '').strip(), 'street': read.get('vendor_street', ''), 'city': read.get('vendor_city', ''), 'state': read.get('vendor_state', ''),
                      'zip': read.get('vendor_zip', ''), 'phone': read.get('vendor_phone', ''), 'email': read.get('vendor_email', ''), 'tax_id': read.get('vendor_tax_id', '')}
    return {'upload': upload, 'read': read, 'read_error': upload.read_error, 'prop': prop, 'prop_how': prop_how, 'elig': elig, 'vendor': vendor, 'vendor_how': vendor_how, 'new_vendor': new_vendor,
            'vendor_text': d.get('vendor_text') or '', 'number': number, 'date': day_text or day.isoformat(), 'due': due_text, 'amount': amount_text, 'memo': memo, 'account': account, 'account_why': account_why,
            'accounts': accounts, 'budget_warning': warn_budget, 'bank': bank_state, 'reserve': reserve, 'duplicates': dups, 'dup_ok': dup_ok, 'channel': channel, 'default_channel': default,
            'limit': limit, 'flags': flags, 'blocks': blocks, 'can_create': not blocks, 'is_1099': bool(d.get('is_1099')), 'file_url': upload.file.url if upload.file else ''}


DRAFT_KEYS = ('property_id', 'vendor_id', 'vendor_text', 'number', 'date', 'due', 'amount', 'memo', 'account_id', 'bank_id', 'channel', 'is_1099')


def save_choices(upload, post):
    """Keep what the person changed on the card. Only the fields that were sent are touched, so a refresh never loses another choice."""
    d = dict(upload.draft or {})
    for key in DRAFT_KEYS:
        if key in post:
            value = post.get(key)
            if key in ('property_id', 'vendor_id', 'account_id', 'bank_id'):
                value = int(value) if str(value).isdigit() else None
            d[key] = value
    for key in ('reserve', 'bank_confirmed', 'dup_ok', 'is_1099'):                  # tick boxes: only meaningful when the card's whole form was sent
        if post.get('_card'):
            d[key] = bool(post.get(key))
    prev = post.get('prev_property_id')
    if prev is not None and 'property_id' in post and str(post.get('property_id')) != str(prev):
        for stale in ('account_id', 'bank_id', 'bank_confirmed'):                    # another property: its accounts and bank accounts are different
            d.pop(stale, None)
    if 'vendor_text' in post:
        if (post.get('vendor_text') or '').strip():
            d['vendor_none'] = True                                                  # a name the person typed: do not quietly swap in the reader's match
            if 'vendor_id' not in post:
                d.pop('vendor_id', None)
        else:
            d.pop('vendor_text', None)
            d.pop('vendor_none', None)
    upload.draft = d
    upload.save(update_fields=['draft'])
    return d


# ---------------------------------------------------------------- vendors

def clean_phone(text):
    digits = re.sub(r'\D', '', text or '')
    if len(digits) == 11 and digits.startswith('1'):
        digits = digits[1:]
    return f'{digits[:3]}-{digits[3:6]}-{digits[6:]}' if len(digits) == 10 else ''


def clean_email(text):
    try:
        validate_email((text or '').strip())
        return text.strip()
    except ValidationError:
        return ''


def vendor_address_lines(contact):
    """The mailing address for an envelope: [] when we do not have one."""
    profile = VendorProfile.objects.filter(contact=contact).first()
    return profile.address_lines if profile else []


@transaction.atomic
def create_vendor(upload, prop=None, is_1099=False):
    """One click: make the vendor Contact (and its address and tax id) from what the invoice says. An existing vendor with the same name is used instead of a twin."""
    card_read = upload.read or {}
    d = upload.draft or {}
    name = ' '.join((d.get('vendor_text') or card_read.get('vendor') or '').split())
    if not name:
        raise payables.PayableError('The invoice does not say who it is from: type the vendor name first.')
    existing = Contact.objects.filter(name__iexact=name, contact_type=Contact.ContactType.VENDOR).first()
    if existing is not None:
        vendor = existing
    else:
        vendor = Contact.objects.create(name=name[:200], contact_type=Contact.ContactType.VENDOR, phone=clean_phone(card_read.get('vendor_phone')), email=clean_email(card_read.get('vendor_email')),
                                        source=Contact.Source.DOCUMENT, notes='Made from an invoice on the Payables page.')
    profile, _ = VendorProfile.objects.get_or_create(contact=vendor)
    for field, key in (('street', 'vendor_street'), ('city', 'vendor_city'), ('state', 'vendor_state'), ('zip_code', 'vendor_zip'), ('tax_id', 'vendor_tax_id'), ('website', 'vendor_website')):
        if not getattr(profile, field) and card_read.get(key):
            value = str(card_read[key]).strip()
            if field == 'street':
                value = ', '.join(p.strip() for p in value.replace('|', ',').split(',') if p.strip())              # "1330 West Industrial Avenue | #107" is one street line: "1330 West Industrial Avenue, #107"
            setattr(profile, field, value)
    profile.is_1099 = profile.is_1099 or bool(is_1099)
    profile.save()
    if prop is not None:
        terms, made = VendorTerms.objects.get_or_create(association=prop, vendor=vendor, defaults={'pay_method': VendorTerms.Pay.CHECK, 'is_1099': profile.is_1099})
    d['vendor_id'] = vendor.pk
    d.pop('vendor_none', None)
    upload.draft = d
    upload.save(update_fields=['draft'])
    return vendor


# ---------------------------------------------------------------- making the payable

@transaction.atomic
def create_payable(upload, user):
    """Turn a card into a payable and send it down its channel. Raises payables.PayableError with a plain message if something is missing."""
    card = build_card(upload)
    if not card['can_create']:
        raise payables.PayableError(card['blocks'][0])
    prop, account, bank = card['prop'], card['account'], card['bank']['selected']
    if account is None:
        raise payables.PayableError('Pick the account this goes against in the books.')
    vendor = card['vendor']
    vendor_text = '' if vendor is not None else (card['vendor_text'] or card['read'].get('vendor') or '').strip()
    amount = _amount(card['amount'])
    day = _parse_date(card['date']) or timezone.localdate()
    due = _parse_date(card['due'])
    kind = Bill.Kind.RESERVE if card['reserve'] else Bill.Kind.EXCEPTION
    memo = (card['memo'] or '').strip()
    channel = card['channel']
    if upload.bill_id and upload.bill.status == Bill.Status.DRAFT:                  # an inbox draft: fix the draft itself instead of making a second bill
        bill = upload.bill
        bill.association, bill.vendor, bill.vendor_text, bill.number, bill.date, bill.due_date, bill.amount = prop, vendor, vendor_text[:200], (card['number'] or '')[:60], day, due, amount
        bill.kind, bill.reserve, bill.memo = kind, card['reserve'], memo[:300]
        bill.lines.all().delete()
        BillLine.objects.create(bill=bill, account=account, amount=amount, memo=memo[:200])
    else:
        bill = payables.create_bill(prop, day, [(account, amount, memo)], vendor=vendor, vendor_text=vendor_text, number=(card['number'] or ''), due=due, kind=kind, reserve=card['reserve'],
                                    source=Bill.Source.UPLOAD, user=user, memo=memo)
        if upload.file:
            bill.file.name = upload.file.name
    bill.bank_account, bill.approval_channel, bill.invoice_read, bill.created_by = bank, channel, upload.read or {}, user
    bill.save()
    if vendor is not None:                                                         # remember the vendor's usual account here
        terms, _ = VendorTerms.objects.get_or_create(association=prop, vendor=vendor, defaults={'pay_method': VendorTerms.Pay.CHECK,
                                                                                                'is_1099': bool(VendorProfile.objects.filter(contact=vendor, is_1099=True).exists())})
        if not terms.default_account_id:
            terms.default_account = account
            terms.save(update_fields=['default_account'])
    payables.approve_bill(bill, user)                                              # management: approved and posted; board: waiting for the board
    upload.status, upload.bill, upload.decided_at = PayableUpload.Status.CREATED, bill, timezone.now()
    upload.save(update_fields=['status', 'bill', 'decided_at'])
    return bill


def discard(upload):
    upload.status, upload.decided_at = PayableUpload.Status.DISCARDED, timezone.now()
    upload.save(update_fields=['status', 'decided_at'])
    if upload.bill_id and upload.bill.status == Bill.Status.DRAFT:                 # an inbox draft that is not wanted
        payables.void_bill(upload.bill)


# ---------------------------------------------------------------- the other tabs

def board_approve(bill, user, by, on=None, bank=None):
    """The board's okay is in: the payable becomes approved (and is posted). It must carry the bank account that will pay it."""
    by = (by or '').strip()
    if not by:
        raise payables.PayableError('Say who on the board approved it.')
    if bank is not None:
        bill.bank_account = bank
    if bill.bank_account_id is None:
        options = banks_for(bill.association, bill.reserve)
        if len(options) == 1:
            bill.bank_account = options[0]
        else:
            raise payables.PayableError('Choose the bank account this check will be written from.')
    bill.save(update_fields=['bank_account'])
    return payables.approve_bill(bill, user, board_by=by, board_on=on)


def set_bank(bill, bank):
    own = bank.association_id == bill.association_id
    if not bank.is_active or (bank.kind == BankAccount.Kind.TRUST and ledger.is_office(bill.association)) or not (own or bank.kind == BankAccount.Kind.TRUST):
        raise payables.PayableError('That bank account does not belong to this association.')
    bill.bank_account = bank
    bill.save(update_fields=['bank_account'])


def waiting_for_board():
    return list(Bill.objects.filter(status=Bill.Status.AWAITING_BOARD).select_related('vendor', 'association', 'bank_account').prefetch_related('lines__account').order_by('association__name', 'date'))


def ready_groups():
    """The approved payables still owed, grouped by association and then bank account: [{assoc, total, banks: [{bank, bills, total}]}]."""
    bills = [b for b in Bill.objects.filter(status=Bill.Status.APPROVED).select_related('vendor', 'association', 'bank_account').prefetch_related('payments') if b.balance > 0]
    out = {}
    for b in sorted(bills, key=lambda b: (b.association.name.lower(), b.bank_account_id or 0, b.due_date or b.date)):
        a = out.setdefault(b.association_id, {'assoc': b.association, 'total': ZERO, 'banks': {}})
        g = a['banks'].setdefault(b.bank_account_id, {'bank': b.bank_account, 'bills': [], 'total': ZERO, 'options': banks_for(b.association, b.reserve)})
        g['bills'].append(b)
        g['total'] += b.balance
        a['total'] += b.balance
    return [{'assoc': a['assoc'], 'total': a['total'], 'banks': list(a['banks'].values())} for a in out.values()]


def recently_paid(limit=30):
    return list(Bill.objects.filter(status=Bill.Status.PAID).select_related('vendor', 'association', 'bank_account').order_by('-date', '-pk')[:limit])


def tab_counts():
    ready = Bill.objects.filter(status=Bill.Status.APPROVED)
    waiting = Bill.objects.filter(status=Bill.Status.AWAITING_BOARD)
    return {'review': PayableUpload.objects.filter(status=PayableUpload.Status.PENDING).count(), 'board': waiting.count(), 'ready': ready.count(),
            'board_total': sum((b.amount for b in waiting), ZERO), 'ready_total': sum((b.balance for b in ready.prefetch_related('payments')), ZERO)}
