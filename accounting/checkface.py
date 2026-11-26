"""The check face for the company's blank check stock: one letter sheet, the check on the top third and two stubs below it.

The stock (measured from a scan of it, in points from the top left of the sheet): the security-printed face runs from y 4.7 to 244.3 and from x 7.3 to 595.7, with a pre-printed banner along
its top edge (y 5 to 11) and a microprint band down its left edge (to x 30). The perforations that separate the stubs are at y 252 and y 504. A heat-sensitive red seal and a hologram sit in the
lower middle of the face (x 292 to 380, y 166 to 198): nothing is ever printed there. The face is otherwise BLANK: no labels, no bank, no MICR line, so every part is drawn here.

Everything printed on a check lives in this module so the weekly run, a reprint and the test print are the same page: `check_data(payment)` gathers what goes on it, `draw_check(canvas, data)`
draws it (the face, then the vendor stub, then the records stub). The bank numbers line along the bottom uses the E-13B MICR font (GnuMICR, static/fonts/gnumicr, GPL v2: its licence and
source sit beside it) when its files are there, and plain digits in Courier-Bold when they are not, so nothing breaks.
"""
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import stringWidth

from . import budget_project as bp
from . import checkprint

W, H = 612.0, 792.0                                                    # letter, in points
FACE_TOP, FACE_BOTTOM, FACE_LEFT, FACE_RIGHT = 4.7, 244.3, 7.3, 595.7
PERFORATIONS = (252.0, 504.0)                                          # where the sheet tears: the check, the vendor stub, the records stub
KEEP_OUT = (284.0, 164.0, 388.0, 206.0)                                # left, top, right, bottom of the seal and the hologram, with a margin: nothing prints in here
LEFT_X, RIGHT_X = 40.0, 578.0                                          # the text margins on the face
STUB_LEFT, STUB_RIGHT = 38.0, 574.0
STUBS = ((262.0, 498.0), (514.0, 786.0))                               # the vendor stub and the records stub: top and bottom
MICR_X, MICR_BASELINE = 26.0, 234.0                                    # the numbers line: 0.25 inch from the left of the face, its characters 0.25 inch above the bottom of the check
MICR_SIZE = 12.0                                                       # E-13B: 9 points wide per character (8 to the inch), 8.4 points tall
DEFAULT_DOWN_MM = 8.0                                                  # the printer lays everything on the sheet a little high, so the whole page is drawn this far lower (the guides are not: they mark the stock); each association can set its own
MAX_DOWN_MM = 30.0
SIGNATURE_BOX = (418.0, 152.0, 570.0, 186.0)                           # left, top, right, bottom: where the signature image goes, sitting on the signature line, clear of the seal
FONT_DIR = Path(settings.BASE_DIR) / 'static' / 'fonts' / 'gnumicr'
_micr_state = {}


# ---------------------------------------------------------------- the numbers on the bottom

def aba_ok(number):
    """A 9-digit routing number whose check digit is right (3-7-1 weights, a multiple of 10)."""
    d = [int(c) for c in number] if number.isdigit() and len(number) == 9 else None
    return bool(d) and (3 * (d[0] + d[3] + d[6]) + 7 * (d[1] + d[4] + d[7]) + (d[2] + d[5] + d[8])) % 10 == 0


def clean_routing(raw):
    """The routing number as 9 digits, or a ValueError that says what is wrong (a blank is allowed: it means none yet)."""
    digits = re.sub(r'\D', '', raw or '')
    if not digits:
        return ''
    if len(digits) != 9:
        raise ValueError(f'A routing number has 9 digits; this has {len(digits)}.')
    if not aba_ok(digits):
        raise ValueError('That routing number is not valid (its check digit is wrong). Look at it again on a check or the bank statement.')
    return digits


def micr_line(routing, account, number):
    """The bank numbers line in the E-13B key map: A is the transit symbol, C the on-us symbol. Standard business check order: C + the check number (6 digits) + C, then A + the 9-digit
    routing number + A, then the account number + C. '' when there is no routing or account number to print."""
    routing, account = re.sub(r'\D', '', routing or ''), re.sub(r'\D', '', account or '')
    digits = re.sub(r'\D', '', str(number or ''))
    if not routing or not account:
        return ''
    return f'C{digits.zfill(6)}C A{routing}A {account}C'


