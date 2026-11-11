"""An association's governing documents as the research source for the association.

Documents are uploaded (one file or many, in any mix) and read ONCE: the words of every page are kept, so nothing is read again to answer a question (see docreader.py).
Claude sorts what was uploaded into the documents it holds (a declaration, articles, bylaws, rules, amendments), and a person confirms. From the kept words it
(1) proposes settings, each with the page and the words it came from, which take effect only when a person confirms them, and (2) answers questions, citing the document and page.
A question sends only the handful of pages most likely to answer it, and a question asked before is answered from memory until the documents change.
With no Claude connection set up, everything else still works.
"""
import hashlib
import logging
import math
import re
from collections import Counter
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db.models import F
from django.utils import timezone

from core.contact_document_import import _file_to_content_blocks  # noqa: F401  (docreader uses it for a picture)

from . import ledger
from datetime import timedelta

from .models import AssociationDocument, DocumentPage, DocumentQuestion, DocumentQuestionRead, SettingProposal

logger = logging.getLogger(__name__)
MODEL = 'claude-sonnet-5'
MAX_DOC_BYTES = 50 * 1024 * 1024
ALLOWED_EXTENSIONS = ('pdf', 'jpg', 'jpeg', 'png', 'gif', 'webp', 'txt', 'csv')
Kind = AssociationDocument.Kind
WANTED = [Kind.DECLARATION, Kind.ARTICLES, Kind.BYLAWS, Kind.RULES, Kind.CONTRACT, Kind.BUDGET]
REQUIRED = (Kind.CONTRACT, Kind.BUDGET)
PAGES_FIRST_TRY, PAGES_SECOND_TRY, PAGE_CHARS = 12, 40, 6000
TAG_WEIGHT = 3          # a page Claude tagged with the question's subject counts this many times a page that merely mentions it
SIGNIFICANT = (Kind.DECLARATION, Kind.ARTICLES, Kind.BYLAWS, Kind.RULES, Kind.AMENDMENT, Kind.CONTRACT)
ASK_AGAIN_DAYS = 90


class DocumentError(Exception):
    pass


# ---------------------------------------------------------------- what is on file

def _sources(association):
    """{kind: [{'doc', 'start', 'end', 'title'}]}: where each kind of document is, whole files that were uploaded as one kind and confirmed parts of sorted files."""
    out = {}
    for d in AssociationDocument.objects.filter(property=association).order_by('uploaded_at'):
        if d.auto_sort:
            if d.sort_status != 'confirmed':
                continue
            for s in d.sections or []:
                if s.get('repeat_of'):
                    continue          # a second copy of a document already counted
                out.setdefault(s['kind'], []).append({'doc': d, 'start': s['start'], 'end': s['end'], 'title': s.get('title') or d.name})
        else:
            out.setdefault(d.kind, []).append({'doc': d, 'start': None, 'end': None, 'title': d.name})
    return out


def _where(src):
    if src['start'] is None:
        return src['doc'].name
    pages = f'page {src["start"]}' if src['start'] == src['end'] else f'pages {src["start"]} to {src["end"]}'
    return f'{src["doc"].name}, {pages}'


def _guess_parent(section):
    """Which document an amendment amends, from its title, when the sorter did not say."""
    title = (section.get('title') or '').lower()
    for kind, word in ((Kind.DECLARATION, 'declaration'), (Kind.ARTICLES, 'articles'), (Kind.BYLAWS, 'bylaw'), (Kind.RULES, 'rule')):
        if word in title:
            return kind
    return ''


def _node(doc, section, role):
    start, end = section.get('start'), section.get('end')
    where = doc.name if start is None else (f'page {start}' if start == end else f'pages {start} to {end}')
    return {'doc': doc, 'kind': section['kind'], 'role': role, 'start': start, 'title': (section.get('title') or doc.name), 'where': where,
            'url': (doc.file.url + (f'#page={start}' if start else '')) if doc.file else '', 'children': [], 'repeat': bool(section.get('repeat_of'))}


