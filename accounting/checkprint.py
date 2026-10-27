"""Printing checks on blank check stock.

The person uploads a copy of the blank stock (a scan, a photo or a PDF) and Claude finds where the date, payee, amount, amount in words and memo go; the answer is kept as
fractions of the page, so nothing is typed in by hand. A test print on plain paper held over the stock shows whether it lines up, and a nudge moves everything a little.
The checks are printed from the browser, one per sheet, so there is no special software.
"""
import base64
import io
import logging
from decimal import Decimal

from django.conf import settings

logger = logging.getLogger(__name__)
MODEL = 'claude-sonnet-5'
FIELDS = ['date', 'payee', 'amount', 'words', 'memo']
STUB_FIELDS = ['stub_payee', 'stub_amount', 'stub_date', 'stub_memo']

# a common layout: the check on the top third, a stub below it, a second stub at the bottom (letter size, fractions of the page from the top left)
DEFAULT_LAYOUT = {
    'page_width_in': 8.5, 'page_height_in': 11.0, 'dx': 0.0, 'dy': 0.0, 'learned': False,
    'date': [0.76, 0.075], 'payee': [0.12, 0.125], 'amount': [0.79, 0.125], 'words': [0.10, 0.165], 'memo': [0.10, 0.225],
    'stub_payee': [0.10, 0.40], 'stub_amount': [0.70, 0.40], 'stub_date': [0.10, 0.37], 'stub_memo': [0.10, 0.43],
}

