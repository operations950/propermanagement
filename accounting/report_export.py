"""Download any report screen as a PDF or an Excel file. The report page is made as usual; its title, its tables and its notes are read back out of the page (the same figures the
person is looking at, for the same dates and book) and written as the file. One path for every report: add `@exportable` to a report view and put `?export=pdf` or `?export=xlsx`
on its address. The buttons on the report screens do that."""
import io
import re
from functools import wraps
from html.parser import HTMLParser

from django.http import HttpResponse

SKIP_TAGS = {'script', 'style', 'noscript', 'svg', 'template'}
BOLD_ROW = ('tot', 'sub', 'rp-h1', 'rp-h2', 'fw-bold', 'fw-semibold', 'grand', 'final')
NUM_CLASS = ('num', 'ac-num')


class _Page(HTMLParser):
    """Collects, in page order: the title (first h2), its sub line, the tables (rows of cells) and the small notes outside tables."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []          # ('table', rows) | ('note', text)
        self.title = ''
        self.sub = ''
        self._skip = 0            # depth inside script/style
        self._noprint = []        # stack of tags that opened an ac-noprint region
        self._stack = []
        self._h2 = False
        self._after_h2 = False
        self._table = None
        self._row = None
        self._cell = None
        self._note = None

    # -- helpers
    def _hidden(self):
        return self._skip > 0 or bool(self._noprint)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = (a.get('class') or '').split()
        if tag in SKIP_TAGS:
            self._skip += 1
            return
        if tag not in ('br', 'hr', 'img', 'input', 'meta', 'link'):
            self._stack.append((tag, 'ac-noprint' in cls or tag == 'form' or a.get('hidden') is not None))
            if self._stack[-1][1]:
                self._noprint.append(tag)
        if self._hidden():
            return
        if self._cell is not None and tag not in ('td', 'th'):
            self._cell['text'].append(' ')              # <span class="code">4500</span>Name, <small>2026</small>: keep the pieces apart
        if tag == 'h2' and not self.title:
            self._h2 = True
            self._buf = []
        elif tag == 'table':
            self._after_h2 = False
            self._table = []
        elif tag == 'tr' and self._table is not None:
            self._row = {'cells': [], 'cls': cls}
        elif tag in ('td', 'th') and self._row is not None:
            self._cell = {'text': [], 'cls': cls, 'span': int(a.get('colspan') or 1) if str(a.get('colspan') or '1').isdigit() else 1, 'head': tag == 'th'}
        elif tag == 'p' and self._table is None and self.title and ('small' in cls or 'text-muted' in cls):
            self._note = []
        elif tag == 'br' and self._cell is not None:
            self._cell['text'].append(' ')

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        hidden_before = self._hidden()
        if self._cell is not None and tag not in ('td', 'th') and not hidden_before:
            self._cell['text'].append(' ')
        if self._stack and self._stack[-1][0] == tag:
            _, opened = self._stack.pop()
            if opened and self._noprint:
                self._noprint.pop()
        elif any(t == tag for t, _ in self._stack):
            while self._stack:
                t, opened = self._stack.pop()
                if opened and self._noprint:
                    self._noprint.pop()
                if t == tag:
                    break
        if hidden_before and not self._hidden() and False:
            return
        if tag == 'h2' and self._h2:
            self._h2 = False
            self.title = _clean(''.join(self._buf))
            self._after_h2 = True
            self._sub = []
        elif tag in ('td', 'th') and self._cell is not None and self._row is not None:
            self._row['cells'].append({'text': _clean(''.join(self._cell['text'])), 'cls': self._cell['cls'], 'span': self._cell['span'], 'head': self._cell['head']})
            self._cell = None
        elif tag == 'tr' and self._row is not None and self._table is not None:
            if self._row['cells']:
                self._table.append(self._row)
            self._row = None
        elif tag == 'table' and self._table is not None:
            if self._table:
                self.blocks.append(('table', self._table))
            self._table = None
        elif tag == 'p' and self._note is not None:
            text = _clean(''.join(self._note))
            if text:
                self.blocks.append(('note', text))
            self._note = None

    def handle_data(self, data):
        if self._hidden():
            return
        if self._h2:
            self._buf.append(data)
        elif self._cell is not None:
            self._cell['text'].append(data)
        elif self._note is not None:
            self._note.append(data)
        elif self._after_h2 and self._table is None:
            self._sub.append(data)
            self.sub = _clean(' '.join(self._sub))[:300]


def _clean(text):
    return re.sub(r'\s+', ' ', (text or '').replace('\xa0', ' ')).strip()


def _number(text):
    """The number in a figure like $1,234.56, (1,234.56) or -12.00; None when it is not one."""
    t = text.strip().replace('$', '').replace(',', '').replace(' ', '')
    if not t or t in ('-', '—', '–'):
        return None
    neg = False
    if t.startswith('(') and t.endswith(')'):
        neg, t = True, t[1:-1]
    if t.startswith('-'):
        neg, t = not neg, t[1:]
    if t.endswith('-'):
        neg, t = not neg, t[:-1]
    if not re.fullmatch(r'\d+(\.\d+)?', t):
        return None
    v = float(t)
    return -v if neg else v


def parse(html):
    p = _Page()
    p.feed(html)
    p.close()
    return {'title': p.title or 'Report', 'sub': p.sub, 'blocks': p.blocks}


def _is_num(cell):
    return any(c in cell['cls'] for c in NUM_CLASS)


def _bold(row):
    return any(c in row['cls'] for c in BOLD_ROW) or all(c['head'] for c in row['cells'])


def file_name(page, ext):
    base = _clean(page['sub'].split('·')[0]) if page['sub'] else ''
    name = f"{base} - {page['title']}" if base else page['title']
    name = re.sub(r'[\\/:*?"<>|]+', '', name).strip()[:120] or 'Report'
    return f'{name}.{ext}'


# ------------------------------------------------------------------ Excel
def to_xlsx(page):
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = re.sub(r'[\[\]:*?/\\]', '', page['title'])[:31] or 'Report'
    r = 1
    ws.cell(r, 1, page['title']).font = Font(bold=True, size=14)
    r += 1
    if page['sub']:
        ws.cell(r, 1, page['sub']).font = Font(color='666666')
        r += 1
    r += 1
    widths = {}
    head_fill = PatternFill('solid', fgColor='E7ECEF')
    for kind, data in page['blocks']:
        if kind == 'note':
            ws.cell(r, 1, data).font = Font(italic=True, color='666666')
            r += 1
            continue
        for row in data:
            col = 1
            bold = _bold(row)
            for cell in row['cells']:
                num = _number(cell['text']) if _is_num(cell) else None
                c = ws.cell(r, col, num if num is not None else cell['text'])
                if num is not None:
                    c.number_format = '#,##0.00;-#,##0.00'
                    c.alignment = Alignment(horizontal='right')
                elif _is_num(cell):
                    c.alignment = Alignment(horizontal='right')
                if bold:
                    c.font = Font(bold=True)
                if all(x['head'] for x in row['cells']):
                    c.fill = head_fill
                if cell['span'] == 1:
                    widths[col] = max(widths.get(col, 0), min(len(cell['text']), 70))
                col += cell['span']
            r += 1
        r += 1
    for col, w in widths.items():
        ws.column_dimensions[get_column_letter(col)].width = max(10, w + 2)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ------------------------------------------------------------------ PDF
def to_pdf(page):
    from xml.sax.saxutils import escape
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    tables = [d for k, d in page['blocks'] if k == 'table']
    ncols = max((sum(c['span'] for c in row['cells']) for t in tables for row in t), default=1)
    size = landscape(letter) if ncols > 5 else letter
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=size, leftMargin=0.5 * inch, rightMargin=0.5 * inch, topMargin=0.5 * inch, bottomMargin=0.6 * inch, title=page['title'])
    width = size[0] - inch
    h1 = ParagraphStyle('h1', fontName='Helvetica-Bold', fontSize=14, leading=17)
    sub = ParagraphStyle('sub', fontName='Helvetica', fontSize=9, leading=12, textColor=colors.HexColor('#555555'))
    txt = ParagraphStyle('t', fontName='Helvetica', fontSize=7.5, leading=9.2)
    txtb = ParagraphStyle('tb', parent=txt, fontName='Helvetica-Bold')
    numst = ParagraphStyle('n', parent=txt, alignment=TA_RIGHT)
    numb = ParagraphStyle('nb', parent=numst, fontName='Helvetica-Bold')
    note = ParagraphStyle('note', fontName='Helvetica-Oblique', fontSize=8, leading=10, textColor=colors.HexColor('#555555'))
    story = [Paragraph(escape(page['title']), h1)]
    if page['sub']:
        story.append(Paragraph(escape(page['sub']), sub))
    story.append(Spacer(1, 8))
    for kind, data in page['blocks']:
        if kind == 'note':
            story += [Paragraph(escape(data), note), Spacer(1, 4)]
            continue
        n = max(sum(c['span'] for c in row['cells']) for row in data)
        longest = [0] * n
        for row in data:
            col = 0
            for c in row['cells']:
                if c['span'] == 1 and col < n:
                    longest[col] = max(longest[col], min(len(c['text']), 60))
                col += c['span']
        weights = [max(6, x) + (2 if i == 0 else 0) for i, x in enumerate(longest)]
        total = sum(weights) or 1
        col_w = [width * w / total for w in weights]
        cells, style, spans = [], [('VALIGN', (0, 0), (-1, -1), 'TOP'), ('TOPPADDING', (0, 0), (-1, -1), 2), ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
                                  ('LINEBELOW', (0, 0), (-1, -1), 0.25, colors.HexColor('#dddddd'))], []
        for ri, row in enumerate(data):
            bold = _bold(row)
            out, col = [], 0
            for c in row['cells']:
                st = (numb if bold else numst) if _is_num(c) else (txtb if bold else txt)
                out.append(Paragraph(escape(c['text']), st))
                if c['span'] > 1:
                    spans.append(('SPAN', (col, ri), (col + c['span'] - 1, ri)))
                    out += [''] * (c['span'] - 1)
                col += c['span']
            out += [''] * (n - len(out))
            cells.append(out[:n])
            if all(c['head'] for c in row['cells']):
                style.append(('BACKGROUND', (0, ri), (-1, ri), colors.HexColor('#e7ecef')))
        t = Table(cells, colWidths=col_w, repeatRows=1 if data and all(c['head'] for c in data[0]['cells']) else 0)
        t.setStyle(TableStyle(style + spans))
        story += [t, Spacer(1, 10)]

    def foot(canvas, d):
        canvas.saveState()
        canvas.setFont('Helvetica', 7.5)
        canvas.setFillColor(colors.HexColor('#777777'))
        canvas.drawString(0.5 * inch, 0.35 * inch, page['title'] + (' - ' + page['sub'].split('·')[0].strip() if page['sub'] else ''))
        canvas.drawRightString(size[0] - 0.5 * inch, 0.35 * inch, f'Page {d.page}')
        canvas.restoreState()
    doc.build(story, onFirstPage=foot, onLaterPages=foot)
    return buf.getvalue()


def respond(html, kind):
    page = parse(html)
    if kind == 'xlsx':
        body, ctype = to_xlsx(page), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    else:
        kind, body, ctype = 'pdf', to_pdf(page), 'application/pdf'
    response = HttpResponse(body, content_type=ctype)
    response['Content-Disposition'] = f'attachment; filename="{file_name(page, kind)}"'
    return response


def exportable(view):
    """A report view that can also be downloaded: `?export=pdf` or `?export=xlsx` returns the same report as that file."""
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        response = view(request, *args, **kwargs)
        kind = request.GET.get('export')
        if kind in ('pdf', 'xlsx') and getattr(response, 'status_code', 0) == 200 and hasattr(response, 'content'):
            return respond(response.content.decode('utf-8'), kind)
        return response
    return wrapped