def _count(nodes, tally):
    for n in nodes:
        tally[n['role']] = tally.get(n['role'], 0) + 1
        _count(n['children'], tally)
    return tally


def _summary(tally):
    bits = []
    for role, one, many in (('amendment', 'amendment', 'amendments'), ('exhibit', 'exhibit', 'exhibits'), ('copy', 'second copy', 'second copies')):
        if tally.get(role):
            bits.append(f'{tally[role]} {one if tally[role] == 1 else many}')
    return ' · '.join(bits)


def checklist(association):
    """The documents we want on file, each with its status and where it is, as a tree: an amendment sits under the document it amends, an exhibit under the amendment (or
    document) it is attached to, and a second copy under the document it copies. Anything that cannot be placed goes in a last row, 'Needs a look'."""
    rows = {k: {'kind': k, 'label': Kind(k).label, 'required': k in REQUIRED, 'mains': [], 'items': []} for k in WANTED}
    unplaced = []
    for d in AssociationDocument.objects.filter(property=association).order_by('uploaded_at'):
        if d.auto_sort:
            if d.sort_status != 'confirmed':
                continue
            sections = d.sections or []
        else:
            sections = [{'kind': d.kind, 'title': d.name, 'start': None, 'end': None}]
        by_start, anchor = {}, None
        for s in sections:
            kind = s.get('kind')
            if s.get('repeat_of'):
                node = _node(d, s, 'copy')
                parent = by_start.get(s['repeat_of'])
                (parent['children'] if parent is not None and parent['role'] != 'doc' else rows[parent['kind']]['items'] if parent is not None else unplaced).append(node)
                by_start[s['start']] = node
                continue
            if kind in rows:
                node = _node(d, s, 'doc')
                rows[kind]['mains'].append(node)
                anchor = node
            elif kind == Kind.AMENDMENT:
                node = _node(d, s, 'amendment')
                target = s.get('applies_to') or _guess_parent(s)
                (rows[target]['items'] if target in rows else unplaced).append(node)
                anchor = node
            elif kind == Kind.EXHIBIT:
                node = _node(d, s, 'exhibit')
                parent = by_start.get(s.get('parent_page')) or anchor
                if parent is None:
                    unplaced.append(node)
                elif parent['role'] == 'doc':
                    rows[parent['kind']]['items'].append(node)
                else:
                    parent['children'].append(node)
            else:
                node = _node(d, s, 'other')
                unplaced.append(node)
            if s.get('start') is not None:
                by_start[s['start']] = node
    out = []
    for kind in WANTED:
        r = rows[kind]
        first = r['mains'][0] if r['mains'] else None
        tally = _count(r['items'], {})
        out.append({'kind': kind, 'label': r['label'], 'doc': first['doc'] if first else None, 'required': r['required'], 'where': [m['where'] if m['start'] is not None else m['doc'].name for m in r['mains']],
                    'url': first['url'] if first else '', 'count': len(r['mains']), 'items': r['items'], 'summary': _summary(tally), 'main': first['where'] if first and first['start'] is not None else ''})
    if unplaced:
        out.append({'kind': 'unplaced', 'label': 'Needs a look', 'doc': None, 'required': False, 'where': [], 'url': '', 'count': 0, 'items': unplaced, 'summary': f'{len(unplaced)} piece{"s" if len(unplaced) != 1 else ""} not placed yet', 'main': '', 'unplaced': True})
    return out


def has_contract(association):
    return bool(_sources(association).get(Kind.CONTRACT))


