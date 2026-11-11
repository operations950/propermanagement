"""The lines of the old system's general ledger (Yardi), kept as detail behind the figures we carried across as one number. They are not journal entries and change no balance: they only
let the budget screen show what is inside "Jan-Sep, carried from the old system". A Yardi general ledger export (one book, cash or accrual, all properties) is read, checked against what we
carried for every income and expense account, and stored. Loading the same book again replaces what was stored for it."""
import collections
from datetime import date
from decimal import Decimal

from django.db import transaction

from core.models import Property

from .models import Account, JournalLine, LedgerDetail

# the names Yardi's export uses for the associations, as they are called here
ALIASES = {'St. Andrews Grand': 'St Andrews Grand', 'St Andrews GLEN': 'St Andrews Glen', 'La Pensee Condo': 'La Pensee', 'Lakeside Point #8': 'Lakeside Point 8',
           'Lakeside Point Gardens #2': 'Lakeside Point 2', 'Castlewood Town Villas Condominium': 'Castlewood Villas'}
CENT = Decimal('0.01')


class GLError(Exception):
    pass


def read(path):
    """(book, period text, lines) from a Yardi general ledger export. Each line: property, account number and name, date, period, who, control number, reference, debit, credit, remarks."""
    import openpyxl
    book = period = None
    lines, cur = [], None
    for i, r in enumerate(openpyxl.load_workbook(path, read_only=True, data_only=True).worksheets[0].iter_rows(values_only=True), 1):
        first = str(r[0]).strip() if r[0] is not None else ''
        if i <= 5:
            if first.startswith('Book ='):
                book = first.split('=', 1)[1].strip().lower()
            elif first.startswith('Period ='):
                period = first.split('=', 1)[1].strip()
            continue
        if first.isdigit() and r[3] and r[1] is None:
            cur = (first, str(r[3]).strip())
        elif first and hasattr(r[1], 'year'):
            if cur is None:
                raise GLError(f'Row {i}: a line before any account heading.')
            lines.append({'prop': first, 'code': cur[0], 'acct': cur[1], 'date': r[1].date(), 'period': r[2].date().replace(day=1) if hasattr(r[2], 'date') else r[1].date().replace(day=1),
                          'who': str(r[3] or '').strip(), 'ctrl': str(r[4] or '').strip(), 'ref': str(r[5] if r[5] is not None else '').strip(),
                          'debit': Decimal(str(r[6] or 0)).quantize(CENT), 'credit': Decimal(str(r[7] or 0)).quantize(CENT), 'remarks': str(r[9] or '').strip(), 'row': i})
    if book not in ('cash', 'accrual'):
        raise GLError(f'The file does not say which book it is (found {book!r}). Export it with Book = Cash or Book = Accrual.')
    if not lines:
        raise GLError('No ledger lines found in the file.')
    return book, period or '', lines


def association_for(name):
    target = ALIASES.get(name, name)
    prop = Property.objects.filter(property_type=Property.Type.ASSOCIATION, name__iexact=target).first()
    if prop is None:
        raise GLError(f'No association is called "{name}" here. Add it to ALIASES in accounting/gl_detail.py.')
    return prop


def check(book, lines):
    """What the file says against what we hold. Returns {'entries', 'unbalanced': [...], 'missing_accounts': [...], 'differences': [(association, number, name, detail, carried)]}.
    For every income and expense account the lines must add up to the year to date we carried across (the opening entry for that book)."""
    props = {name: association_for(name) for name in {l['prop'] for l in lines}}
    entries = collections.defaultdict(Decimal)
    net = collections.defaultdict(Decimal)
    for l in lines:
        entries[(l['prop'], l['ctrl'])] += l['debit'] - l['credit']
        net[(l['prop'], l['code'])] += l['debit'] - l['credit']
    unbalanced = [(k, v) for k, v in entries.items() if v != 0]
    have = {(a.association_id, a.code): a for a in Account.objects.filter(association__in=props.values())}
    missing = sorted({(p, c) for (p, c) in net if (props[p].pk, c) not in have})
    carried = collections.defaultdict(Decimal)
    for l in JournalLine.objects.filter(account__association__in=props.values(), entry__source='opening', entry__basis__in=('both', book), account__kind__in=('income', 'expense')).select_related('account'):
        carried[(l.account.association_id, l.account.code)] += l.debit - l.credit
    names = {l['code']: l['acct'] for l in lines}
    diffs = []
    for key in sorted(set(carried) | {(props[p].pk, c) for (p, c) in net if c[0] in '456'}):
        pid, code = key
        pname = next((n for n, pr in props.items() if pr.pk == pid), None)
        got = net.get((pname, code), Decimal('0'))
        if got != carried.get(key, Decimal('0')):
            diffs.append((props[pname].name if pname else pid, code, names.get(code, ''), got, carried.get(key, Decimal('0'))))
    return {'props': props, 'entries': len(entries), 'unbalanced': unbalanced, 'missing_accounts': missing, 'differences': diffs}


@transaction.atomic
def load(book, period, lines, batch=''):
    """Store the lines, replacing what was stored for the same book and associations. Returns {association name: lines stored}. Run `check` first."""
    props = {name: association_for(name) for name in {l['prop'] for l in lines}}
    accounts = {(a.association_id, a.code): a for a in Account.objects.filter(association__in=props.values())}
    LedgerDetail.objects.filter(association__in=props.values(), book=book).delete()
    rows, count = [], collections.Counter()
    for l in lines:
        prop = props[l['prop']]
        rows.append(LedgerDetail(association=prop, account=accounts[(prop.pk, l['code'])], book=book, date=l['date'], period=l['period'], who=l['who'][:200], ctrl=l['ctrl'][:30],
                                 reference=l['ref'][:80], debit=l['debit'], credit=l['credit'], remarks=l['remarks'][:500], batch=(batch or period)[:60]))
        count[prop.name] += 1
    LedgerDetail.objects.bulk_create(rows, batch_size=1000)
    return dict(count)