def micr_font():
    """(font name, size, is_micr): GnuMICR at 12 points when its files are in the project, else Courier-Bold."""
    if 'font' not in _micr_state:
        afm, pfb = FONT_DIR / 'GnuMICR.afm', FONT_DIR / 'GnuMICR.pfb'
        try:
            if 'GnuMICR' not in pdfmetrics.getRegisteredFontNames():
                if not (afm.exists() and pfb.exists()):
                    raise FileNotFoundError
                pdfmetrics.registerTypeFace(pdfmetrics.EmbeddedType1Face(str(afm), str(pfb)))
                pdfmetrics.registerFont(pdfmetrics.Font('GnuMICR', 'GnuMICR', 'WinAnsiEncoding'))
            _micr_state['font'] = ('GnuMICR', MICR_SIZE, True)
        except Exception:
            _micr_state['font'] = ('Courier-Bold', 11.0, False)
    return _micr_state['font']


def numbers_text(routing, account, number):
    """What the bottom line says: the MICR string with the font, plain 'check routing account' digits without it."""
    line = micr_line(routing, account, number)
    if not line or micr_font()[2]:
        return line
    return ' '.join(re.sub(r'\D', '', str(x or '')) for x in (str(number or '').zfill(6), routing, account))


# ---------------------------------------------------------------- what goes on a check

def date_text(day):
    return f'{day:%m/%d/%Y}'


def down_points(data):
    """How far the whole page is drawn below where it would be, in points, from the association's setting in millimetres (never negative, never more than 30 mm)."""
    try:
        mm = float(data.get('down_mm', DEFAULT_DOWN_MM))
    except (TypeError, ValueError):
        mm = DEFAULT_DOWN_MM
    return max(0.0, min(mm, MAX_DOWN_MM)) * 72.0 / 25.4


def _lines(text):
    return bp.address_lines(text or '')[:3]


def invoice_rows(payment):
    """The invoices a check pays, oldest first: number, date, description, the books account(s), the amount paid on this check."""
    rows = []
    for link in payment.bill_links.select_related('bill', 'bill__approved_by').order_by('bill__date', 'bill_id'):
        b = link.bill
        accounts = sorted({f'{x.account.code} {x.account.name}' for x in b.lines.select_related('account')})
        first_memo = next((x.memo for x in b.lines.all() if x.memo), '')
        rows.append({'number': b.number or '(no number)', 'date': date_text(b.date), 'description': (b.memo or first_memo or '').strip(), 'account': ', '.join(accounts), 'amount': link.amount, 'bill': b})
    return rows


def check_data(payment):
    """Everything printed on one check, from its payment, the bank account it is drawn on and the invoices it pays."""
    from . import ledger
    from types import SimpleNamespace
    assoc = payment.association
    bank = payment.bank_account or SimpleNamespace(payer_name='', payer_address='', bank_name='', bank_address='', routing_number='', account_number='', label='')
    profile = ledger.profile_for(payment.bank_account.association if payment.bank_account_id else assoc)          # the checkbook's owner: Proper Realty for a check from its trust account
    rows = invoice_rows(payment)
    memo = payment.memo or ', '.join(r['number'] for r in rows if r['number'] != '(no number)')
    return {
        'payer_name': (bank.payer_name or profile.legal_name or assoc.name).strip(), 'payer_lines': _lines(bank.payer_address),
        'bank_name': (bank.bank_name or '').strip(), 'bank_lines': _lines(bank.bank_address),
        'number': payment.number or '', 'date': date_text(payment.date), 'payee': payment.payee, 'amount': Decimal(payment.amount), 'memo': memo[:80],
        'micr': numbers_text(bank.routing_number, bank.account_number, payment.number), 'association': assoc.name, 'bank_label': bank.label,
        'invoices': rows, 'test': False, 'signature': signature_bytes(profile), 'down_mm': profile.check_down_mm,
    }