def files(association):
    """Every uploaded file with what is going on with it, for the screen."""
    out = []
    for d in AssociationDocument.objects.filter(property=association).order_by('-uploaded_at'):
        if d.text_status in ('', 'waiting', 'reading'):
            state = 'reading'
        elif d.text_status == 'failed':
            state = 'failed'
        elif d.auto_sort and d.sort_status in ('proposed', 'manual'):
            state = 'needs_look'
        elif d.auto_sort and d.sort_status == 'waiting':
            state = 'sorting'
        else:
            state = 'ready'
        secs = d.sections if d.auto_sort else []
        summary = ''
        if d.auto_sort and d.sort_status == 'confirmed' and secs:
            docs = sum(1 for s in secs if not s.get('repeat_of') and s.get('kind') in WANTED)
            rest = len(secs) - docs
            summary = f'Sorted into {docs} document{"s" if docs != 1 else ""} and {rest} attachment{"s" if rest != 1 else ""}: see the list above'
        out.append({'doc': d, 'state': state, 'sections': secs, 'summary': summary})
    return out


# ---------------------------------------------------------------- uploading

def _check_file(f):
    name = getattr(f, 'name', '') or ''
    ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    if ext not in ALLOWED_EXTENSIONS:
        raise DocumentError(f'"{name}" is not a PDF, a picture or a text file.')
    if f.size > MAX_DOC_BYTES:
        raise DocumentError(f'"{name}" is too large (the limit is {MAX_DOC_BYTES // (1024 * 1024)} MB).')
    return name.rsplit('.', 1)[0][:200]


def upload(association, kind, uploaded_file, user=None):
    """One file that you say what it is (the way to add a management contract or a budget by hand)."""
    name = _check_file(uploaded_file)
    return AssociationDocument.objects.create(property=association, kind=kind, name=name, file=uploaded_file, uploaded_by=user, text_status='waiting')


def upload_many(association, uploaded_files, user=None):
    """Any number of files, uploaded without saying what they are. Each is read once, then sorted into the documents it holds; a person confirms. Returns ([documents], [problems])."""
    made, problems = [], []
    for f in uploaded_files:
        try:
            name = _check_file(f)
        except DocumentError as e:
            problems.append(str(e))
            continue
        made.append(AssociationDocument.objects.create(property=association, kind=Kind.OTHER, name=name, file=f, uploaded_by=user, text_status='waiting', auto_sort=True, sort_status='waiting'))
    return made, problems


def confirm_sections(doc, choices):
    """A person confirms what the file holds. `choices` is [(kind, title)] in the order of the sections shown."""
    sections = list(doc.sections or [])
    if len(choices) != len(sections):
        raise DocumentError('That list changed. Reload the page.')
    valid = {k for k, _ in Kind.choices}
    for s, choice in zip(sections, choices):
        kind, title = choice[0], choice[1]
        s['kind'] = kind if kind in valid else 'other'
        s['title'] = (title or '').strip()[:160] or s['title']
        applies = choice[2] if len(choice) > 2 else None
        if s['kind'] == 'amendment' and applies in ('declaration', 'articles', 'bylaws', 'rules'):
            s['applies_to'] = applies
        elif s['kind'] != 'amendment':
            s.pop('applies_to', None)
    doc.sections, doc.sort_status = sections, 'confirmed'
    doc.save(update_fields=['sections', 'sort_status'])


def sort_again(doc):
    doc.sort_status = 'waiting'
    doc.save(update_fields=['sort_status'])


def read_again(doc):
    doc.text_status, doc.read_tries, doc.read_note = 'waiting', 0, ''
    if doc.auto_sort:
        doc.sort_status = 'waiting'
    doc.save(update_fields=['text_status', 'read_tries', 'read_note', 'sort_status'])


def source_label(doc, page):
    """'Declaration of Condominium (recorded 1989), page 12' for a page of a file, from how the file was sorted."""
    if doc.auto_sort and doc.sections:
        for s in doc.sections:
            if s['start'] <= (page or 0) <= s['end']:
                return f'{s.get("title") or doc.name}, page {page}'
    return f'{doc.name}, page {page}' if page else doc.name


