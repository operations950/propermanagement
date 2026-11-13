"""The monthly financial package for one association: one PDF to hand to the board.

Order: cover, profit and loss, balance sheet, budget vs actual, owner aging, then the bank statements themselves (the PDFs the bank sent, operating accounts
first). Reconciliations are not part of it. The first four come from the same figures the Reports screens show (reporting.py, charges.py); a bank account
whose statement file for the month is not on file is named on the cover, so a gap is never silent.
"""
import io
from datetime import date
from decimal import Decimal

from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import charges, ledger, reporting
from .models import ZERO, BankAccount, BankStatement

INK, MUTED, LINE, TINT = '#1c2430', '#6b7280', '#c9ced6', '#eef2f7'
BASIS_LABEL = {'accrual': 'Accrual book', 'cash': 'Cash book'}


def file_name(prop, month):
    return f'{prop.name} - Financial package - {month:%Y-%m}.pdf'.replace('/', '-')


# ---------------------------------------------------------------- the bank statements

def bank_statements(prop, month):
    """[(bank account, BankStatement or None)] for the month, operating accounts first. A statement counts for the month its period ends in."""
    first, last = ledger.month_start(month), ledger.month_end(month)
    out = []
    for b in BankAccount.objects.filter(association=prop, is_active=True).order_by('kind', 'pk'):
        s = BankStatement.objects.filter(bank_account=b, period_end__gte=first, period_end__lte=last).order_by('-period_end').first()
        out.append((b, s))
    return out


def _statement_pages(statement):
    """The statement file's pages, or (None, why) when there is no readable PDF."""
    if statement is None or not statement.file:
        return None, 'statement file not on file'
    try:
        with statement.file.open('rb') as fh:
            data = fh.read()
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            return None, 'statement file is locked'
        return list(reader.pages), ''
    except Exception:
        return None, 'statement file could not be read as a PDF'


def _label(bank):
    return f'{bank.name}' + (f' ({bank.bank_name} {bank.last4})' if bank.last4 and bank.last4 not in bank.name else '')


# ---------------------------------------------------------------- drawing

def _styles():
    base = ParagraphStyle('b', fontName='Helvetica', fontSize=9, leading=12, textColor=colors.HexColor(INK))
    return {'base': base,
            'title': ParagraphStyle('t', parent=base, fontName='Helvetica-Bold', fontSize=16, leading=20),
            'sub': ParagraphStyle('s', parent=base, textColor=colors.HexColor(MUTED), fontSize=9),
            'note': ParagraphStyle('n', parent=base, textColor=colors.HexColor(MUTED), fontSize=8, leading=10.5),
            'cover_name': ParagraphStyle('cn', parent=base, fontName='Helvetica-Bold', fontSize=26, leading=32),
            'cover_title': ParagraphStyle('ct', parent=base, fontSize=15, leading=20, textColor=colors.HexColor(MUTED)),
            'h': ParagraphStyle('h', parent=base, fontName='Helvetica-Bold', fontSize=10, leading=14)}


def _doc(buf, size, footer):
    def on_page(canvas, doc):
        canvas.saveState()
        canvas.setFont('Helvetica', 7.5)
        canvas.setFillColor(colors.HexColor(MUTED))
        w = size[0]
        canvas.drawString(0.6 * inch, 0.4 * inch, footer)
        canvas.drawRightString(w - 0.6 * inch, 0.4 * inch, f'Page {doc.page}')
        canvas.restoreState()
    doc = SimpleDocTemplate(buf, pagesize=size, leftMargin=0.6 * inch, rightMargin=0.6 * inch, topMargin=0.6 * inch, bottomMargin=0.65 * inch)
    return doc, on_page


def _heading(prop, title, line2, st):
    return [Paragraph(title, st['title']), Paragraph(f'{prop.name} &middot; {line2}', st['sub']), Spacer(1, 10)]


