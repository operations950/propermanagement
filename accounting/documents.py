"""An association's governing documents as the research source for the association.

Documents are uploaded and listed with their status. Claude reads them to (1) propose settings, each with the page and the words it came from, which take effect
only when a person confirms them, and (2) answer questions, with the page cited. With no Claude connection set up, everything else still works.
"""
import logging
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.utils import timezone

from core.contact_document_import import _file_to_content_blocks

from . import ledger
from .models import AssociationDocument, DocumentQuestion, SettingProposal

logger = logging.getLogger(__name__)
MODEL = 'claude-sonnet-5'
MAX_DOC_BYTES = 12 * 1024 * 1024
WANTED = [AssociationDocument.Kind.DECLARATION, AssociationDocument.Kind.ARTICLES, AssociationDocument.Kind.BYLAWS, AssociationDocument.Kind.RULES,
          AssociationDocument.Kind.CONTRACT, AssociationDocument.Kind.BUDGET]


class DocumentError(Exception):
    pass


def checklist(association):
    """The documents we want on file, each with its status."""
    have = {}
    for d in AssociationDocument.objects.filter(property=association):
        have.setdefault(d.kind, d)
    rows = []
    for kind in WANTED:
        d = have.get(kind)
        required = kind in (AssociationDocument.Kind.CONTRACT, AssociationDocument.Kind.BUDGET)
        rows.append({'kind': kind, 'label': AssociationDocument.Kind(kind).label, 'doc': d, 'required': required})
    return rows


def has_contract(association):
    return AssociationDocument.objects.filter(property=association, kind=AssociationDocument.Kind.CONTRACT).exists()


def upload(association, kind, uploaded_file, user=None):
    if uploaded_file.size > MAX_DOC_BYTES:
        raise DocumentError('That file is too large (the limit is 12 MB).')
    name = uploaded_file.name.rsplit('.', 1)[0][:200]
    return AssociationDocument.objects.create(property=association, kind=kind, name=name, file=uploaded_file, uploaded_by=user)


def _blocks(doc):
    try:
        doc.file.open('rb')
        data = doc.file.read()
        doc.file.close()
    except Exception:
        raise DocumentError(f'The file for "{doc.name}" could not be opened.')
    return _file_to_content_blocks(data, '', doc.file.name.rsplit('/', 1)[-1])


def _client():
    if not settings.ANTHROPIC_API_KEY:
        raise DocumentError('Reading documents needs the Claude connection, which is not set up yet.')
    import anthropic
    from core.app_settings import sanitized_setting
    return anthropic.Anthropic(api_key=sanitized_setting('ANTHROPIC_API_KEY'))


def _labelled_blocks(docs):
    out = []
    for d in docs:
        out.append({'type': 'text', 'text': f'--- Document: {d.get_kind_display()} ({d.name}) ---'})
        out += _blocks(d)
    return out


# ---------------------------------------------------------------- ask

def ask(association, question, user=None):
    question = (question or '').strip()
    if not question:
        raise DocumentError('Type a question.')
    docs = list(AssociationDocument.objects.filter(property=association).order_by('kind'))
    if not docs:
        raise DocumentError('Upload a document first: there is nothing to read yet.')
    client = _client()
    try:
        message = client.messages.create(
            model=MODEL, max_tokens=1500,
            messages=[{'role': 'user', 'content': _labelled_blocks(docs) + [{'type': 'text', 'text':
                'You answer questions about one condominium or homeowners association using ONLY the documents above. Answer in plain words, briefly. '
                'After the answer give the source: the document name and the page number(s), and the exact words that support it. '
                'If the documents do not say, say so plainly and do not guess.\n\nQuestion: ' + question}]}])
    except Exception:
        logger.exception('Claude could not answer a question about the documents')
        raise DocumentError('The documents could not be read right now. Try again in a minute.')
    answer = ''.join(b.text for b in message.content if getattr(b, 'type', '') == 'text').strip()
    return DocumentQuestion.objects.create(property=association, question=question, answer=answer or 'No answer came back.', asked_by=user)


# ---------------------------------------------------------------- proposals

PROPOSE_TOOL = {
    'name': 'report_settings',
    'description': 'Report the settings found in the document, each with the page and the exact words.',
    'input_schema': {'type': 'object', 'properties': {'settings': {'type': 'array', 'items': {'type': 'object', 'properties': {
        'key': {'type': 'string', 'enum': ['late_fee', 'grace_days', 'interest_rate', 'spending_limit', 'management_fee']},
        'late_fee_kind': {'type': 'string', 'enum': ['flat', 'percent', 'greater'], 'description': 'For late_fee: a flat amount, a percent of the installment, or the greater of the two.'},
        'flat': {'type': ['number', 'null'], 'description': 'A dollar amount: the flat late fee, the spending limit, or the monthly management fee.'},
        'percent': {'type': ['number', 'null'], 'description': 'A percent: the late-fee percent, or the annual interest rate.'},
        'days': {'type': ['integer', 'null'], 'description': 'For grace_days: days after the due date before the fee applies.'},
        'page': {'type': ['integer', 'null']}, 'quote': {'type': 'string', 'description': 'The exact words in the document.'}},
        'required': ['key', 'quote']}}}, 'required': ['settings']},
}