# ---------------------------------------------------------------- Claude

def _client():
    if not settings.ANTHROPIC_API_KEY:
        raise DocumentError('Reading documents needs the Claude connection, which is not set up yet.')
    import anthropic
    from core.app_settings import sanitized_setting
    return anthropic.Anthropic(api_key=sanitized_setting('ANTHROPIC_API_KEY'))


def _ensure_read(docs):
    """Documents not read yet are queued; asking waits until they are."""
    waiting = [d for d in docs if d.text_status in ('', 'waiting', 'reading')]
    for d in waiting:
        if d.text_status == '':
            AssociationDocument.objects.filter(pk=d.pk, text_status='').update(text_status='waiting', read_tries=0)
    if waiting:
        names = ', '.join(d.name for d in waiting[:3]) + (' and more' if len(waiting) > 3 else '')
        raise DocumentError(f'Still reading {names}. It is read once, which can take a few minutes for a long scan; ask again then.')
    usable = [d for d in docs if d.text_status in ('ready', 'partial')]
    if not usable:
        raise DocumentError('None of the uploaded files could be read. Open the list of files for the reason.')
    return usable


# ---------------------------------------------------------------- finding the pages that matter

STOP = set('the and for are with that this from have will shall may not can you any all per who what when where how does did our their about into than then there been being its is of to in on or an as at by be if a it do get was were has had would could should which these those such upon under over also only more most other some'.split())


def _stem(w):
    for suf in ('ing', 'ed', 'es', 's', 'ly'):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[:-len(suf)]
    return w


def _terms(text):
    return [_stem(w) for w in re.findall(r'[a-z]{3,}', (text or '').lower()) if w not in STOP]


def rank_pages(pages, query, k):
    """The k pages most likely to answer `query`, with the page after and before the best three, in file order. `pages` are DocumentPage rows."""
    q = set(_terms(query))
    if not pages:
        return []
    tfs, df, tag_sets, tag_df = [], Counter(), [], Counter()
    for p in pages:
        tf = Counter(_terms(p.text))
        tfs.append(tf)
        for t in tf:
            df[t] += 1
        tags = set(_terms(getattr(p, 'tags', '') or ''))          # what Claude noted the page is about, once, when it was read
        tag_sets.append(tags)
        for t in tags:
            tag_df[t] += 1
    n = len(pages)
    scored = []
    for i, tf in enumerate(tfs):
        s = sum((1 + math.log(tf[t])) * math.log(1 + n / df[t]) for t in q if t in tf)
        s += TAG_WEIGHT * sum(math.log(1 + n / tag_df[t]) for t in q if t in tag_sets[i])
        if s > 0:
            scored.append((s, i))
    scored.sort(reverse=True)
    chosen = {i for _, i in scored[:k]}
    index = {(p.document_id, p.number): i for i, p in enumerate(pages)}
    for _, i in scored[:3]:
        p = pages[i]
        for neighbour in (p.number - 1, p.number + 1):
            j = index.get((p.document_id, neighbour))
            if j is not None:
                chosen.add(j)
    return [pages[i] for i in sorted(chosen)]


def _page_text(pages):
    return '\n\n'.join(f'=== {source_label(p.document, p.number)} ===\n{p.text[:PAGE_CHARS]}' for p in pages)


# ---------------------------------------------------------------- ask

def _norm_question(q):
    return ' '.join(re.sub(r'[^a-z0-9 ]', ' ', (q or '').lower()).split())


def _signature(docs):
    return hashlib.sha1('|'.join(f'{d.pk}:{d.page_count}:{d.text_status}' for d in sorted(docs, key=lambda d: d.pk)).encode()).hexdigest()