def sample_data(association=None):
    """A made-up check for lining the stock up: the real payer and bank names and addresses when the association has them, but obviously sample numbers and a sample payee."""
    from . import ledger
    from .models import BankAccount
    bank = BankAccount.objects.filter(association=association, is_active=True, kind='operating').first() if association is not None else None
    amount = Decimal('1234.56')
    return {
        'payer_name': (bank.payer_name if bank and bank.payer_name else (association.name if association else 'SAMPLE ASSOCIATION')), 'payer_lines': _lines(bank.payer_address) if bank else ['1 Sample Street', 'Sample City, FL 33000'],
        'bank_name': (bank.bank_name if bank and bank.bank_name else 'Sample Bank'), 'bank_lines': _lines(bank.bank_address) if bank else ['2 Bank Road', 'Sample City, FL 33000'],
        'number': '123', 'date': date_text(date.today()), 'payee': 'SAMPLE VENDOR, INC.', 'amount': amount, 'memo': 'Sample check, not real',
        'micr': numbers_text('000000000', '0000000000', '123'), 'association': association.name if association else 'Sample Association', 'bank_label': bank.label if bank else 'Operating',
        'invoices': [{'number': '1001', 'date': '10/03/2026', 'description': 'Sample invoice', 'account': '6906 Pest Control', 'amount': Decimal('734.56')},
                     {'number': '1002', 'date': '10/17/2026', 'description': 'Sample invoice', 'account': '6906 Pest Control', 'amount': Decimal('500.00')}],
        'test': True, 'signature': signature_bytes(ledger.profile_for(association)) if association is not None else None,
        'down_mm': ledger.profile_for(association).check_down_mm if association is not None else DEFAULT_DOWN_MM,
    }


# ---------------------------------------------------------------- drawing

def _fit(text, font, size, width, floor=6.0):
    while size > floor and stringWidth(text, font, size) > width:
        size -= 0.25
    return size


def _text(c, x, y, text, font='Helvetica', size=9.0, align='l', width=None, color=colors.black):
    """Text at x and a baseline y measured from the TOP of the sheet; made smaller to fit `width`, and cut with '...' when even the smallest size does not."""
    text = str(text or '')
    if width:
        size = _fit(text, font, size, width)
        while text and stringWidth(text, font, size) > width and len(text) > 1:
            text = text[:-2].rstrip() + '…' if not text.endswith('…') else text[:-2] + '…'
    c.setFillColor(color)
    c.setFont(font, size)
    {'l': c.drawString, 'r': c.drawRightString, 'c': c.drawCentredString}[align](x, H - y, text)
    return size


def _rule(c, x1, y, x2, width=0.5, color=colors.black):
    c.setStrokeColor(color)
    c.setLineWidth(width)
    c.line(x1, H - y, x2, H - y)


def _money(amount):
    return f'{Decimal(amount):,.2f}'


GREY = colors.HexColor('#555555')


def signature_bytes(profile):
    """The association's signature image as bytes, or None when there is none (or it cannot be read: a check then simply prints with the signature line empty)."""
    try:
        if profile is not None and profile.check_signature:
            with profile.check_signature.open('rb') as f:
                return f.read()
    except Exception:
        return None
    return None


def _draw_signature(c, data):
    """The signature image, as large as fits the box and sitting on the signature line. A damaged or unreadable image leaves the line empty rather than failing the check."""
    if not data:
        return
    try:
        import io
        from reportlab.lib.utils import ImageReader
        reader = ImageReader(io.BytesIO(data))
        iw, ih = reader.getSize()
        left, top, right, bottom = SIGNATURE_BOX
        scale = min((right - left) / iw, (bottom - top) / ih)
        w, h = iw * scale, ih * scale
        c.drawImage(reader, left + ((right - left) - w) / 2, H - bottom + 1.5, width=w, height=h, mask='auto')
    except Exception:
        return


