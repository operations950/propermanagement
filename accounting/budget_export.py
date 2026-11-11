"""The budget as a clean sheet to hand to the board: a PDF to print and an Excel file to edit. Both come from the same figures the Board sheet screen shows (views_budget._sheet).
Negative changes are in parentheses; a change that is good for the owners is green and one that is bad is red; the new year's column is tinted and boxed."""
import io
import re
from datetime import date

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

INK = '#1c2430'
GOOD, BAD = '#17803d', '#b42318'
TINT = '#e4edfb'
NOTE = 'Lines of $1 or less in both budgets are left off; the totals include them.'


def file_name(assoc_name, year, stage_label, ext):
    base = re.sub(r'[^A-Za-z0-9 ._()-]+', '', f'{assoc_name} {year} budget ({stage_label})').strip()
    return f'{base}.{ext}'


def _d(v):
    return f'-${abs(round(v)):,}' if round(v) < 0 else f'${round(v):,}'


def _chg(v):
    """A change in dollars: $1,234, ($1,234), or a dash."""
    r = round(v)
    return '—' if r == 0 else f'${r:,}' if r > 0 else f'(${abs(r):,})'


def _pc(p):
    """A change in percent: 3.1%, (3.1%), new when there was nothing before, or a dash."""
    if p is None:
        return 'new'
    return '—' if abs(p) < 0.05 else f'{p:.1f}%' if p > 0 else f'({abs(p):.1f}%)'


def _c2(v):
    return f'${v:,.2f}'


def _c2chg(v):
    return '—' if abs(v) < 0.005 else f'${v:,.2f}' if v > 0 else f'(${abs(v):,.2f})'


def _hex(tone):
    return {'good': GOOD, 'bad': BAD}.get(tone)


def heads(sheet):
    return ['', f'{sheet["year"] - 1} budget', f'{sheet["year"] - 1} {sheet["proj_word"]}', f'{sheet["year"]} budget', '$ change', '% change']