def significant_signature(association):
    """Which documents that could change an answer are on file: the declaration, articles, bylaws, rules, amendments and the contract (not exhibits, repeats or the budget)."""
    parts = []
    for d in AssociationDocument.objects.filter(property=association).order_by('pk'):
        if d.auto_sort:
            if d.sort_status != 'confirmed':
                continue
            parts += [f'{d.pk}:{s.get("start")}-{s.get("end")}:{s.get("kind")}' for s in d.sections or [] if s.get('kind') in SIGNIFICANT and not s.get('repeat_of')]
        elif d.kind in SIGNIFICANT:
            parts.append(f'{d.pk}:{d.kind}')
    return hashlib.sha1('|'.join(parts).encode()).hexdigest()


def staleness(q, current_sig=None, now=None):
    """Why an answer deserves to be asked again: 'documents' (a document that matters was added or changed since), 'age' (over 90 days: the law may have moved), or ''."""
    now = now or timezone.now()
    if q.sig_significant and current_sig and q.sig_significant != current_sig:
        return 'documents'
    if now - (q.answered_at or q.created_at) > timedelta(days=ASK_AGAIN_DAYS):
        return 'age'
    return ''


def ask(association, question, user=None, force=False):
    """Answer from the pages most likely to answer. A question asked before is shown from memory, free, with a note when it is worth asking again; `force` asks Claude again.
    Returns the DocumentQuestion with `from_memory` set (no Claude call was made)."""
    question = (question or '').strip()
    if not question:
        raise DocumentError('Type a question.')
    docs = list(AssociationDocument.objects.filter(property=association))
    if not docs:
        raise DocumentError('Upload a document first: there is nothing to read yet.')
    usable = _ensure_read(docs)
    sig, sig_sig = _signature(usable), significant_signature(association)
    wanted = _norm_question(question)
    old = next((o for o in DocumentQuestion.objects.filter(property=association).order_by('-created_at')[:300] if _norm_question(o.question) == wanted and o.answer), None)
    if old is not None and not force:
        old.asks += 1
        old.save(update_fields=['asks'])
        old.from_memory = True
        return old
    client = _client()
    pages = list(DocumentPage.objects.filter(document__in=usable).select_related('document').order_by('document_id', 'number'))
    skip = {d.pk: {n for s in (d.sections or []) if d.sort_status == 'confirmed' and (s.get('repeat_of') or s.get('kind') == 'exhibit') for n in range(s['start'], s['end'] + 1)} for d in usable if d.auto_sort}
    pages = [p for p in pages if p.number not in skip.get(p.document_id, ())]      # not a second copy, and not the drawings
    answer, chosen = '', []
    for k in (PAGES_FIRST_TRY, PAGES_SECOND_TRY):
        chosen = rank_pages(pages, question, k)
        if not chosen:
            break
        try:
            message = client.messages.create(
                model=MODEL, max_tokens=1500,
                messages=[{'role': 'user', 'content': [{'type': 'text', 'text': _page_text(chosen) + '\n\n' +
                    'You answer questions about one condominium or homeowners association using ONLY the pages above. Answer in plain words, briefly. '
                    'After the answer give the source: the document name and the page number(s) as they are labelled above, and the exact words that support it. '
                    'If these pages do not answer the question, reply with exactly NOT_FOUND and nothing else; do not guess.\n\nQuestion: ' + question}]}])
        except Exception:
            logger.exception('Claude could not answer a question about the documents')
            raise DocumentError('The documents could not be read right now. Try again in a minute.')
        answer = ''.join(b.text for b in message.content if getattr(b, 'type', '') == 'text').strip()
        if not answer.upper().startswith('NOT_FOUND'):
            break
        answer = ''
    text = answer or "The documents don't say. I looked through the pages most likely to, in every file on record."
    sources = '\n'.join(source_label(p.document, p.number) for p in chosen)
    now = timezone.now()
    if old is not None:
        old.answer, old.docs_signature, old.sig_significant, old.sources, old.answered_at, old.asks = text, sig, sig_sig, sources, now, old.asks + 1
        old.save(update_fields=['answer', 'docs_signature', 'sig_significant', 'sources', 'answered_at', 'asks'])
        q = old
    else:
        q = DocumentQuestion.objects.create(property=association, question=question, answer=text, docs_signature=sig, sig_significant=sig_sig, sources=sources, answered_at=now, asked_by=user)
    q.from_memory = False
    return q