def _draw_face(c, d):
    # the payer, top left
    _text(c, LEFT_X, 27, d['payer_name'], 'Helvetica-Bold', 9.5, width=232)
    for i, line in enumerate(d['payer_lines']):
        _text(c, LEFT_X, 37 + 9 * i, line, 'Helvetica', 7.5, width=232)
    # the bank, top middle
    _text(c, 382, 27, d['bank_name'], 'Helvetica-Bold', 9, 'c', width=170)
    for i, line in enumerate(d['bank_lines'][:2]):
        _text(c, 382, 36 + 8.5 * i, line, 'Helvetica', 7, 'c', width=170)
    # the check number, top right, and the date under it
    _text(c, RIGHT_X, 31, d['number'], 'Helvetica-Bold', 14, 'r', width=90)
    _text(c, RIGHT_X - 2, 66, d['date'], 'Helvetica', 10, 'r')
    _rule(c, 484, 69, RIGHT_X)
    _text(c, 484, 77, 'DATE', 'Helvetica', 6, color=GREY)
    # pay to the order of
    _text(c, LEFT_X, 93, 'PAY TO THE ORDER OF', 'Helvetica', 6.5, color=GREY)
    _text(c, LEFT_X + 2, 107, d['payee'], 'Helvetica-Bold', 11, width=400)
    _rule(c, LEFT_X, 110, 452)
    # the amount in numbers, boxed
    c.setStrokeColor(colors.black)
    c.setLineWidth(0.8)
    c.rect(462, H - 114, RIGHT_X - 462, 22)
    _text(c, 467, 107.5, '$', 'Helvetica-Bold', 11)
    _text(c, RIGHT_X - 4, 108, _money(d['amount']), 'Helvetica-Bold', 12, 'r', width=100)
    # the amount in words, filled out to DOLLARS with asterisks
    words = checkprint.amount_in_words(d['amount'])
    room = RIGHT_X - 8 - stringWidth('DOLLARS', 'Helvetica-Bold', 8) - LEFT_X - 2
    size = _fit(words, 'Helvetica', 9.5, room)
    text = words + ' '
    while stringWidth(text + '*', 'Helvetica', size) <= room:
        text += '*'
    _text(c, LEFT_X + 2, 140, text, 'Helvetica', size)
    _text(c, RIGHT_X, 140, 'DOLLARS', 'Helvetica-Bold', 8, 'r')
    _rule(c, LEFT_X, 143, RIGHT_X)
    # memo, bottom left; signature, bottom right (both clear of the seal and the hologram)
    _text(c, LEFT_X + 2, 186, d['memo'], 'Helvetica', 8.5, width=226)
    _rule(c, LEFT_X, 189, 270)
    _text(c, LEFT_X, 197, 'MEMO', 'Helvetica', 6, color=GREY)
    _draw_signature(c, d.get('signature'))
    _rule(c, 410, 186, RIGHT_X)
    _text(c, 494, 195, 'AUTHORIZED SIGNATURE', 'Helvetica', 6, 'c', color=GREY)
    # the bank numbers, in the clear band at the bottom
    if d['micr']:
        font, size, is_micr = micr_font()
        c.setFillColor(colors.black)
        c.setFont(font, size)
        c.drawString(MICR_X, H - MICR_BASELINE, d['micr'])


