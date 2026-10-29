"""Reading an uploaded document once, and working out what it is.

A document is read ONCE: the words of every page are kept (DocumentPage), and questions, settings and sorting all work from those kept words, never from the file again.
  * A page that has a text layer is read here for nothing.
  * A scanned page is read by Claude, a few pages at a time, once, and kept. Pages already read are never read again, so a job that stops halfway picks up where it was.
Sorting looks at the first words of each page (a few thousand words in all, not the document) and says where each document in the file starts and ends.
"""
import io
import logging
import re

from django.utils import timezone

from .models import AssociationDocument, DocumentPage

logger = logging.getLogger(__name__)
OCR_MIN_CHARS = 60          # a page with fewer letters than this is treated as a scan
CHUNK_PAGES = 8             # scanned pages sent to Claude at once
OUTLINE_CHARS = 320         # how much of each page the sorting looks at
KINDS = [k for k, _ in AssociationDocument.Kind.choices]

TRANSCRIBE_TOOL = {
    'name': 'transcribe',
    'description': 'The words on each page, in order.',
    'input_schema': {'type': 'object', 'properties': {'pages': {'type': 'array', 'items': {'type': 'string'}, 'description': 'One entry per page, in order. An empty string for a blank page.'}}, 'required': ['pages']},
}
SORT_TOOL = {
    'name': 'report_documents',
    'description': 'Where each document in the file starts and ends.',
    'input_schema': {'type': 'object', 'properties': {'documents': {'type': 'array', 'items': {'type': 'object', 'properties': {
        'kind': {'type': 'string', 'enum': KINDS}, 'title': {'type': 'string', 'description': 'What it is called, with its date or recording details when the page says them.'},
        'start_page': {'type': 'integer'}, 'end_page': {'type': 'integer'}, 'confidence': {'type': 'number', 'description': '0 to 1: how sure you are of the kind and of where it starts and ends.'}},
        'required': ['kind', 'title', 'start_page', 'end_page', 'confidence']}}}, 'required': ['documents']},
}


def _tool_input(message, name):
    block = next((b for b in message.content if getattr(b, 'type', '') == 'tool_use' and getattr(b, 'name', name) == name), None)
    return (block.input if block else None) or {}


# ---------------------------------------------------------------- reading

def _open(doc):
    from .documents import DocumentError
    try:
        doc.file.open('rb')
        data = doc.file.read()
        doc.file.close()
    except Exception:
        raise DocumentError(f'The file for "{doc.name}" could not be opened.')
    return data


def _transcribe(client, blocks, n_pages, model):
    message = client.messages.create(
        model=model, max_tokens=16000, tools=[TRANSCRIBE_TOOL], tool_choice={'type': 'tool', 'name': 'transcribe'},
        messages=[{'role': 'user', 'content': blocks + [{'type': 'text', 'text':
            f'This has {n_pages} page{"s" if n_pages != 1 else ""}. Transcribe the words on every page faithfully and in order: one entry per page, keeping headings, numbering and paragraph breaks. '
            'Do not summarise or correct anything. A blank page is an empty string.'}]}])
    pages = [str(p) for p in (_tool_input(message, 'transcribe').get('pages') or [])]
    return (pages + [''] * n_pages)[:n_pages]