def qa_payload(association, limit=300):
    """Every answered question, for the screen to filter and rank as the person types: most read and asked first."""
    sig = significant_signature(association)
    now = timezone.now()
    rows = []
    for q in DocumentQuestion.objects.filter(property=association).exclude(answer='')[:limit]:
        when = q.answered_at or q.created_at
        rows.append({'id': q.pk, 'q': q.question, 'a': q.answer, 'sources': [s for s in (q.sources or '').split('\n') if s], 'reads': q.reads, 'asks': q.asks,
                     'stale': staleness(q, sig, now), 'when': f'{when:%b} {when.day}, {when.year}'})
    rows.sort(key=lambda r: (-(r['reads'] + r['asks']), r['q']))
    return rows


def record_read(question, user):
    """Someone sat with this answer on screen: count it once for that person that day."""
    _, new = DocumentQuestionRead.objects.get_or_create(question=question, user=user, day=timezone.localdate())
    if new:
        DocumentQuestion.objects.filter(pk=question.pk).update(reads=F('reads') + 1, last_read_at=timezone.now())
    return new


# ---------------------------------------------------------------- proposals

PROPOSE_TOOL = {
    'name': 'report_settings',
    'description': 'Report the settings found in the document, each with the page and the exact words.',
    'input_schema': {'type': 'object', 'properties': {'settings': {'type': 'array', 'items': {'type': 'object', 'properties': {
        'key': {'type': 'string', 'enum': ['late_fee', 'grace_days', 'interest_rate', 'spending_limit', 'management_fee']},
        'late_fee_kind': {'type': 'string', 'enum': ['flat', 'percent', 'greater'], 'description': 'For late_fee: a flat amount, a percent of the installment, or the greater of the two.'},
        'flat': {'type': ['number', 'null'], 'description': 'A dollar amount: the flat late fee, the spending limit, or the monthly management fee.'},
        'percent': {'type': ['number', 'null'], 'description': 'A percent: the late-fee percent, or the annual interest rate.'},
        'days': {'type': ['integer', 'null'], 'description': 'For grace_days: days after the due date before the fee applies.'},
        'page': {'type': ['integer', 'null'], 'description': 'The page number as labelled above.'}, 'quote': {'type': 'string', 'description': 'The exact words in the document.'}},
        'required': ['key', 'quote']}}}, 'required': ['settings']},
}
SETTINGS_QUERY = 'late fee late charge delinquent assessment interest grace days management fee monthly fee spend spending limit authority approval board expenditure'


