"""A generated document as a PDF (so it can be emailed as an attachment). The documents are simple, controlled HTML (the
templates in catalog.py / catalog_meetings.py: paragraphs, lists, a few tables, signature lines), so this reads that
HTML into a small tree and lays it out with reportlab. It is not a general HTML renderer."""
import io
import os
from html import escape
from html.parser import HTMLParser

from django.conf import settings
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.platypus.flowables import HRFlowable

from . import services

VOID = {'br', 'img', 'hr', 'input', 'meta', 'link'}
BODY_W = 6.5 * inch
BASE = ParagraphStyle('lf', fontName='Helvetica', fontSize=10.5, leading=14, spaceAfter=9)
SMALL = ParagraphStyle('lf-small', parent=BASE, fontSize=9, leading=11, spaceAfter=0)
CELL = ParagraphStyle('lf-cell', parent=BASE, spaceAfter=0, fontSize=10, leading=13)


class Node:
    def __init__(self, tag, attrs=None):
        self.tag, self.attrs, self.children = tag, dict(attrs or {}), []

    def has_class(self, name):
        return name in (self.attrs.get('class') or '').split()


class _Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node('root')
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(Node(tag, attrs))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _parse(html):
    tree = _Tree()
    tree.feed(html)
    return tree.root


# ------------------------------------------------------------------------------------------------------------ inline
def _squash(text):
    return ' '.join(text.split()) if text.strip() else (' ' if text else '')


def _inline(node):
    """Paragraph markup for a node's inline content."""
    out = []
    for child in node.children:
        if isinstance(child, str):
            out.append(escape(_squash(child)))
            continue
        tag = child.tag
        if tag == 'br':
            out.append('<br/>')
        elif tag in ('strong', 'b'):
            out.append('<b>' + _inline(child) + '</b>')
        elif tag in ('em', 'i'):
            out.append('<i>' + _inline(child) + '</i>')
        elif tag == 'u':
            out.append('<u>' + _inline(child) + '</u>')
        elif tag == 'span' and child.has_class('lf-blank'):
            out.append('_______ ')
        elif tag == 'span' and child.has_class('lf-line-inline'):
            out.append('____________________')
        elif tag in ('img', 'hr', 'table'):
            continue
        elif tag == 'div':
            out.append('<br/>' + _inline(child))
        else:
            out.append(_inline(child))
    text = ''.join(out).strip()
    while text.startswith('<br/>'):
        text = text[5:].lstrip()
    return text


def _is_block(child):
    return not isinstance(child, str) and child.tag in ('p', 'div', 'ol', 'ul', 'table', 'hr', 'li', 'img', 'h1', 'h2', 'h3', 'h4')


# ------------------------------------------------------------------------------------------------------------ blocks
def _logo_image(width=150):
    for candidate in (os.path.join(settings.BASE_DIR, 'static', 'img', 'proper-realty-logo.png'), os.path.join(getattr(settings, 'STATIC_ROOT', '') or '', 'img', 'proper-realty-logo.png')):
        if candidate and os.path.exists(candidate):
            img = Image(candidate)
            ratio = img.imageWidth / float(img.imageHeight)
            img.drawWidth, img.drawHeight = width, width / ratio
            img.hAlign = 'RIGHT'
            return img
    return None


def _para(node, style=BASE):
    markup = _inline(node)
    if not markup:
        return None
    s = style
    if node.has_class('lf-center'):
        s = ParagraphStyle('c', parent=style, alignment=TA_CENTER)
    elif node.has_class('lf-r-block'):
        s = ParagraphStyle('r', parent=style, alignment=TA_RIGHT)
    if node.has_class('lf-h'):
        markup = '<b>' + markup + '</b>'
    return Paragraph(markup, s)


def _cell_flowables(td, style=CELL):
    flows = []
    for child in td.children:
        if not isinstance(child, str) and child.tag == 'img':
            logo = _logo_image()
            if logo:
                flows.append(logo)
    markup = _inline(td)
    if markup:
        if td.has_class('lf-r') or td.has_class('lf-contact'):
            style = ParagraphStyle('rr', parent=style, alignment=TA_RIGHT)
        flows.append(Paragraph(markup, style))
    return flows or [Spacer(1, 1)]