def read_document(doc):
    """Keep the words of every page of the file. Raises on a problem the job should retry; returns the number of scanned pages Claude read."""
    from . import documents
    data = _open(doc)
    name = doc.file.name.rsplit('/', 1)[-1]
    ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    note = ''
    if ext == 'pdf':
        from pypdf import PdfReader, PdfWriter
        try:
            reader = PdfReader(io.BytesIO(data))
            n = len(reader.pages)
        except Exception:
            raise documents.DocumentError(f'"{doc.name}" is not a PDF this program can open.')
        if not DocumentPage.objects.filter(document=doc).count() == n:
            DocumentPage.objects.filter(document=doc).delete()
            texts = []
            for i in range(n):
                try:
                    texts.append((reader.pages[i].extract_text() or '').strip())
                except Exception:
                    texts.append('')
            DocumentPage.objects.bulk_create([DocumentPage(document=doc, number=i + 1, text=t) for i, t in enumerate(texts)])
        pages = {p.number: p for p in DocumentPage.objects.filter(document=doc)}
        todo = [i for i in range(n) if len(re.findall(r'[A-Za-z]', pages[i + 1].text)) < OCR_MIN_CHARS and not pages[i + 1].by_claude]
        read_by_claude = 0
        if todo:
            try:
                client = documents._client()
            except documents.DocumentError:
                client = None
            if client is None:
                note = f'{len(todo)} scanned page{"s" if len(todo) != 1 else ""} could not be read: the Claude connection is not set up.'
            else:
                for run_start in range(0, len(todo), CHUNK_PAGES):
                    idxs = todo[run_start:run_start + CHUNK_PAGES]
                    writer = PdfWriter()
                    for i in idxs:
                        writer.add_page(reader.pages[i])
                    buf = io.BytesIO()
                    writer.write(buf)
                    import base64
                    blocks = [{'type': 'document', 'source': {'type': 'base64', 'media_type': 'application/pdf', 'data': base64.b64encode(buf.getvalue()).decode()}}]
                    for i, text in zip(idxs, _transcribe(client, blocks, len(idxs), documents.MODEL)):
                        p = pages[i + 1]
                        p.text, p.by_claude = text.strip(), True
                        p.save(update_fields=['text', 'by_claude'])
                        read_by_claude += 1
        left = sum(1 for p in DocumentPage.objects.filter(document=doc) if len(re.findall(r'[A-Za-z]', p.text)) < OCR_MIN_CHARS and not p.by_claude)
        doc.page_count = n
        doc.text_status = 'partial' if left and note else 'ready'
        doc.read_note = note
        doc.save(update_fields=['page_count', 'text_status', 'read_note'])
        return read_by_claude
    if ext in ('jpg', 'jpeg', 'png', 'gif', 'webp'):
        client = documents._client()
        blocks = documents._file_to_content_blocks(data, '', name)
        text = _transcribe(client, blocks, 1, documents.MODEL)[0]
        pages_text = [text.strip()]
    elif ext in ('txt', 'csv'):
        raw = data.decode('utf-8', errors='replace')
        pages_text = [raw[i:i + 3500] for i in range(0, len(raw), 3500)] or ['']
    else:
        raise documents.DocumentError(f'"{doc.name}" is not a PDF, picture or text file, so it cannot be read.')
    DocumentPage.objects.filter(document=doc).delete()
    DocumentPage.objects.bulk_create([DocumentPage(document=doc, number=i + 1, text=t, by_claude=ext not in ('txt', 'csv')) for i, t in enumerate(pages_text)])
    doc.page_count, doc.text_status, doc.read_note = len(pages_text), 'ready', ''
    doc.save(update_fields=['page_count', 'text_status', 'read_note'])
    return 0 if ext in ('txt', 'csv') else 1


# ---------------------------------------------------------------- sorting

def clean_sections(found, n):
    """The sections Claude reported, made safe: inside the file, in order, not overlapping, and with no page left out (what is left over is 'something else')."""
    rows = []
    for s in found or []:
        try:
            start, end = int(s.get('start_page')), int(s.get('end_page'))
        except (TypeError, ValueError):
            continue
        kind = s.get('kind') if s.get('kind') in KINDS else 'other'
        try:
            conf = max(0.0, min(1.0, float(s.get('confidence'))))
        except (TypeError, ValueError):
            conf = 0.5
        start, end = max(1, min(start, n)), max(1, min(end, n))
        if end < start:
            start, end = end, start
        rows.append({'kind': kind, 'title': (s.get('title') or '').strip()[:160], 'start': start, 'end': end, 'confidence': round(conf, 2)})
    rows.sort(key=lambda r: (r['start'], r['end']))
    out, cursor = [], 1
    for r in rows:
        if r['start'] < cursor:
            r['start'] = cursor
        if r['start'] > r['end']:
            continue
        if r['start'] > cursor:
            out.append({'kind': 'other', 'title': 'Other pages', 'start': cursor, 'end': r['start'] - 1, 'confidence': 0.3})
        out.append(r)
        cursor = r['end'] + 1
    if cursor <= n:
        out.append({'kind': 'other', 'title': 'Other pages', 'start': cursor, 'end': n, 'confidence': 0.3})
    return out or [{'kind': 'other', 'title': '', 'start': 1, 'end': max(n, 1), 'confidence': 0.0}]


