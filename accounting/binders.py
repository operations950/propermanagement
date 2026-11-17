"""A big PDF of governing documents, sorted by what is in it and saved as one file per document.

The file storage refuses a file over about 10 MB, and a big upload is nearly always several documents joined together (declaration, articles, bylaws, amendments, exhibits). Cutting it
every 100 pages would jumble them, so a big PDF goes through a BINDER:

  1. It is cut into hidden STAGING PARTS under the limit (documents.split_pdf). They are scaffolding: they are not documents of the association and appear in no list.
  2. Every part is read once (accounting/docreader.py); then the whole binder is SORTED AS ONE FILE, with page numbers running straight through, so Claude proposes where each document starts and ends.
  3. A second copy of a document is found by comparing the words of the pages, not only by Claude's say-so. A copy is left out (not saved twice); the person can say "keep it anyway".
  4. The person confirms on the screen they already know.
  5. FINALIZE: each confirmed document becomes its own file with its own kind and title, built from the staging parts' pages. Exhibits and surveys stay inside the file of the document they belong to,
     each amendment is its own file, leftover pages are grouped as "Other pages". The words already read, and the notes on what each page is about, are carried over: nothing is read or billed twice.
     A single document that is itself too big is cut into "part 1 of 2" at page boundaries. The staging parts are deleted last, and only when every new file is saved.

The same re-cut is used for a file that fit under the limit, was sorted and confirmed, and that the person wants as separate documents (split_sorted).
Part 0 of a binder carries the state (binder_state), the proposed sections (sections) and the original name (binder_name).
"""
import io
import logging
import re
import uuid
from collections import Counter
from datetime import timedelta
from types import SimpleNamespace

from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from . import docreader, documents
from .documents import DocumentError
from .models import AssociationDocument, DocumentPage

logger = logging.getLogger(__name__)
Kind = AssociationDocument.Kind

COPY_SIMILARITY = 0.95          # the share of the words two documents have in common for the second to be called a copy of the first
SHORT_WORDS, SHORT_SIMILARITY = 1000, 0.99      # a short document differs from its sibling by a date or a name: it must match nearly exactly to be called a copy
HINTED_SIMILARITY = 0.85        # Claude said it is a copy and the words mostly agree
MIN_WORDS_FOR_COPY = 150        # a few lines (or a scanned drawing) are never called a copy
LENGTH_RATIO = 0.9
STALE_FINALIZING = timedelta(minutes=15)
MAIN_KINDS = (Kind.DECLARATION, Kind.ARTICLES, Kind.BYLAWS, Kind.RULES, Kind.CONTRACT, Kind.BUDGET)
APPLIES_WORD = {'declaration': 'Declaration', 'articles': 'Articles', 'bylaws': 'Bylaws', 'rules': 'Rules and regulations'}
STATE_FOR_SCREEN = {'reading': 'reading', 'sorting': 'sorting', 'needs_confirm': 'needs_look', 'finalizing': 'sorting', 'failed': 'failed'}


# ---------------------------------------------------------------- the binder

def parts_of(binder_id):
    return list(AssociationDocument.objects.filter(binder_id=binder_id, is_staging=True).order_by('binder_order'))


def head_of(binder_id):
    return AssociationDocument.objects.filter(binder_id=binder_id, is_staging=True, binder_order=0).first()


def start_binder(association, uploaded_file, name, user=None):
    """Hold a big PDF as hidden staging parts of one binder. Returns part 0, which carries the state."""
    uploaded_file.seek(0)
    cut = documents.split_pdf(uploaded_file.read())
    binder_id, total, made = str(uuid.uuid4()), len(cut), []
    try:
        for i, (first, last, blob) in enumerate(cut):
            label = name if i == 0 else f'{name} (part {i + 1} of {total})'
            made.append(documents._store(association, Kind.OTHER, label, ContentFile(blob, name=f'{_safe(name)} part {i + 1}.pdf'), user,
                                         binder_id=binder_id, binder_order=i, binder_name=name, is_staging=True, binder_state='reading' if i == 0 else ''))
    except Exception:
        _drop(made)
        raise
    return made[0]


def _safe(name):
    return re.sub(r'[^A-Za-z0-9 ._()-]+', '', name or 'document').strip()[:80] or 'document'


def _drop(docs):
    """Remove documents and their stored files (best effort for the files)."""
    for d in docs:
        try:
            if d.file:
                d.file.delete(save=False)
        except Exception:
            logger.warning('A stored file could not be removed: %s', getattr(d.file, 'name', ''))
        if d.pk:
            d.delete()


def cancel(head):
    """Throw a binder away: every staging part and its file."""
    _drop(parts_of(head.binder_id))


# ---------------------------------------------------------------- reading and sorting (called by the background job)