def pdf(sheet):
    """The sheet as a PDF (letter, portrait). `sheet` is views_budget._sheet's dict with assoc name, year, stage label, unit count and date added."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.6 * inch, rightMargin=0.6 * inch, topMargin=0.6 * inch, bottomMargin=0.65 * inch,
                            title=f'{sheet["assoc"]} {sheet["year"]} budget', author='Proper Realty')
    title = ParagraphStyle('t', fontName='Helvetica-Bold', fontSize=17, leading=21, textColor=colors.HexColor(INK))
    sub = ParagraphStyle('s', fontName='Helvetica', fontSize=9.5, leading=13, textColor=colors.HexColor('#667085'))
    story = [Paragraph(sheet['assoc'], title), Paragraph(f'{sheet["year"]} proposed budget &middot; {sheet["stage_label"]} &middot; {sheet["units"]} unit{"s" if sheet["units"] != 1 else ""}', sub), Spacer(1, 10)]
    rows, style = [heads(sheet)], [('FONT', (0, 0), (-1, 0), 'Helvetica-Bold', 8), ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor(INK)), ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
                                  ('LINEBELOW', (0, 0), (-1, 0), 1, colors.HexColor(INK)), ('FONT', (0, 1), (-1, -1), 'Helvetica', 8.5), ('TOPPADDING', (0, 0), (-1, -1), 2.6),
                                  ('BOTTOMPADDING', (0, 0), (-1, -1), 2.6), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE')]

    def add(cells, kind='line', tone='', ptone=''):
        rows.append(cells)
        i = len(rows) - 1
        if kind == 'section':
            style.extend([('FONT', (0, i), (-1, i), 'Helvetica-Bold', 8.5), ('TOPPADDING', (0, i), (-1, i), 9), ('LINEBELOW', (0, i), (-1, i), 0.5, colors.HexColor('#c9d2e0'))])
        elif kind == 'total':
            style.extend([('FONT', (0, i), (-1, i), 'Helvetica-Bold', 8.5), ('LINEABOVE', (0, i), (-1, i), 0.5, colors.HexColor('#98a2b3'))])
        elif kind == 'grand':
            style.extend([('FONT', (0, i), (-1, i), 'Helvetica-Bold', 9.5), ('BACKGROUND', (0, i), (-1, i), colors.HexColor('#eef2f7')), ('LINEABOVE', (0, i), (-1, i), 1.2, colors.HexColor(INK)),
                          ('LINEBELOW', (0, i), (-1, i), 1.2, colors.HexColor(INK))])
        if _hex(tone):
            style.append(('TEXTCOLOR', (4, i), (4, i), colors.HexColor(_hex(tone))))
        if _hex(ptone):
            style.append(('TEXTCOLOR', (5, i), (5, i), colors.HexColor(_hex(ptone))))

    def line(label, x, kind='line'):
        add([label, _d(x['prior']), _d(x['projected']), _d(x['proposed']), _chg(x['d']), _pc(x['p'])], kind, x['tone'], x['ptone'])

    for sec in sheet['sections']:
        if not sec['lines']:
            continue
        add([sec['name'].upper()] + [''] * 5, 'section')
        for l in sec['lines']:
            line(l['name'], l)
        line(f'Total {sec["name"].lower()}', sec, 'total')
        if sec['key'] == 'Operating':
            line('Total expenses without reserves', sheet['op'], 'total')
    if sheet['has_income']:
        line('Total expenses with reserves', sheet['total'], 'total')
        line('Assessments needed', sheet['needed'], 'grand')
    else:
        line('Total expenses with reserves', sheet['total'], 'grand')
    last = len(rows) - 1
    ink = colors.HexColor(INK)
    style.extend([('BACKGROUND', (3, 0), (3, last), colors.HexColor(TINT)), ('LINEBEFORE', (3, 0), (3, last), 1.8, ink), ('LINEAFTER', (3, 0), (3, last), 1.8, ink),
                  ('LINEABOVE', (3, 0), (3, 0), 1.8, ink), ('LINEBELOW', (3, last), (3, last), 1.8, ink), ('FONT', (3, 1), (3, last), 'Helvetica-Bold', 8.5)])
    table = Table(rows, colWidths=[2.55 * inch, 0.95 * inch, 0.95 * inch, 0.95 * inch, 0.8 * inch, 0.6 * inch], repeatRows=1)
    table.setStyle(TableStyle(style))
    story += [table, Spacer(1, 14)]
    ps = ParagraphStyle('u', fontName='Helvetica', fontSize=7.5, leading=9.5, textColor=colors.HexColor('#667085'))
    pb = ParagraphStyle('ub', fontName='Helvetica-Bold', fontSize=8.5, leading=10.5, textColor=colors.HexColor(INK))
    per = [['By unit calculation', f'{sheet["year"] - 1}', f'{sheet["year"]}', '$ change', '% change']]
    pstyle = [('FONT', (0, 0), (-1, 0), 'Helvetica-Bold', 8.5), ('BACKGROUND', (0, 0), (-1, 0), ink), ('TEXTCOLOR', (0, 0), (-1, 0), colors.white), ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
              ('FONT', (0, 1), (-1, -1), 'Helvetica', 8.5), ('BOX', (0, 0), (-1, -1), 0.8, ink), ('VALIGN', (0, 0), (-1, -1), 'TOP'),
              ('TOPPADDING', (0, 0), (-1, -1), 3.5), ('BOTTOMPADDING', (0, 0), (-1, -1), 3.5)]
    for g in sheet['calc']:
        per.append([[Paragraph(g['header'], pb), Paragraph(g['units'], ps)], '', '', '', ''])
        pstyle.append(('BACKGROUND', (0, len(per) - 1), (1, len(per) - 1), colors.HexColor('#f6f8fb')))
        for p in g['rows']:
            per.append([p['label'], _c2(p['old']), _c2(p['new']), _c2chg(p['d']), _pc(p['p'])])
            i = len(per) - 1
            pstyle.append(('LINEBELOW', (0, i), (-1, i), 0.3, colors.HexColor('#e3e8ef')))
            if p.get('bold'):
                pstyle.append(('FONT', (0, i), (-1, i), 'Helvetica-Bold', 8.5))
            if _hex(p['tone']):
                pstyle.append(('TEXTCOLOR', (3, i), (3, i), colors.HexColor(_hex(p['tone']))))
            if _hex(p['ptone']):
                pstyle.append(('TEXTCOLOR', (4, i), (4, i), colors.HexColor(_hex(p['ptone']))))
    n = len(per) - 1
    pstyle.extend([('BACKGROUND', (2, 1), (2, n), colors.HexColor(TINT)), ('FONT', (2, 1), (2, n), 'Helvetica-Bold', 8.5), ('LINEBEFORE', (2, 0), (2, n), 1.8, ink), ('LINEAFTER', (2, 0), (2, n), 1.8, ink),
                   ('LINEABOVE', (2, 0), (2, 0), 1.8, ink), ('LINEBELOW', (2, n), (2, n), 1.8, ink)])
    pt = Table(per, colWidths=[2.55 * inch, 1.1 * inch, 1.1 * inch, 1.05 * inch, 0.9 * inch], repeatRows=1)
    pt.setStyle(TableStyle(pstyle))
    story += [pt, Spacer(1, 8), Paragraph(f'{sheet["basis_note"]} Change is {sheet["year"]} against the {sheet["year"] - 1} budget. {NOTE}', sub)]

    def footer(canvas, d):
        canvas.saveState()
        canvas.setFont('Helvetica', 7.5)
        canvas.setFillColor(colors.HexColor('#667085'))
        canvas.drawString(0.6 * inch, 0.4 * inch, f'Prepared by Proper Realty, {sheet["on"]:%B} {sheet["on"].day}, {sheet["on"].year}')
        canvas.drawRightString(letter[0] - 0.6 * inch, 0.4 * inch, f'Page {d.page}')
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


def xlsx(sheet):
    """The sheet as an Excel workbook with real numbers (so it can be edited), set up to print one page wide."""
    wb = Workbook()
    ws = wb.active
    ws.title = f'{sheet["year"]} budget'
    thin = Side(style='thin', color='98A2B3')
    med = Side(style='medium', color=INK.lstrip('#'))
    ink = Font(bold=True, color=INK.lstrip('#'))
    tint = PatternFill('solid', fgColor=TINT.lstrip('#'))
    money, money2 = '$#,##0;[Red]-$#,##0', '$#,##0.00;($#,##0.00);"—"'
    chg_fmt, pct = '$#,##0;($#,##0);"—"', '0.0%;(0.0%);"—"'
    ws['A1'] = sheet['assoc']
    ws['A1'].font = Font(bold=True, size=15)
    ws['A2'] = f'{sheet["year"]} proposed budget · {sheet["stage_label"]} · {sheet["units"]} unit{"s" if sheet["units"] != 1 else ""}'
    ws['A2'].font = Font(color='667085')
    r = 4
    for c, h in enumerate(heads(sheet), 1):
        cell = ws.cell(row=r, column=c, value=h)
        cell.font = ink
        cell.alignment = Alignment(horizontal='left' if c == 1 else 'right')
        cell.border = Border(bottom=Side(style='medium', color=INK.lstrip('#')))
    head_row = r
    r += 1

    def put(label, x, bold=False, fill=None, top=False):
        nonlocal r
        for c, v in enumerate([label, x['prior'], x['projected'], x['proposed'], x['d'], None if x['p'] is None else x['p'] / 100], 1):
            if c == 6 and v is None:
                v = 'new' if round(x['d']) else '—'
            cell = ws.cell(row=r, column=c, value=v)
            cell.number_format = pct if c == 6 else chg_fmt if c == 5 else money
            cell.alignment = Alignment(horizontal='left' if c == 1 else 'right')
            if bold:
                cell.font = ink
            if fill:
                cell.fill = PatternFill('solid', fgColor=fill)
            if top:
                cell.border = Border(top=thin)
            tone = {5: x['tone'], 6: x['ptone']}.get(c)
            if _hex(tone):
                cell.font = Font(bold=bold, color=_hex(tone).lstrip('#'))
        r += 1

    for sec in sheet['sections']:
        if not sec['lines']:
            continue
        ws.cell(row=r, column=1, value=sec['name'].upper()).font = ink
        for c in range(1, 7):
            ws.cell(row=r, column=c).border = Border(bottom=Side(style='thin', color='C9D2E0'))
        r += 1
        for l in sec['lines']:
            put(l['name'], l)
        put(f'Total {sec["name"].lower()}', sec, bold=True, top=True)
        if sec['key'] == 'Operating':
            put('Total expenses without reserves', sheet['op'], bold=True)
    if sheet['has_income']:
        put('Total expenses with reserves', sheet['total'], bold=True, top=True)
        put('Assessments needed', sheet['needed'], bold=True, fill='EEF2F7', top=True)
    else:
        put('Total expenses with reserves', sheet['total'], bold=True, fill='EEF2F7', top=True)
    last_row = r - 1

    def box(col, first, last):
        """Tint a column and put a thick border around it, keeping the lines the cells already have."""
        for row in range(first, last + 1):
            cell = ws.cell(row=row, column=col)
            b = cell.border
            cell.border = Border(left=med, right=med, top=med if row == first else b.top, bottom=med if row == last else b.bottom)
            cell.fill = tint

    box(4, head_row, last_row)
    r += 1
    per_head = r
    for c, h in enumerate(['By unit calculation', f'{sheet["year"] - 1}', f'{sheet["year"]}', '$ change', '% change'], 1):
        cell = ws.cell(row=r, column=c, value=h)
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor=INK.lstrip('#'))
        cell.alignment = Alignment(horizontal='left' if c == 1 else 'right')
    r += 1
    for g in sheet['calc']:
        text = f'{g["header"]}: {g["units"]}'
        cell = ws.cell(row=r, column=1, value=text)
        cell.font = Font(bold=True, color=INK.lstrip('#'))
        cell.alignment = Alignment(wrap_text=True, vertical='top')
        ws.row_dimensions[r].height = 15 * max(1, -(-len(text) // 46))
        r += 1
        for p in g['rows']:
            for c, v in enumerate([p['label'], p['old'], p['new'], p['d'], None if p['p'] is None else p['p'] / 100], 1):
                if c == 5 and v is None:
                    v = 'new' if abs(p['d']) >= 0.005 else '—'
                cell = ws.cell(row=r, column=c, value=v)
                cell.number_format = pct if c == 5 else money2
                cell.alignment = Alignment(horizontal='left' if c == 1 else 'right')
                if p.get('bold'):
                    cell.font = ink
                tone = {4: p['tone'], 5: p['ptone']}.get(c)
                if _hex(tone):
                    cell.font = Font(bold=bool(p.get('bold')), color=_hex(tone).lstrip('#'))
            r += 1
    box(3, per_head, r - 1)
    for row in range(per_head + 1, r):
        ws.cell(row=row, column=3).font = ink
    r += 1
    ws.cell(row=r, column=1, value=f'{sheet["basis_note"]} Change is {sheet["year"]} against the {sheet["year"] - 1} budget. {NOTE} Prepared by Proper Realty, {sheet["on"]:%B} {sheet["on"].day}, {sheet["on"].year}.').font = Font(color='667085', size=9)
    ws.column_dimensions['A'].width = 44
    for c in range(2, 7):
        ws.column_dimensions[get_column_letter(c)].width = 16
    ws.freeze_panes = 'A5'
    ws.page_setup.orientation = 'portrait'
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = '4:4'
    ws.sheet_view.showGridLines = False
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
