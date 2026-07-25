import base64
import io
import logging

from django.conf import settings

from .models import Contact, Property

logger = logging.getLogger(__name__)

MODEL = 'claude-sonnet-5'

# Keeps a very large spreadsheet/text dump from blowing the context window — good enough for
# "a vendor list or tenant roster," not meant to swallow a multi-thousand-row export.
MAX_TEXT_CHARS = 60000

EXTRACT_CONTACTS_TOOL = {
    'name': 'extract_contacts',
    'description': 'Extract every contact (person or business) mentioned in a document, for a '
                    'property management company importing them into its contact list.',
    'input_schema': {
        'type': 'object',
        'properties': {
            'contacts': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {
                        'name': {
                            'type': 'string',
                            'description': "The person's full name, or the business name if there's no individual named.",
                        },
                        'phone': {'type': 'string', 'description': 'Phone number exactly as written. Empty string if none.'},
                        'email': {'type': 'string', 'description': 'Email address. Empty string if none.'},
                        'contact_type': {
                            'type': 'string',
                            'enum': [c[0] for c in Contact.ContactType.choices],
                            'description': (
                                'Best guess at what this contact is: vendor for an outside contractor/repair '
                                'company, owner/tenant/board_member/association_member/on_site_staff/guest if '
                                'the document clearly says so, other/staff_adjacent if genuinely unclear.'
                            ),
                        },
                        'trade': {
                            'type': 'string',
                            'description': 'Only if contact_type is vendor: their trade (plumbing, HVAC, '
                                            'cleaning, handyman, landscaping, etc). Empty string otherwise.',
                        },
                        'property_name': {
                            'type': ['string', 'null'],
                            'description': (
                                'Best-guess property from the provided list this contact belongs to — e.g. '
                                'the document is titled or grouped by one property/association, or a column '
                                'names it directly. Null if the document covers many properties with no way '
                                'to tell which one this specific contact belongs to, or names none at all.'
                            ),
                        },
                    },
                    'required': ['name', 'phone', 'email', 'contact_type', 'trade', 'property_name'],
                },
            },
        },
        'required': ['contacts'],
    },
}

PROMPT = """\
Extract every contact (person or business) mentioned in the attached document, for a property \
management company importing them into its contact list. This might be a vendor list, a board \
roster, a spreadsheet of tenants, a photo of a business card, or anything similar — pull out \
everyone who has at least a name and either a phone number or an email address. Skip anything \
that isn't actually a contact (page headers, notes, a bare address with no name attached).

Known properties: {property_names}\
"""


class DocumentImportError(Exception):
    """Raised for anything that keeps a document from being read at all —
    an unsupported file type, a Claude API failure, or a document Claude
    genuinely found no contacts in. The message is shown to the staff
    member directly, so it's written for them, not a log."""


def _file_to_content_blocks(data, content_type, filename):
    """Turns the uploaded file into Anthropic message content block(s).
    PDFs and images go straight through as native document/image blocks;
    everything else (spreadsheets, plain text) gets decoded to text here
    since Claude's document API only reads PDF natively."""
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''

    if content_type == 'application/pdf' or ext == 'pdf':
        return [{
            'type': 'document',
            'source': {'type': 'base64', 'media_type': 'application/pdf', 'data': base64.b64encode(data).decode()},
        }]

    image_exts = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png', 'gif': 'image/gif', 'webp': 'image/webp'}
    if (content_type or '').startswith('image/') or ext in image_exts:
        media_type = content_type if (content_type or '').startswith('image/') else image_exts[ext]
        return [{
            'type': 'image',
            'source': {'type': 'base64', 'media_type': media_type, 'data': base64.b64encode(data).decode()},
        }]

    if ext in ('xlsx', 'xlsm'):
        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
        lines = []
        for sheet in wb.worksheets:
            lines.append(f'--- Sheet: {sheet.title} ---')
            for row in sheet.iter_rows(values_only=True):
                if any(cell is not None for cell in row):
                    lines.append(','.join('' if cell is None else str(cell) for cell in row))
        text = '\n'.join(lines)[:MAX_TEXT_CHARS]
        return [{'type': 'text', 'text': f'--- Spreadsheet contents ---\n{text}'}]

    if ext in ('csv', 'txt') or content_type in ('text/csv', 'text/plain'):
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError:
            text = data.decode('latin-1', errors='replace')
        text = text[:MAX_TEXT_CHARS]
        return [{'type': 'text', 'text': f'--- Document contents ---\n{text}'}]

    raise DocumentImportError(
        f'Unsupported file type ("{filename}") — try a PDF, an image, a CSV, an Excel file, or plain text.'
    )


def extract_contacts_from_document(uploaded_file):
    """Sends an uploaded file to Claude and returns a list of extracted
    contact dicts (name/phone/email/contact_type/trade/property_id/
    property_name). Nothing is saved here — the caller shows these to the
    staff member to edit/exclude/accept before anything becomes a real
    Contact."""
    if not settings.ANTHROPIC_API_KEY:
        raise DocumentImportError('ANTHROPIC_API_KEY is not configured — document import is unavailable.')

    data = uploaded_file.read()
    content_blocks = _file_to_content_blocks(data, uploaded_file.content_type, uploaded_file.name)

    properties = list(Property.objects.filter(is_active=True).order_by('name'))
    property_names = [p.name for p in properties]
    properties_by_name = {p.name: p for p in properties}

    import anthropic

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    try:
        message = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            tools=[EXTRACT_CONTACTS_TOOL],
            tool_choice={'type': 'tool', 'name': 'extract_contacts'},
            messages=[{
                'role': 'user',
                'content': content_blocks + [{
                    'type': 'text',
                    'text': PROMPT.format(property_names=', '.join(property_names) or '(none configured)'),
                }],
            }],
        )
    except Exception:
        logger.exception('Claude API call failed during document contact import')
        raise DocumentImportError('Claude could not process this document — please try again.')

    tool_use = next((b for b in message.content if b.type == 'tool_use'), None)
    if tool_use is None:
        raise DocumentImportError("Claude didn't return any contacts for this document.")

    contacts = tool_use.input.get('contacts') or []
    if not contacts:
        raise DocumentImportError('No contacts were found in this document.')

    for c in contacts:
        prop = properties_by_name.get(c.get('property_name') or '')
        c['property_id'] = prop.pk if prop else None
        c['property_name'] = prop.name if prop else ''
    return contacts