ONES = ['', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten', 'eleven', 'twelve', 'thirteen', 'fourteen', 'fifteen', 'sixteen', 'seventeen', 'eighteen', 'nineteen']
TENS = ['', '', 'twenty', 'thirty', 'forty', 'fifty', 'sixty', 'seventy', 'eighty', 'ninety']


class CheckPrintError(Exception):
    pass


def _below_thousand(n):
    words = []
    if n >= 100:
        words.append(ONES[n // 100] + ' hundred')
        n %= 100
    if n >= 20:
        words.append(TENS[n // 10] + ('-' + ONES[n % 10] if n % 10 else ''))
    elif n:
        words.append(ONES[n])
    return ' '.join(words)


def amount_in_words(amount):
    """1750.00 -> 'One thousand seven hundred fifty and 00/100'."""
    amount = Decimal(str(amount)).quantize(Decimal('0.01'))
    dollars, cents = int(amount), int((amount - int(amount)) * 100)
    parts, groups = [], [(10 ** 9, 'billion'), (10 ** 6, 'million'), (1000, 'thousand')]
    n = dollars
    for size, name in groups:
        if n >= size:
            parts.append(_below_thousand(n // size) + ' ' + name)
            n %= size
    if n or not parts:
        parts.append(_below_thousand(n) if n else 'zero')
    text = ' '.join(parts)
    return f'{text[0].upper()}{text[1:]} and {cents:02d}/100'


LAYOUT_TOOL = {
    'name': 'report_check_layout',
    'description': 'Report where each thing is printed on a check, as fractions of the page.',
    'input_schema': {'type': 'object', 'properties': {
        'page_width_in': {'type': 'number'}, 'page_height_in': {'type': 'number'},
        **{f: {'type': 'object', 'properties': {'x': {'type': 'number'}, 'y': {'type': 'number'}}, 'required': ['x', 'y'],
               'description': 'Where the text STARTS: x from the left edge and y from the top edge, each a fraction between 0 and 1 of the whole page.'} for f in FIELDS + STUB_FIELDS}},
        'required': ['page_width_in', 'page_height_in'] + FIELDS},
}


def _content(data, filename):
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    if ext == 'pdf':
        return {'type': 'document', 'source': {'type': 'base64', 'media_type': 'application/pdf', 'data': base64.b64encode(data).decode()}}
    media = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png', 'webp': 'image/webp', 'gif': 'image/gif'}.get(ext)
    if media is None:
        raise CheckPrintError('Upload the blank check as a PDF, a photo or a scan (PDF, JPG or PNG).')
    return {'type': 'image', 'source': {'type': 'base64', 'media_type': media, 'data': base64.b64encode(data).decode()}}


def _pdf_size(data):
    try:
        from pypdf import PdfReader
        box = PdfReader(io.BytesIO(data)).pages[0].mediabox
        return round(float(box.width) / 72, 2), round(float(box.height) / 72, 2)
    except Exception:
        return None


def clean_layout(raw, page=None):
    """Turn Claude's answer into a layout, with every position kept inside the page."""
    layout = dict(DEFAULT_LAYOUT)
    layout['learned'] = True
    width, height = (page or (raw.get('page_width_in'), raw.get('page_height_in')))
    layout['page_width_in'] = float(width) if width and 3 <= float(width) <= 14 else 8.5
    layout['page_height_in'] = float(height) if height and 2 <= float(height) <= 17 else 11.0
    for f in FIELDS + STUB_FIELDS:
        pos = raw.get(f)
        if isinstance(pos, dict) and 'x' in pos and 'y' in pos:
            layout[f] = [min(max(float(pos['x']), 0.0), 0.98), min(max(float(pos['y']), 0.0), 0.98)]
    return layout


def learn_layout(data, filename):
    if not settings.ANTHROPIC_API_KEY:
        raise CheckPrintError('Learning the layout needs the Claude connection, which is not set up. The standard layout is used until it is.')
    import anthropic
    from core.app_settings import sanitized_setting
    client = anthropic.Anthropic(api_key=sanitized_setting('ANTHROPIC_API_KEY'))
    try:
        message = client.messages.create(
            model=MODEL, max_tokens=1500, tools=[LAYOUT_TOOL], tool_choice={'type': 'tool', 'name': 'report_check_layout'},
            messages=[{'role': 'user', 'content': [_content(data, filename), {'type': 'text', 'text':
                'This is a blank sheet of check stock: a check, usually with one or two stubs. A printer will fill it in. Say where, as a fraction of the whole page from the top left, the text should START for: '
                'the date, the payee name, the dollar amount in numbers, the amount written in words, and the memo line, and for the stub(s) (payee, amount, date, memo) if there are any. '
                'Also give the page size in inches.'}]}])
    except CheckPrintError:
        raise
    except Exception:
        logger.exception('Claude could not read the check stock')
        raise CheckPrintError('The check stock could not be read right now. The standard layout is used until it can.')
    block = next((b for b in message.content if b.type == 'tool_use'), None)
    if block is None:
        raise CheckPrintError('Nothing could be read from that picture.')
    return clean_layout(block.input, _pdf_size(data) if filename.lower().endswith('.pdf') else None)


def nudge(layout, dx, dy):
    layout = dict(layout or DEFAULT_LAYOUT)
    layout['dx'] = round(float(layout.get('dx', 0)) + dx, 3)
    layout['dy'] = round(float(layout.get('dy', 0)) + dy, 3)
    return layout


def layout_for(profile):
    return profile.check_layout or dict(DEFAULT_LAYOUT)


def sample_check():
    return {'date': 'Oct 27, 2026', 'payee': 'Sample Vendor, Inc.', 'amount': '1,234.56', 'words': amount_in_words('1234.56'), 'memo': 'Sample check, not real',
            'stub_payee': 'Sample Vendor, Inc.', 'stub_amount': '1,234.56', 'stub_date': 'Oct 27, 2026', 'stub_memo': 'Invoice 1001'}


def check_fields(payment):
    memo = payment.memo or ', '.join(l.bill.number for l in payment.bill_links.select_related('bill') if l.bill.number)
    return {'date': f'{payment.date:%b} {payment.date.day}, {payment.date.year}', 'payee': payment.payee, 'amount': f'{payment.amount:,.2f}', 'words': amount_in_words(payment.amount),
            'memo': memo[:60], 'stub_payee': payment.payee, 'stub_amount': f'{payment.amount:,.2f}', 'stub_date': f'{payment.date:%b} {payment.date.day}, {payment.date.year}', 'stub_memo': memo[:60]}


def positions(layout):
    """CSS-ready positions in inches for each field, nudge included."""
    w, h = layout['page_width_in'], layout['page_height_in']
    out = {}
    for f in FIELDS + STUB_FIELDS:
        x, y = layout.get(f, DEFAULT_LAYOUT[f])
        out[f] = {'left': round(x * w + float(layout.get('dx', 0)), 3), 'top': round(y * h + float(layout.get('dy', 0)), 3)}
    return out