def sort_document(doc):
    """Work out which documents are in the file. Leaves sort_status 'proposed' (a person confirms) or 'manual' (a person chooses, when it cannot be worked out)."""
    from . import documents
    pages = list(DocumentPage.objects.filter(document=doc).order_by('number'))
    n = len(pages)
    whole = [{'kind': 'other', 'title': doc.name, 'start': 1, 'end': max(n, 1), 'confidence': 0.0}]
    if not n:
        doc.sections, doc.sort_status = whole, 'manual'
        doc.save(update_fields=['sections', 'sort_status'])
        return
    chars = OUTLINE_CHARS if n <= 200 else 200
    outline = '\n'.join(f'[page {p.number}] ' + ' '.join(p.text.split())[:chars] for p in pages)
    try:
        client = documents._client()
        message = client.messages.create(
            model=documents.MODEL, max_tokens=4000, tools=[SORT_TOOL], tool_choice={'type': 'tool', 'name': 'report_documents'},
            messages=[{'role': 'user', 'content': [{'type': 'text', 'text':
                f'Below are the first words of each of the {n} pages of ONE file uploaded for a Florida condominium or homeowners association. The file may hold a single document or several joined together: '
                'a Declaration of Condominium, Articles of Incorporation, Bylaws, Rules and Regulations, amendments to any of them, a management contract, or a budget. '
                'Say where each document in the file starts and ends. Every page belongs to exactly one document. Use "amendment" for an amendment to the declaration, bylaws or articles and say which one in the title. '
                'Use "other" for anything else (a cover sheet, a certificate, minutes). Give each a short title with its date or recording details when the pages state them.\n\n' + outline}]}])
        sections = clean_sections(_tool_input(message, 'report_documents').get('documents'), n)
        status = 'proposed'
    except documents.DocumentError:
        sections, status = whole, 'manual'
    except Exception:
        logger.exception('Claude could not sort the document "%s"', doc.name)
        sections, status = whole, 'manual'
    doc.sections, doc.sort_status = sections, status
    doc.save(update_fields=['sections', 'sort_status'])


# ---------------------------------------------------------------- the job

def process_waiting(limit=3, log=None):
    """Read documents that are waiting, then sort the ones that were uploaded without saying what they are. Safe to run often and from two places at once."""
    from datetime import timedelta
    say = log or (lambda s: None)
    now = timezone.now()
    AssociationDocument.objects.filter(text_status='reading', read_started_at__lt=now - timedelta(minutes=20)).update(text_status='waiting')
    done = 0
    for doc in list(AssociationDocument.objects.filter(text_status='waiting').order_by('uploaded_at')[:limit]):
        if not AssociationDocument.objects.filter(pk=doc.pk, text_status='waiting').update(text_status='reading', read_started_at=now):
            continue
        doc.refresh_from_db()
        try:
            read_document(doc)
            say(f'read {doc.name}: {doc.page_count} pages')
            done += 1
        except Exception as e:
            logger.exception('Reading the document "%s" failed', doc.name)
            doc.read_tries += 1
            doc.text_status = 'failed' if doc.read_tries >= 3 else 'waiting'
            doc.read_note = (str(e) or 'It could not be read.')[:300]
            doc.save(update_fields=['read_tries', 'text_status', 'read_note'])
            say(f'could not read {doc.name}: {doc.read_note}')
    for doc in AssociationDocument.objects.filter(auto_sort=True, sort_status='waiting', text_status__in=['ready', 'partial']):
        sort_document(doc)
        say(f'sorted {doc.name}: {doc.sort_status}')
    return done