def _statement_table(stmt, width, font=8.5):
    """One reporting.py statement (columns, blocks, finals) as a table: parentheses for negatives, a single line above totals and a double line under the last one."""
    cols = stmt['columns']
    n = len(cols)
    n_rows = 1 + sum(1 + sum((1 if b['show_group_titles'] else 0) + len(g['lines']) + (1 if g['show_total'] else 0) for g in b['groups']) + (1 if b['show_total'] else 0) for b in stmt['blocks']) + len(stmt['finals'])
    pad = 2.2 if n_rows <= 36 else 1.2        # a long statement is tightened so it stays on one page
    if n_rows > 36 and font > 8:
        font = 8
    label_w = width * (0.30 if n > 5 else 0.46)
    val_w = (width - label_w) / n
    hs = ParagraphStyle('hd', fontName='Helvetica-Bold', fontSize=font - 0.6, leading=font + 1.4, alignment=2, textColor=colors.HexColor(INK))
    head = [''] + [Paragraph(c['label'] + (f'<br/>{c["sub"]}' if c.get('sub') else ''), hs) for c in cols]
    rows, style = [head], [('FONTNAME', (0, 0), (-1, -1), 'Helvetica'), ('FONTSIZE', (0, 0), (-1, -1), font), ('TEXTCOLOR', (0, 0), (-1, -1), colors.HexColor(INK)),
                           ('ALIGN', (1, 0), (-1, -1), 'RIGHT'), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'), ('TOPPADDING', (0, 0), (-1, -1), pad), ('BOTTOMPADDING', (0, 0), (-1, -1), pad),
                           ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'), ('LINEBELOW', (0, 0), (-1, 0), 0.8, colors.HexColor(INK))]

    def add(label, values, bold=False, indent=0, top=False, double=False, color=None, tint=False):
        i = len(rows)
        rows.append([label] + [v['t'] for v in values])
        if bold:
            style.append(('FONTNAME', (0, i), (-1, i), 'Helvetica-Bold'))
        if indent:
            style.append(('LEFTPADDING', (0, i), (0, i), 6 + indent))
        if top:
            style.append(('LINEABOVE', (1, i), (-1, i), 0.6, colors.HexColor(INK)))
        if double:
            style.append(('LINEBELOW', (1, i), (-1, i), 1.6, colors.HexColor(INK)))     # a heavier closing line; the grand totals read as the last number on the page
        if color:
            style.append(('TEXTCOLOR', (0, i), (-1, i), colors.HexColor(color)))
        if tint:
            style.append(('BACKGROUND', (0, i), (-1, i), colors.HexColor(TINT)))

    def section(title):
        i = len(rows)
        rows.append([title] + [''] * n)
        style.extend([('FONTNAME', (0, i), (-1, i), 'Helvetica-Bold'), ('SPAN', (0, i), (-1, i)), ('TOPPADDING', (0, i), (-1, i), 8)])

    def subsection(title):
        i = len(rows)
        rows.append([title] + [''] * n)
        style.extend([('FONTNAME', (0, i), (-1, i), 'Helvetica-Bold'), ('SPAN', (0, i), (-1, i)), ('TEXTCOLOR', (0, i), (-1, i), colors.HexColor(MUTED)), ('LEFTPADDING', (0, i), (0, i), 6)])

    for b in stmt['blocks']:
        section(b['title'])
        for g in b['groups']:
            if b['show_group_titles']:
                subsection(g['title'])
            for l in g['lines']:
                add(f'{l["code"]}  {l["label"]}' if l.get('code') else l['label'], l['values'], indent=12 if b['show_group_titles'] else 6)
            if g['show_total']:
                add(g['total_label'], g['values'], bold=True, indent=6, top=True)
        if b['show_total']:
            add(b['total_label'], b['values'], bold=True, top=True)
    for f in stmt['finals']:
        add(f['label'], f['values'], bold=True, top=True, double=f['style'] != 'warn', color='#a5524c' if f['style'] == 'warn' else None)
    t = Table(rows, colWidths=[label_w] + [val_w] * n, repeatRows=1)
    t.setStyle(TableStyle(style))
    return t


def _without_column(stmt, i):
    """The statement with column `i` taken out (the package leaves out the screen's "% of budget")."""
    cut = lambda vals: [v for j, v in enumerate(vals) if j != i]
    blocks = []
    for b in stmt['blocks']:
        groups = [dict(g, values=cut(g['values']), lines=[dict(l, values=cut(l['values'])) for l in g['lines']]) for g in b['groups']]
        blocks.append(dict(b, groups=groups, values=cut(b['values'])))
    return dict(stmt, columns=cut(stmt['columns']), blocks=blocks, finals=[dict(f, values=cut(f['values'])) for f in stmt['finals']])


def _statement_pdf(prop, title, line2, stmt, size, font=8.5):
    st = _styles()
    buf = io.BytesIO()
    doc, on_page = _doc(buf, size, f'{prop.name}  |  {title}')
    story = _heading(prop, title, line2, st) + [_statement_table(stmt, size[0] - 1.2 * inch, font)]
    for note in stmt.get('notes', []):
        story += [Spacer(1, 6), Paragraph(note, st['note'])]
    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return buf.getvalue()


def _aging_pdf(prop, end, ag):
    st = _styles()
    buf = io.BytesIO()
    size = letter
    doc, on_page = _doc(buf, size, f'{prop.name}  |  Owner aging')
    money = lambda v: '' if not v else f'{v:,.2f}'
    head = ['Unit', 'Owner'] + list(ag['buckets']) + ['Owed']
    rows = [head] + [[r['unit'].label, r['owner'].name] + [money(v) for v in r['buckets']] + [f'{r["total"]:,.2f}'] for r in ag['rows']]
    if not ag['rows']:
        rows.append(['Nobody owes anything.'] + [''] * (len(head) - 1))
    rows.append(['Total', ''] + [f'{v:,.2f}' for v in ag['totals']] + [f'{ag["total"]:,.2f}'])
    width = size[0] - 1.2 * inch
    num = 0.95 * inch
    t = Table(rows, colWidths=[0.7 * inch, width - 0.7 * inch - num * (len(head) - 2)] + [num] * (len(head) - 2), repeatRows=1)
    last = len(rows) - 1
    t.setStyle(TableStyle([('FONTNAME', (0, 0), (-1, -1), 'Helvetica'), ('FONTSIZE', (0, 0), (-1, -1), 8.5), ('TEXTCOLOR', (0, 0), (-1, -1), colors.HexColor(INK)),
                           ('ALIGN', (2, 0), (-1, -1), 'RIGHT'), ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'), ('LINEBELOW', (0, 0), (-1, 0), 0.8, colors.HexColor(INK)),
                           ('TOPPADDING', (0, 0), (-1, -1), 2.2), ('BOTTOMPADDING', (0, 0), (-1, -1), 2.2),
                           ('FONTNAME', (0, last), (-1, last), 'Helvetica-Bold'), ('LINEABOVE', (0, last), (-1, last), 0.6, colors.HexColor(INK)), ('LINEBELOW', (0, last), (-1, last), 1.6, colors.HexColor(INK))]))
    story = _heading(prop, 'Owner aging', f'as of {end:%B} {end.day}, {end.year}', st) + [t,
             Spacer(1, 6), Paragraph('What each owner owed on that date, by how long it had been owed. Payments received after that date are not counted.', st['note'])]
    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return buf.getvalue()


def _cover_pdf(prop, month, basis, contents):
    st = _styles()
    buf = io.BytesIO()
    doc, on_page = _doc(buf, letter, f'{prop.name}  |  Financial package')
    rows = [[Paragraph(name, st['base']), Paragraph(note, st['note'])] for name, note in contents]
    t = Table(rows, colWidths=[3.4 * inch, 3.3 * inch])
    t.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LINEBELOW', (0, 0), (-1, -1), 0.4, colors.HexColor(LINE)), ('TOPPADDING', (0, 0), (-1, -1), 5), ('BOTTOMPADDING', (0, 0), (-1, -1), 5)]))
    story = [Spacer(1, 1.6 * inch), Paragraph(prop.name, st['cover_name']), Spacer(1, 6), Paragraph(f'Financial package for {month:%B %Y}', st['cover_title']),
             Spacer(1, 4), Paragraph(f'{BASIS_LABEL[basis]} &middot; as of {ledger.month_end(month):%B} {ledger.month_end(month).day}, {month.year}', st['sub']),
             Spacer(1, 0.6 * inch), Paragraph('Contents', st['h']), Spacer(1, 4), t, Spacer(1, 0.5 * inch),
             Paragraph(f'Prepared by Proper Realty, {date.today():%B} {date.today().day}, {date.today().year}', st['note'])]
    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return buf.getvalue()


# ---------------------------------------------------------------- the package

def build(prop, month, basis='accrual'):
    """The whole package as PDF bytes."""
    basis = basis if basis in BASIS_LABEL else 'accrual'
    month = ledger.month_start(month)
    end = ledger.month_end(month)
    asof = f'as of {end:%B} {end.day}, {end.year} &middot; {BASIS_LABEL[basis]}'
    bva = _without_column(reporting.budget_vs_actual(prop, end, basis), 6)                 # column 6 is "% of budget"
    parts = [('Profit and loss', _statement_pdf(prop, 'Profit and loss', f'{month:%B %Y} &middot; {BASIS_LABEL[basis]}', reporting.profit_and_loss(prop, end, basis, 'month_ytd'), letter)),
             ('Balance sheet', _statement_pdf(prop, 'Balance sheet', asof, reporting.balance_sheet(prop, end, basis, 'compare'), letter)),
             ('Budget vs actual', _statement_pdf(prop, 'Budget vs actual', f'through {end:%B} {end.day}, {end.year} &middot; {BASIS_LABEL[basis]}', bva, landscape(letter), 7.8)),
             ('Owner aging', _aging_pdf(prop, end, charges.aging(prop, end)))]
    contents, statement_pages = [], []
    for name, data in parts:
        contents.append((name, f'{len(PdfReader(io.BytesIO(data)).pages)} page(s)'))
    for bank, statement in bank_statements(prop, month):
        pages, why = _statement_pages(statement)
        if pages is None:
            contents.append((f'Bank statement: {_label(bank)}', f'Not included: {why}.'))
        else:
            contents.append((f'Bank statement: {_label(bank)}', f'{len(pages)} page(s), period ending {statement.period_end:%b} {statement.period_end.day}'))
            statement_pages.append(pages)
    writer = PdfWriter()
    for data in [_cover_pdf(prop, month, basis, contents)] + [d for _, d in parts]:
        for page in PdfReader(io.BytesIO(data)).pages:
            writer.add_page(page)
    for pages in statement_pages:
        for page in pages:
            writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