def process(say=None):
    """Move every binder along one step: reading finished -> sorting -> proposed (waiting for the person); a save that stopped halfway is made ready to try again."""
    say = say or (lambda s: None)
    now = timezone.now()
    for head in AssociationDocument.objects.filter(is_staging=True, binder_order=0, binder_state__in=['reading', 'sorting', 'finalizing']):
        if head.binder_state == 'finalizing':
            if head.read_started_at and head.read_started_at < now - STALE_FINALIZING:
                head.binder_state, head.read_note = 'needs_confirm', 'Saving the documents stopped halfway. Press the button again.'
                head.save(update_fields=['binder_state', 'read_note'])
            continue
        parts = parts_of(head.binder_id)
        if head.binder_state == 'reading':
            if any(p.text_status == 'failed' for p in parts):
                bad = next(p for p in parts if p.text_status == 'failed')
                head.binder_state, head.read_note = 'failed', (bad.read_note or 'A part of it could not be read.')[:300]
                head.save(update_fields=['binder_state', 'read_note'])
                say(f'could not read {head.binder_name}')
                continue
            if not all(p.text_status in ('ready', 'partial') for p in parts):
                continue
            head.binder_state = 'sorting'
            head.save(update_fields=['binder_state'])
        try:
            sort_binder(head)
            say(f'sorted {head.binder_name}: {head.sort_status}')
        except Exception as e:
            logger.exception('Sorting the big upload "%s" failed', head.binder_name)
            head.binder_state, head.read_note = 'failed', (str(e) or 'It could not be sorted.')[:300]
            head.save(update_fields=['binder_state', 'read_note'])


def combined_pages(parts):
    """Every page of the binder as one run, numbered 1..N: SimpleNamespace(number, text, by_claude, tags, part, local)."""
    out, offset = [], 0
    for part in parts:
        rows = list(DocumentPage.objects.filter(document=part).order_by('number'))
        for r in rows:
            out.append(SimpleNamespace(number=offset + r.number, text=r.text, by_claude=r.by_claude, tags=r.tags, part=part, local=r.number - 1))
        offset += max(part.page_count or 0, len(rows))
    return out


def sort_binder(head):
    """Sort the whole binder as one file and flag the second copies. Leaves part 0 'needs_confirm' with the proposed sections."""
    pages = combined_pages(parts_of(head.binder_id))
    sections, status = docreader.sort_pages(pages, head.binder_name)
    head.sections = mark_copies(sections, {p.number: p.text for p in pages})
    head.sort_status = status
    head.binder_state = 'needs_confirm'
    head.read_note = 'This file could not be sorted automatically. Say what it holds.' if status == 'manual' else ''
    head.save(update_fields=['sections', 'sort_status', 'binder_state', 'read_note'])


# ---------------------------------------------------------------- second copies

def _bag(texts):
    words = []
    for t in texts:
        words += re.findall(r'[a-z0-9]+', (t or '').lower())
    return Counter(words), len(words)


def similarity(bag_a, n_a, bag_b, n_b):
    """The share of the words two runs of pages have in common (0 to 1): words are counted, not ordered, so a scan's wrong letters cost a little and a rewritten page costs a lot."""
    if not n_a or not n_b:
        return 0.0, 0.0
    shared = sum((bag_a & bag_b).values())
    return shared / max(n_a, n_b), min(n_a, n_b) / max(n_a, n_b)


def mark_copies(sections, texts):
    """Mark the sections that are a second copy of an earlier one, judged by the words on their pages (`texts` is {page number: text}). A copy gets leave_out=True, dup_of (the start page
    of the original), dup_title and dup_score (a whole percent); the person can say 'keep it anyway'. Surveys and drawings (exhibits) and other pages are never judged. The input is not changed."""
    sections = [dict(s) for s in sections]
    bags = {}
    for i, s in enumerate(sections):
        if s.get('kind') in ('other', 'exhibit'):
            continue
        bags[i] = _bag(texts.get(n, '') for n in range(s['start'], s['end'] + 1))
    for j, s in enumerate(sections):
        for key in ('leave_out', 'dup_of', 'dup_title', 'dup_score', 'keep'):
            s.pop(key, None)
        if j not in bags or bags[j][1] < MIN_WORDS_FOR_COPY:
            continue
        hinted = bool(s.get('repeat_of'))
        best = None
        for i in range(j):
            if i not in bags or sections[i].get('leave_out'):
                continue
            sim, ratio = similarity(bags[i][0], bags[i][1], bags[j][0], bags[j][1])
            if best is None or sim > best[0]:
                best = (sim, ratio, i)
        if best is None:
            continue
        sim, ratio, i = best
        words = max(bags[i][1], bags[j][1])
        need = HINTED_SIMILARITY if hinted else (SHORT_SIMILARITY if words < SHORT_WORDS else COPY_SIMILARITY)
        if sim >= need and ratio >= (0.8 if hinted else LENGTH_RATIO):
            s['leave_out'], s['dup_of'], s['dup_title'], s['dup_score'] = True, sections[i]['start'], sections[i].get('title') or 'an earlier document', int(round(sim * 100))
    return sections