def propose_from_document(doc, user=None):
    """Read one document for late fee, grace period, interest, spending limit and management fee. Saves proposals; nothing is applied."""
    client = _client()
    try:
        message = client.messages.create(
            model=MODEL, max_tokens=2500, tools=[PROPOSE_TOOL], tool_choice={'type': 'tool', 'name': 'report_settings'},
            messages=[{'role': 'user', 'content': _labelled_blocks([doc]) + [{'type': 'text', 'text':
                'From this document find, only if it clearly states them: the late fee on a late assessment (flat, percent, or the greater of the two), the grace period in days, the annual '
                'interest rate on unpaid assessments, the amount the manager may spend without board approval, and the monthly management fee. For each give the page and the exact words. '
                'Omit anything the document does not say.'}]}])
    except Exception:
        logger.exception('Claude could not read a document for settings')
        raise DocumentError('The document could not be read right now. Try again in a minute.')
    block = next((b for b in message.content if b.type == 'tool_use'), None)
    found = (block.input.get('settings') if block else None) or []
    SettingProposal.objects.filter(property=doc.property, document=doc, status=SettingProposal.Status.PROPOSED).delete()
    made = []
    for f in found:
        key = f.get('key')
        if key == 'late_fee':
            value = {'kind': f.get('late_fee_kind') or 'flat', 'flat': f.get('flat'), 'percent': f.get('percent')}
        elif key == 'grace_days':
            value = {'days': f.get('days')}
        elif key == 'interest_rate':
            value = {'percent': f.get('percent')}
        elif key in ('spending_limit', 'management_fee'):
            value = {'amount': f.get('flat')}
        else:
            continue
        if all(v in (None, '') for k, v in value.items() if k != 'kind'):
            continue
        made.append(SettingProposal.objects.create(property=doc.property, key=key, value=value, document=doc, page=f.get('page'), quote=(f.get('quote') or '')[:1000]))
    return made


def describe(proposal):
    v = proposal.value
    if proposal.key == 'late_fee':
        flat, pct, kind = v.get('flat'), v.get('percent'), v.get('kind')
        if kind == 'flat':
            return f'A late fee of ${Decimal(str(flat)):,.2f}'
        if kind == 'percent':
            return f'A late fee of {pct}% of the installment'
        return f'A late fee of ${Decimal(str(flat or 0)):,.2f} or {pct}% of the installment, whichever is greater'
    if proposal.key == 'grace_days':
        return f'{v.get("days")} days of grace after the due date'
    if proposal.key == 'interest_rate':
        return f'{v.get("percent")}% a year on unpaid assessments'
    if proposal.key == 'spending_limit':
        return f'The manager may spend up to ${Decimal(str(v.get("amount"))):,.2f} without the board'
    return f'A management fee of ${Decimal(str(v.get("amount"))):,.2f} a month'


LABELS = {'late_fee': 'Late fee', 'grace_days': 'Grace period', 'interest_rate': 'Interest on unpaid assessments', 'spending_limit': 'Spending limit without the board', 'management_fee': 'Management fee'}


def _dec(value):
    try:
        return Decimal(str(value)).quantize(Decimal('0.01'))
    except (InvalidOperation, TypeError):
        raise DocumentError('That value is not a number.')


def apply_setting(profile, key, value):
    """Put a confirmed setting on the association."""
    if key == 'late_fee':
        profile.late_fee_kind = value.get('kind') or 'flat'
        profile.late_fee_flat = _dec(value['flat']) if value.get('flat') not in (None, '') else None
        profile.late_fee_percent = Decimal(str(value['percent'])) if value.get('percent') not in (None, '') else None
        profile.late_fee_confirmed = True
    elif key == 'grace_days':
        profile.late_fee_grace_days = int(value['days'])
    elif key == 'interest_rate':
        profile.interest_rate = Decimal(str(value['percent']))
    elif key == 'spending_limit':
        profile.spending_limit = _dec(value['amount'])
    elif key == 'management_fee':
        profile.management_fee = _dec(value['amount'])
    else:
        raise DocumentError('Unknown setting.')
    profile.save()


def confirm(proposal, user=None, value=None):
    apply_setting(ledger.profile_for(proposal.property), proposal.key, value or proposal.value)
    if value:
        proposal.value = value
    proposal.status, proposal.decided_by, proposal.decided_at = SettingProposal.Status.CONFIRMED, user, timezone.now()
    proposal.save()


def dismiss(proposal, user=None):
    proposal.status, proposal.decided_by, proposal.decided_at = SettingProposal.Status.DISMISSED, user, timezone.now()
    proposal.save()