def _draw_stub(c, d, top, bottom, records):
    left, right = STUB_LEFT, STUB_RIGHT
    _text(c, left, top + 8, 'RECORDS COPY' if records else 'VENDOR COPY', 'Helvetica-Bold', 6.5, color=GREY)
    _text(c, left, top + 26, d['payee'], 'Helvetica-Bold', 11, width=340)
    _text(c, right, top + 26, f'Check No. {d["number"]}', 'Helvetica-Bold', 11, 'r')
    _text(c, left, top + 40, f'{d["association"]}  -  {d["bank_label"]}', 'Helvetica', 8, color=GREY, width=380)
    _text(c, right, top + 40, d['date'], 'Helvetica', 9, 'r')
    _text(c, left, top + 54, f'Memo: {d["memo"]}' if d['memo'] else '', 'Helvetica', 8, color=GREY, width=380)
    _text(c, right, top + 54, f'Total  ${_money(d["amount"])}', 'Helvetica-Bold', 10, 'r')
    _rule(c, left, top + 60, right, 0.6)
    head_y, first_y, step = top + 72, top + 84, 11.0
    limit = bottom - 14
    capacity = max(int((limit - first_y) // step) + 1, 1)
    rows = d['invoices']
    two_columns = len(rows) > capacity
    heads = ((left, 'INVOICE'), (left + 100, 'DATE'), (left + 170, 'DESCRIPTION'))
    if not two_columns:
        for x, label in heads:
            _text(c, x, head_y, label, 'Helvetica-Bold', 6, color=GREY)
        _text(c, right, head_y, 'AMOUNT', 'Helvetica-Bold', 6, 'r', color=GREY)
        for i, r in enumerate(rows):
            y = first_y + i * step
            _text(c, left, y, r['number'], 'Helvetica', 8, width=94)
            _text(c, left + 100, y, r['date'], 'Helvetica', 8)
            _text(c, left + 170, y, r['description'], 'Helvetica', 8, width=330 - 8)
            _text(c, right, y, _money(r['amount']), 'Helvetica', 8, 'r')
    else:                                                      # more invoices than one column holds: two compact columns (number, date, amount)
        per_column = capacity * 2
        shown = rows if len(rows) <= per_column else rows[:per_column - 1]
        col_w = (right - left - 16) / 2
        for col in (0, 1):
            x0 = left + col * (col_w + 16)
            _text(c, x0, head_y, 'INVOICE', 'Helvetica-Bold', 6, color=GREY)
            _text(c, x0 + 100, head_y, 'DATE', 'Helvetica-Bold', 6, color=GREY)
            _text(c, x0 + col_w, head_y, 'AMOUNT', 'Helvetica-Bold', 6, 'r', color=GREY)
        for i, r in enumerate(shown):
            col, row = divmod(i, capacity)
            x0, y = left + col * (col_w + 16), first_y + row * step
            _text(c, x0, y, r['number'], 'Helvetica', 8, width=94)
            _text(c, x0 + 100, y, r['date'], 'Helvetica', 8)
            _text(c, x0 + col_w, y, _money(r['amount']), 'Helvetica', 8, 'r')
        if len(rows) > per_column:
            col, row = divmod(per_column - 1, capacity)
            x0 = left + col * (col_w + 16)
            _text(c, x0, first_y + row * step, f'and {len(rows) - len(shown)} more invoices (see the payables list)', 'Helvetica-Oblique', 8, width=col_w)


def _draw_guides(c):
    """For the alignment test print: faint marks where the face, the perforations and the seal and hologram are, so the printed sheet can be held against the stock."""
    grey = colors.HexColor('#9aa3b2')
    c.saveState()
    c.setStrokeColor(grey)
    c.setLineWidth(0.4)
    for x in (FACE_LEFT, FACE_RIGHT):                          # corner marks of the face
        for y in (FACE_TOP, FACE_BOTTOM):
            sx, sy = (1 if x == FACE_LEFT else -1), (1 if y == FACE_TOP else -1)
            c.line(x, H - y, x + 12 * sx, H - y)
            c.line(x, H - y, x, H - y - 12 * sy)
    c.setDash(3, 3)
    for y in PERFORATIONS:
        c.line(0, H - y, W, H - y)
    left, top, right, bottom = KEEP_OUT
    c.rect(left, H - bottom, right - left, bottom - top)
    c.setDash()
    c.setFont('Helvetica', 5.5)
    c.setFillColor(grey)
    c.drawCentredString((left + right) / 2, H - top - 6, 'seal and hologram: nothing prints here')
    c.drawString(10, H - PERFORATIONS[0] - 8, 'tear here')
    c.drawString(10, H - PERFORATIONS[1] - 8, 'tear here')
    c.restoreState()


def draw_check(c, data, guides=False):
    """One sheet: the check face, the vendor stub and the records stub, on a letter page that is already set up on the canvas."""
    c.saveState()
    c.translate(0, -down_points(data))                                                # everything lower by the association's setting; the stock guides below stay where the stock is
    _draw_face(c, data)
    _draw_stub(c, data, STUBS[0][0], STUBS[0][1], records=False)
    _draw_stub(c, data, STUBS[1][0], STUBS[1][1], records=True)
    if data.get('test'):
        _text(c, LEFT_X, 484, 'TEST PAGE: print it on plain paper and hold it over a blank check. Nothing was paid.', 'Helvetica', 7, color=GREY)
    c.restoreState()
    if guides:
        _draw_guides(c)
