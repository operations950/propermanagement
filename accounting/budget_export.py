"""The budget as a clean sheet to hand to the board: a PDF to print and an Excel file to edit. Both come from the same figures the Board sheet screen shows (views_budget._sheet)."""
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


def file_name(assoc_name, year, stage_label, ext):
    base = re.sub(r'[^A-Za-z0-9 ._()-]+', '', f'{assoc_name} {year} budget ({stage_label})').strip()
    return f'{base}.{ext}'


def _d(v):
    return f'-${abs(round(v)):,}' if round(v) < 0 else f'${round(v):,}'


def _chg(v):
    r = round(v)
    return f'+${r:,}' if r > 0 else f'-${abs(r):,}' if r < 0 else '$0'


def _pc(p):
    return '' if p is None else f'{p:+.0f}%' if round(p) else '0%'


def _c2(v):
    return f'${v:,.2f}'


def _c2chg(v):
    return f'{"+" if v > 0.004 else "-" if v < -0.004 else ""}${abs(v):,.2f}'


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

    def add(cells, kind='line'):
        rows.append(cells)
        i = len(rows) - 1
        if kind == 'section':
            style.extend([('FONT', (0, i), (-1, i), 'Helvetica-Bold', 8.5), ('SPAN', (0, i), (-1, i)), ('TOPPADDING', (0, i), (-1, i), 9), ('LINEBELOW', (0, i), (-1, i), 0.5, colors.HexColor('#c9d2e0'))])
        elif kind == 'total':
            style.extend([('FONT', (0, i), (-1, i), 'Helvetica-Bold', 8.5), ('LINEABOVE', (0, i), (-1, i), 0.5, colors.HexColor('#98a2b3'))])
        elif kind == 'grand':
            style.extend([('FONT', (0, i), (-1, i), 'Helvetica-Bold', 9.5), ('BACKGROUND', (0, i), (-1, i), colors.HexColor('#eef2f7')), ('LINEABOVE', (0, i), (-1, i), 1.2, colors.HexColor(INK)),
                          ('LINEBELOW', (0, i), (-1, i), 1.2, colors.HexColor(INK))])

    for sec in sheet['sections']:
        if not sec['lines']:
            continue
        add([sec['name'].upper()] + [''] * 5, 'section')
        for l in sec['lines']:
            add([l['name'], _d(l['prior']), _d(l['projected']), _d(l['proposed']), _chg(l['d']), _pc(l['p'])])
        add([f'Total {sec["name"].lower()}', _d(sec['prior']), _d(sec['projected']), _d(sec['proposed']), _chg(sec['d']), _pc(sec['p'])], 'total')
        if sec['key'] == 'Operating':
            o = sheet['op']
            add(['Total expenses without reserves', _d(o['prior']), _d(o['projected']), _d(o['proposed']), _chg(o['d']), _pc(o['p'])], 'total')
    t = sheet['total']
    add(['Total expenses with reserves', _d(t['prior']), _d(t['projected']), _d(t['proposed']), _chg(t['d']), _pc(t['p'])], 'grand')
    table = Table(rows, colWidths=[2.55 * inch, 0.95 * inch, 0.95 * inch, 0.95 * inch, 0.8 * inch, 0.6 * inch], repeatRows=1)
    table.setStyle(TableStyle(style))
    story += [table, Spacer(1, 14)]
    per = [['What it means per unit', f'{sheet["year"] - 1} budget', f'{sheet["year"]} budget', '$ change', '% change']]
    pstyle = [('FONT', (0, 0), (-1, 0), 'Helvetica-Bold', 8.5), ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor(INK)), ('TEXTCOLOR', (0, 0), (-1, 0), colors.white), ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
              ('FONT', (0, 1), (-1, -1), 'Helvetica', 8.5), ('BOX', (0, 0), (-1, -1), 0.8, colors.HexColor(INK)), ('LINEBELOW', (0, 1), (-1, -2), 0.3, colors.HexColor('#e3e8ef')),
              ('TOPPADDING', (0, 0), (-1, -1), 3.5), ('BOTTOMPADDING', (0, 0), (-1, -1), 3.5)]
    for i, p in enumerate(sheet['per'], 1):
        per.append([p['label'], _c2(p['old']), _c2(p['new']), _c2chg(p['d']), _pc(p['p'])])
        if p.get('bold'):
            pstyle.append(('FONT', (0, i), (-1, i), 'Helvetica-Bold', 8.5))
    pt = Table(per, colWidths=[2.55 * inch, 1.1 * inch, 1.1 * inch, 1.05 * inch, 0.9 * inch])
    pt.setStyle(TableStyle(pstyle))
    story += [pt, Spacer(1, 8), Paragraph(f'Equal share per unit. Change is {sheet["year"]} against the {sheet["year"] - 1} budget.', sub)]

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
    ink = Font(bold=True, color=INK.lstrip('#'))
    money, money2, pct = '$#,##0;[Red]-$#,##0', '$#,##0.00;[Red]-$#,##0.00', '0%;[Red]-0%'
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
    r += 1

    def put(label, prior, proj, prop, d, p, bold=False, fill=None, top=False):
        nonlocal r
        for c, v in enumerate([label, prior, proj, prop, d, p], 1):
            cell = ws.cell(row=r, column=c, value=v)
            cell.number_format = pct if c == 6 else money
            if bold:
                cell.font = ink
            if fill:
                cell.fill = PatternFill('solid', fgColor=fill)
            if top:
                cell.border = Border(top=thin)
        r += 1

    for sec in sheet['sections']:
        if not sec['lines']:
            continue
        ws.cell(row=r, column=1, value=sec['name'].upper()).font = ink
        for c in range(1, 7):
            ws.cell(row=r, column=c).border = Border(bottom=Side(style='thin', color='C9D2E0'))
        r += 1
        for l in sec['lines']:
            put(l['name'], l['prior'], l['projected'], l['proposed'], l['d'], None if l['p'] is None else l['p'] / 100)
        put(f'Total {sec["name"].lower()}', sec['prior'], sec['projected'], sec['proposed'], sec['d'], None if sec['p'] is None else sec['p'] / 100, bold=True, top=True)
        if sec['key'] == 'Operating':
            o = sheet['op']
            put('Total expenses without reserves', o['prior'], o['projected'], o['proposed'], o['d'], None if o['p'] is None else o['p'] / 100, bold=True)
    t = sheet['total']
    put('Total expenses with reserves', t['prior'], t['projected'], t['proposed'], t['d'], None if t['p'] is None else t['p'] / 100, bold=True, fill='EEF2F7', top=True)
    r += 1
    for c, h in enumerate(['What it means per unit', f'{sheet["year"] - 1} budget', f'{sheet["year"]} budget', '$ change', '% change'], 1):
        cell = ws.cell(row=r, column=c, value=h)
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor=INK.lstrip('#'))
        cell.alignment = Alignment(horizontal='left' if c == 1 else 'right')
    r += 1
    for p in sheet['per']:
        for c, v in enumerate([p['label'], p['old'], p['new'], p['d'], None if p['p'] is None else p['p'] / 100], 1):
            cell = ws.cell(row=r, column=c, value=v)
            cell.number_format = pct if c == 5 else money2
            if p.get('bold'):
                cell.font = ink
        r += 1
    r += 1
    ws.cell(row=r, column=1, value=f'Equal share per unit. Change is {sheet["year"]} against the {sheet["year"] - 1} budget. Prepared by Proper Realty, {sheet["on"]:%B} {sheet["on"].day}, {sheet["on"].year}.').font = Font(color='667085', size=9)
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