# ---------------------------------------------------------------- what the files will be

def _title(s):
    title = (s.get('title') or '').strip() or Kind(s['kind']).label
    if s['kind'] == Kind.AMENDMENT:
        word = APPLIES_WORD.get(s.get('applies_to') or '')
        if word and word.split()[0].lower() not in title.lower():
            title = f'Amendment to the {word}: {title}'
        elif not word and 'amend' not in title.lower():
            title = f'Amendment: {title}'
    if s.get('dup_of') and s.get('keep') and 'copy' not in title.lower():
        title += ' (second copy)'
    return title[:190]


def plan_files(sections):
    """[{'kind', 'title', 'applies_to', 'pages': [page numbers in order]}] and the left-out copies [{'start', 'end', 'of', 'score'}].
    One file for each declaration, articles, bylaws, rules, contract and budget; one for each amendment; an exhibit or survey goes inside the file of the document it belongs to (named by its parent
    page, else the document before it); a run of 'other' pages is one 'Other pages' file; a copy that was not kept is left out, with the exhibits that belong to it."""
    groups, left_out, by_start, anchor, other_run = [], [], {}, None, None
    for s in sections:
        pages = list(range(s['start'], s['end'] + 1))
        kind = s['kind']
        if s.get('leave_out') and not s.get('keep'):
            left_out.append({'start': s['start'], 'end': s['end'], 'of': s.get('dup_title') or 'an earlier document', 'score': s.get('dup_score') or 0})
            by_start[s['start']], anchor, other_run = 'skip', 'skip', None
            continue
        if kind == 'exhibit':
            target = by_start.get(s.get('parent_page'), anchor)
            if target == 'skip':
                continue
            if target is not None:
                groups[target]['pages'] += pages
                other_run = None
                continue
            kind = 'other'
        if kind == 'other':
            if other_run is not None:
                groups[other_run]['pages'] += pages
            else:
                groups.append({'kind': 'other', 'title': 'Other pages', 'applies_to': '', 'pages': pages})
                other_run = len(groups) - 1
            continue
        groups.append({'kind': kind, 'title': _title(s), 'applies_to': s.get('applies_to') or '', 'pages': pages})
        by_start[s['start']] = anchor = len(groups) - 1
        other_run = None
    others = [g for g in groups if g['kind'] == 'other']
    if len(others) > 1:
        for g in others:
            g['title'] = f'Other pages (pages {g["pages"][0]}-{g["pages"][-1]})'
    return groups, left_out


# ---------------------------------------------------------------- the re-cut

def _cut_and_store(association, user, plan, pages, created):
    """Build each planned file from the pages and store it, carrying the words and notes over. Appends every stored document to `created` (so a failure can remove them)."""
    from pypdf import PdfReader, PdfWriter
    by_number = {p.number: p for p in pages}
    readers = {}

    def reader_for(part):
        if part.pk not in readers:
            readers[part.pk] = PdfReader(io.BytesIO(docreader._open(part)))
        return readers[part.pk]

    partial = any(p.part.text_status == 'partial' for p in pages)
    for g in plan:
        writer = PdfWriter()
        for n in g['pages']:
            src = by_number[n]
            writer.add_page(reader_for(src.part).pages[src.local])
        buf = io.BytesIO()
        writer.write(buf)
        blob = buf.getvalue()
        chunks = [(1, len(g['pages']), blob)] if len(blob) <= documents.STORE_LIMIT else documents.split_pdf(blob)
        total = len(chunks)
        for ci, (first, last, data) in enumerate(chunks, 1):
            name = g['title'] if total == 1 else f'{g["title"]} (part {ci} of {total})'
            doc = documents._store(association, g['kind'], name, ContentFile(data, name=f'{_safe(name)}.pdf'), user, text_status='partial' if partial else 'ready')
            created.append(doc)
            rows = []
            for local_no in range(first, last + 1):
                src = by_number[g['pages'][local_no - 1]]
                rows.append(DocumentPage(document=doc, number=local_no - first + 1, text=src.text, by_claude=src.by_claude, tags=src.tags))
            DocumentPage.objects.bulk_create(rows)
            doc.page_count = len(rows)
            doc.save(update_fields=['page_count'])
    return created