def _table(node):
    rows = []
    for tr in _find(node, 'tr'):
        cells = [c for c in tr.children if not isinstance(c, str) and c.tag in ('td', 'th')]
        rows.append(cells)
    rows = [r for r in rows if r]
    if not rows:
        return None
    ncols = max(len(r) for r in rows)
    data = []
    for r in rows:
        line = []
        for c in r:
            flows = _cell_flowables(c, CELL if c.tag == 'td' else ParagraphStyle('th', parent=CELL, fontName='Helvetica-Bold'))
            line.append(flows)
        line += [''] * (ncols - len(line))
        data.append(line)
    if node.has_class('lf-kv') and ncols == 2:
        left = 0.25 if node.has_class('lf-left') else 0.48
        widths = [BODY_W * left, BODY_W * (1 - left)]
    elif node.has_class('lf-grid') and ncols == 4:
        widths = [BODY_W * 0.1, BODY_W * 0.3, BODY_W * 0.3, BODY_W * 0.3]
    elif node.has_class('lf-head') and ncols == 2:
        widths = [BODY_W * 0.65, BODY_W * 0.35]
    else:
        widths = [BODY_W / ncols] * ncols
    table = Table(data, colWidths=widths, hAlign='LEFT')
    style = [('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 4), ('RIGHTPADDING', (0, 0), (-1, -1), 4), ('TOPPADDING', (0, 0), (-1, -1), 2), ('BOTTOMPADDING', (0, 0), (-1, -1), 2)]
    if node.has_class('lf-kv') and not node.has_class('lf-left'):
        style.append(('ALIGN', (0, 0), (0, -1), 'RIGHT'))
    if node.has_class('lf-grid'):
        style += [('GRID', (0, 0), (-1, -1), 0.6, colors.black), ('TOPPADDING', (0, 1), (-1, -1), 9), ('BOTTOMPADDING', (0, 1), (-1, -1), 9)]
    table.setStyle(TableStyle(style))
    return table


def _find(node, tag):
    out = []
    for child in node.children:
        if isinstance(child, str):
            continue
        if child.tag == tag:
            out.append(child)
        elif child.tag in ('tbody', 'thead', 'tfoot'):
            out.extend(_find(child, tag))
    return out


def _list(node, ordered):
    flows = []
    n = 0
    for li in (c for c in node.children if not isinstance(c, str) and c.tag == 'li'):
        n += 1
        markup = _inline(li)
        if markup:
            flows.append(Paragraph(markup, ParagraphStyle('li', parent=BASE, leftIndent=22, bulletIndent=6, spaceAfter=3), bulletText=f'{n}.' if ordered else '•'))
    flows.append(Spacer(1, 6))
    return flows


def _line(width=None, left=0):
    return HRFlowable(width=width or BODY_W * 0.5, thickness=0.8, color=colors.black, hAlign='RIGHT' if left else 'LEFT', spaceBefore=18, spaceAfter=3)


def _blocks(node):
    flows = []
    pending = Node('p')
    for child in node.children:
        if _is_block(child):
            if pending.children:
                p = _para(pending)
                if p is not None:
                    flows.append(p)
                pending = Node('p')
            flows.extend(_block(child))
        else:
            pending.children.append(child)
    if pending.children:
        p = _para(pending)
        if p is not None:
            flows.append(p)
    return flows


def _block(node):
    tag = node.tag
    if tag == 'p':
        p = _para(node)
        return [p] if p is not None else []
    if tag in ('ol', 'ul'):
        return _list(node, tag == 'ol')
    if tag == 'table':
        t = _table(node)
        return [t, Spacer(1, 8)] if t is not None else []
    if tag == 'hr':
        return [HRFlowable(width='100%', thickness=0.6, color=colors.black)]
    if tag == 'img':
        logo = _logo_image()
        return [logo] if logo else []
    if tag == 'div':
        if node.has_class('lf-line'):
            return [_line()]
        if node.has_class('lf-sig'):
            return [KeepTogether([Spacer(1, 22), _line(BODY_W * 0.5, left=1), Paragraph(_inline(node) or '', ParagraphStyle('sig', parent=BASE, alignment=TA_LEFT, leftIndent=BODY_W * 0.5)) if _inline(node) else Spacer(1, 1)])]
        if node.has_class('lf-notary'):
            return [_notary(node)]
        if node.has_class('lf-by'):
            return [Paragraph('By: ______________________________', ParagraphStyle('by', parent=BASE, alignment=TA_RIGHT, spaceBefore=22))]
        return _blocks(node)
    if tag in ('h1', 'h2', 'h3', 'h4'):
        return [Paragraph('<b>' + _inline(node) + '</b>', BASE)]
    return _blocks(node)


def _notary(node):
    left, right = [], []
    for child in node.children:
        if isinstance(child, str):
            continue
        target = left if child.has_class('lf-left') else right
        target.extend(_blocks(child) or [Paragraph('[Notary Seal]', BASE)])
    # the right-hand column is a stack of short lines (a signature line, then the tick boxes and the ID lines)
    stacked = []
    for item in right:
        stacked.append(item)
    table = Table([[left or '', stacked or '']], colWidths=[BODY_W * 0.4, BODY_W * 0.6], hAlign='LEFT')
    table.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    return KeepTogether([Spacer(1, 18), table])


# ------------------------------------------------------------------------------------------------------------- public
def html_to_pdf(html, title='Document'):
    """PDF bytes for a document's frozen HTML."""
    root = _parse(html.replace(services.LOGO_TOKEN, 'logo.png'))
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=1 * inch, rightMargin=1 * inch, topMargin=0.9 * inch, bottomMargin=0.9 * inch, title=title, author='Proper Realty Co.')
    flows = _blocks(root)
    doc.build(flows or [Paragraph(' ', BASE)])
    return buf.getvalue()


def document_pdf(doc):
    return html_to_pdf(doc.body_html, title=doc.template.name + (f' - {doc.subject}' if doc.subject else ''))


def pdf_filename(doc):
    base = ''.join(ch if ch.isalnum() or ch in ' -_' else '' for ch in f'{doc.template.name} {doc.subject}').strip()
    return (base or 'document')[:90].replace(' ', '_') + '.pdf'