def propose_from_document(doc, user=None):
    """Read one document (from its kept words) for late fee, grace period, interest, spending limit and management fee. Saves proposals; nothing is applied."""
    usable = _ensure_read([doc])
    client = _client()
    pages = list(DocumentPage.objects.filter(document=doc).select_related('document').order_by('number'))
    chosen = pages if len(pages) <= 25 else rank_pages(pages, SETTINGS_QUERY, 20)
    try:
        message = client.messages.create(
            model=MODEL, max_tokens=2500, tools=[PROPOSE_TOOL], tool_choice={'type': 'tool', 'name': 'report_settings'},
            messages=[{'role': 'user', 'content': [{'type': 'text', 'text': _page_text(chosen) + '\n\n' +
                'From these pages find, only if they clearly state them: the late fee on a late assessment (flat, percent, or the greater of the two), the grace period in days, the annual '
                'interest rate on unpaid assessments, the amount the manager may spend without board approval, and the monthly management fee. For each give the page and the exact words. '
                'Omit anything the pages do not say.'}]}])
    except Exception:
        logger.exception('Claude could not read a document for settings')
        raise DocumentError('The document could not be read right now. Try again in a minute.')
    block = next((b for b in message.content if b.type == 'tool_use'), None)
    found = (block.input.get('settings') if block else None) or []
    SettingProposal.objects.filter(property=doc.property, document=doc, status=SettingProposal.Status.PROPOSED).delete()
    made = []
    for f in found:
        key = f.get('key')
        if key == 'late_fee':
            value = {'kind': f.get('late_fee_kind') or 'flat', 'flat': f.get('flat'), 'percent': f.get('percent')}
        elif key == 'grace_days':
            value = {'days': f.get('days')}
        elif key == 'interest_rate':
            value = {'percent': f.get('percent')}
        elif key in ('spending_limit', 'management_fee'):
            value = {'amount': f.get('flat')}
        else:
            continue
        if all(v in (None, '') for k, v in value.items() if k != 'kind'):
            continue
        made.append(SettingProposal.objects.create(property=doc.property, key=key, value=value, document=doc, page=f.get('page'), quote=(f.get('quote') or '')[:1000]))
    return made


def describe(proposal):
    v = proposal.value
    if proposal.key == 'late_fee':
        flat, pct, kind = v.get('flat'), v.get('percent'), v.get('kind')
        if kind == 'flat':
            return f'A late fee of ${Decimal(str(flat)):,.2f}'
        if kind == 'percent':
            return f'A late fee of {pct}% of the installment'
        return f'A late fee of ${Decimal(str(flat or 0)):,.2f} or {pct}% of the installment, whichever is greater'
    if proposal.key == 'grace_days':
        return f'{v.get("days")} days of grace after the due date'
    if proposal.key == 'interest_rate':
        return f'{v.get("percent")}% a year on unpaid assessments'
    if proposal.key == 'spending_limit':
        return f'The manager may spend up to ${Decimal(str(v.get("amount"))):,.2f} without the board'
    return f'A management fee of ${Decimal(str(v.get("amount"))):,.2f} a month'


LABELS = {'late_fee': 'Late fee', 'grace_days': 'Grace period', 'interest_rate': 'Interest on unpaid assessments', 'spending_limit': 'Spending limit without the board', 'management_fee': 'Management fee'}


def _dec(value):
    try:
        return Decimal(str(value)).quantize(Decimal('0.01'))
    except (InvalidOperation, TypeError):
        raise DocumentError('That value is not a number.')


def apply_setting(profile, key, value):
    """Put a confirmed setting on the association."""
    if key == 'late_fee':
        profile.late_fee_kind = value.get('kind') or 'flat'
        profile.late_fee_flat = _dec(value['flat']) if value.get('flat') not in (None, '') else None
        profile.late_fee_percent = Decimal(str(value['percent'])) if value.get('percent') not in (None, '') else None
        profile.late_fee_confirmed = True
    elif key == 'grace_days':
        profile.late_fee_grace_days = int(value['days'])
    elif key == 'interest_rate':
        profile.interest_rate = Decimal(str(value['percent']))
    elif key == 'spending_limit':
        profile.spending_limit = _dec(value['amount'])
    elif key == 'management_fee':
        profile.management_fee = _dec(value['amount'])
    else:
        raise DocumentError('Unknown setting.')
    profile.save()


def confirm(proposal, user=None, value=None):
    apply_setting(ledger.profile_for(proposal.property), proposal.key, value or proposal.value)
    if value:
        proposal.value = value
    proposal.status, proposal.decided_by, proposal.decided_at = SettingProposal.Status.CONFIRMED, user, timezone.now()
    proposal.save()


def dismiss(proposal, user=None):
    proposal.status, proposal.decided_by, proposal.decided_at = SettingProposal.Status.DISMISSED, user, timezone.now()
    proposal.save()