def finalize(head, user=None):
    """Save a confirmed binder as one file per document, then delete the staging parts. Returns (documents, left_out copies). Only one save runs at a time; a failure leaves every
    staging part in place with the state 'failed' and a plain message, and the person can press the button again."""
    claimed = AssociationDocument.objects.filter(pk=head.pk, binder_state__in=['needs_confirm', 'failed']).update(binder_state='finalizing', read_started_at=timezone.now(), read_note='')
    if not claimed:
        raise DocumentError('That upload is already being saved, or is not ready to be saved.')
    head.refresh_from_db()
    parts = parts_of(head.binder_id)
    created = []
    try:
        plan, left_out = plan_files(head.sections or [])
        if not plan:
            raise DocumentError('Nothing is left to save: every part was left out.')
        with transaction.atomic():
            _cut_and_store(head.property, user, plan, combined_pages(parts), created)
    except Exception as exc:
        _drop(created)
        logger.exception('Saving the big upload "%s" failed', head.binder_name)
        note = str(exc) if isinstance(exc, DocumentError) else 'The documents could not be saved just now. Nothing was lost; press the button again.'
        AssociationDocument.objects.filter(pk=head.pk).update(binder_state='failed', read_note=note[:300])
        raise DocumentError(note)
    _drop(parts)
    return created, left_out


def confirm(head, choices, keeps, user=None):
    """The person confirms the proposed documents. `choices` is [(kind, title, applies_to)] in the order shown, `keeps` the numbers of the copies to keep anyway. Then it is saved."""
    sections = [dict(s) for s in head.sections or []]
    if len(choices) != len(sections):
        raise DocumentError('That list changed. Reload the page.')
    valid = {k for k, _ in Kind.choices}
    for i, (s, choice) in enumerate(zip(sections, choices)):
        kind, title = choice[0], choice[1]
        s['kind'] = kind if kind in valid else 'other'
        s['title'] = (title or '').strip()[:160] or s['title']
        applies = choice[2] if len(choice) > 2 else None
        if s['kind'] == 'amendment' and applies in APPLIES_WORD:
            s['applies_to'] = applies
        elif s['kind'] != 'amendment':
            s.pop('applies_to', None)
        if s.get('leave_out'):
            s['keep'] = i in keeps
    head.sections = sections
    head.save(update_fields=['sections'])
    return finalize(head, user)


def sort_again(head):
    head.binder_state, head.read_note = 'sorting', ''
    head.save(update_fields=['binder_state', 'read_note'])


# ---------------------------------------------------------------- a file that fit under the limit

def split_sorted(doc, user=None):
    """Make a sorted and confirmed file into separate documents, one per document it holds (same rules as a big upload; a second copy is left out). The original is removed only after every
    new file is saved. Returns (documents, left_out copies)."""
    if doc.is_staging or not doc.auto_sort or doc.sort_status != 'confirmed' or doc.text_status not in ('ready', 'partial'):
        raise DocumentError('Sort the file and confirm what it holds first.')
    rows = list(DocumentPage.objects.filter(document=doc).order_by('number'))
    pages = [SimpleNamespace(number=r.number, text=r.text, by_claude=r.by_claude, tags=r.tags, part=doc, local=r.number - 1) for r in rows]
    sections = mark_copies(doc.sections or [], {p.number: p.text for p in pages})
    for s in sections:
        s.pop('repeat_of', None)
    plan, left_out = plan_files(sections)
    if len(plan) < 2 and not left_out:
        raise DocumentError('This file holds one document, so there is nothing to separate.')
    created = []
    try:
        with transaction.atomic():
            _cut_and_store(doc.property, user, plan, pages, created)
    except Exception as exc:
        _drop(created)
        logger.exception('Splitting "%s" failed', doc.name)
        raise DocumentError(str(exc) if isinstance(exc, DocumentError) else 'It could not be split just now. The original file is untouched.')
    _drop([doc])
    return created, left_out


# ---------------------------------------------------------------- for the screen

def rows_for(association):
    """One row for each big upload that is still being sorted or waits for a person, in the shape of documents.files(): {'doc' (part 0), 'state', 'sections', 'summary', 'binder', 'parts'}."""
    out = []
    for head in AssociationDocument.objects.filter(property=association, is_staging=True, binder_order=0).order_by('-uploaded_at'):
        sections = head.sections or []
        copies = [s for s in sections if s.get('leave_out')]
        summary = f'Read as one file of {sum(p.page_count or 0 for p in parts_of(head.binder_id))} pages, in {AssociationDocument.objects.filter(binder_id=head.binder_id).count()} parts.'
        out.append({'doc': head, 'state': STATE_FOR_SCREEN.get(head.binder_state, 'reading'), 'sections': sections, 'summary': summary, 'binder': True,
                    'saving': head.binder_state == 'finalizing', 'copies': len(copies)})
    return out
