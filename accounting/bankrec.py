"""Bank statements and the reconciliation.

The statement's lines are lined up with the ledger's lines on the same bank account, surest matches first, and whatever is left is for a person:
  1. a check, by its number;
  2. one to one, by amount, then by who was paid and how and when;
  3. several bank lines that add up to one ledger line (six power-company debits for one bill);
  4. several ledger lines that add up to one bank line (two owners' checks in one deposit);
  5. any few bank lines that add up to one ledger line (a journal entry that lumped checks and interest together).
What cannot be lined up is either a check or deposit that simply has not reached the bank yet (a person marks it as timing, and it carries to next month),
or a bank line the books do not have yet (a person adds it to the books, one click).

The proof: statement balance, less what has not cleared, plus deposits on their way, must equal the books, plus anything the bank has that the books do not.
"""
import base64
import csv
import io
import logging
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from itertools import combinations

from django.conf import settings
from django.db import transaction

from . import charges, ledger
from .models import (ZERO, BankAccount, BankLine, BankStatement, JournalEntry, JournalLine, Payment, ReconAcceptance, ReconLedgerItem, ReconMatch, Reconciliation, Receipt)

logger = logging.getLogger(__name__)
STATEMENT_MODEL = 'claude-sonnet-5'
STALE_DAYS = 30
STOP = {'the', 'and', 'inc', 'llc', 'co', 'corp', 'payment', 'pymt', 'purchase', 'sale', 'web', 'ach', 'debit', 'credit', 'direct', 'customer', 'id', 'for', 'from'}


class StatementError(Exception):
    pass


# ---------------------------------------------------------------- reading a statement

def _money(value):
    s = str(value).strip().replace('$', '').replace(',', '')
    neg = s.startswith('(') and s.endswith(')')
    s = s.strip('()')
    if s.startswith('-'):
        neg, s = True, s[1:]
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return -d if neg else d


def _date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    for fmt in ('%m/%d/%Y', '%Y-%m-%d', '%m/%d/%y', '%m-%d-%Y'):
        try:
            return datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            continue
    return None


def kind_for(description, amount, check_number=''):
    d = (description or '').lower()
    if check_number or re.search(r'\bcheck\b|\bchk\b', d):
        return BankLine.Kind.CHECK
    if 'interest' in d and amount > 0:
        return BankLine.Kind.INTEREST
    if re.search(r'fee|service charge|analysis', d) and amount < 0:
        return BankLine.Kind.FEE
    return BankLine.Kind.DEPOSIT if amount > 0 else BankLine.Kind.WITHDRAWAL


def read_csv_statement(data):
    """{'lines': [...], 'end_balance': Decimal or None, 'begin_balance': None, 'period_end': date or None} from a bank's CSV download."""
    rows = list(csv.reader(io.StringIO(data.decode('utf-8-sig', errors='replace'))))
    head = next((i for i, r in enumerate(rows) if any('date' in c.lower() for c in r) and any(w in ' '.join(r).lower() for w in ('amount', 'debit', 'credit', 'withdraw', 'deposit'))), None)
    if head is None:
        raise StatementError('The CSV needs a header row with a date column and an amount (or debit and credit) column.')
    names = [c.strip().lower() for c in rows[head]]
    col = lambda *words: next((i for i, n in enumerate(names) if any(w in n for w in words)), None)
    di, ti, ai, dri, cri, ci, bi = col('date'), col('description', 'memo', 'detail', 'payee'), col('amount'), col('debit', 'withdraw'), col('credit', 'deposit'), col('check'), col('balance')
    lines, last_balance, last_date = [], None, None
    for r in rows[head + 1:]:
        if di is None or di >= len(r):
            continue
        day = _date(r[di])
        if day is None:
            continue
        if ai is not None and ai < len(r) and _money(r[ai]) is not None:
            amount = _money(r[ai])
        else:
            debit = _money(r[dri]) if dri is not None and dri < len(r) and r[dri].strip() else None
            credit = _money(r[cri]) if cri is not None and cri < len(r) and r[cri].strip() else None
            amount = (credit or ZERO) - abs(debit or ZERO)
        number = r[ci].strip() if ci is not None and ci < len(r) else ''
        desc = r[ti].strip() if ti is not None and ti < len(r) else ''
        lines.append({'date': day, 'description': desc or 'Bank line', 'amount': amount, 'kind': kind_for(desc, amount, number), 'check_number': number})
        if bi is not None and bi < len(r) and _money(r[bi]) is not None and (last_date is None or day >= last_date):
            last_balance, last_date = _money(r[bi]), day
    if not lines:
        raise StatementError('No transactions were found in that CSV.')
    return {'lines': lines, 'end_balance': last_balance, 'begin_balance': None, 'period_end': max(l['date'] for l in lines)}


STATEMENT_TOOL = {
    'name': 'report_statement',
    'description': 'Report every transaction on a bank statement, and its balances.',
    'input_schema': {
        'type': 'object',
        'properties': {
            'period_end': {'type': 'string', 'description': 'The last day of the statement period, YYYY-MM-DD.'},
            'begin_balance': {'type': ['number', 'null'], 'description': 'The balance at the start of the period.'},
            'end_balance': {'type': 'number', 'description': 'The balance at the end of the period.'},
            'lines': {'type': 'array', 'items': {'type': 'object', 'properties': {
                'date': {'type': 'string', 'description': 'YYYY-MM-DD; use the statement period for the year.'},
                'description': {'type': 'string'},
                'amount': {'type': 'number', 'description': 'Positive for money in (deposits, credits, interest); negative for money out (checks, withdrawals, debits, fees).'},
                'check_number': {'type': 'string', 'description': 'For a check, its number; otherwise empty.'},
            }, 'required': ['date', 'description', 'amount']}},
        },
        'required': ['period_end', 'end_balance', 'lines'],
    },
}


def read_pdf_statement(data):
    if not settings.ANTHROPIC_API_KEY:
        raise StatementError('Reading a PDF statement needs the Claude connection, which is not set up. Upload the bank\'s CSV download instead.')
    import anthropic
    from core.app_settings import sanitized_setting
    client = anthropic.Anthropic(api_key=sanitized_setting('ANTHROPIC_API_KEY'))
    try:
        message = client.messages.create(
            model=STATEMENT_MODEL, max_tokens=8000, tools=[STATEMENT_TOOL], tool_choice={'type': 'tool', 'name': 'report_statement'},
            messages=[{'role': 'user', 'content': [
                {'type': 'document', 'source': {'type': 'base64', 'media_type': 'application/pdf', 'data': base64.b64encode(data).decode()}},
                {'type': 'text', 'text': 'This is a bank statement. List EVERY transaction (each check, each debit, each deposit and credit, interest, fees) exactly as printed, one line each, '
                                         'with the check number for checks. Do not combine lines. Money out is negative, money in is positive.'}]}])
    except Exception:
        logger.exception('Claude could not read the bank statement')
        raise StatementError('The statement could not be read right now. Upload the bank\'s CSV download, or try again.')
    block = next((b for b in message.content if b.type == 'tool_use'), None)
    if block is None:
        raise StatementError('Nothing could be read from that statement.')
    found = block.input
    lines = []
    for l in found.get('lines') or []:
        day, amount = _date(l.get('date')), _money(l.get('amount'))
        if day is None or amount is None:
            continue
        number = str(l.get('check_number') or '').strip()
        lines.append({'date': day, 'description': (l.get('description') or 'Bank line').strip(), 'amount': amount, 'kind': kind_for(l.get('description'), amount, number), 'check_number': number})
    if not lines:
        raise StatementError('No transactions were found on that statement.')
    end = _money(found.get('end_balance'))
    begin = _money(found.get('begin_balance')) if found.get('begin_balance') is not None else None
    return {'lines': lines, 'end_balance': end, 'begin_balance': begin, 'period_end': _date(found.get('period_end')) or max(l['date'] for l in lines)}


def read_statement(name, data):
    ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    if ext == 'csv':
        return read_csv_statement(data), 'csv'
    if ext == 'pdf':
        return read_pdf_statement(data), 'claude'
    raise StatementError('Upload the statement as a PDF or a CSV.')


@transaction.atomic
def import_statement(bank_account, parsed, file=None, read_by='csv', end_balance=None):
    """Save a read statement. A statement already saved for that period is replaced, unless some of its lines have been matched."""
    end = ledger.money(end_balance) if end_balance not in (None, '') else parsed.get('end_balance')
    if end is None:
        raise StatementError("The statement's ending balance could not be read: type it in.")
    period_end = parsed['period_end']
    existing = BankStatement.objects.filter(bank_account=bank_account, period_end=period_end).first()
    if existing:
        if BankLine.objects.filter(statement=existing, match__isnull=False).exists():
            raise StatementError('Lines of that statement are already matched. Undo the matches before replacing it.')
        existing.delete()
    st = BankStatement.objects.create(bank_account=bank_account, period_end=period_end, begin_balance=parsed.get('begin_balance'), end_balance=ledger.money(end), read_by=read_by)
    if file is not None:
        st.file.save(getattr(file, 'name', 'statement.pdf'), file, save=True)
    BankLine.objects.bulk_create([BankLine(statement=st, date=l['date'], description=l['description'][:300], amount=ledger.money(l['amount']), kind=l['kind'], check_number=l.get('check_number', '')[:20])
                                  for l in parsed['lines']])
    return st


def statement_for(bank_account, month_first):
    return BankStatement.objects.filter(bank_account=bank_account, period_end__gte=month_first, period_end__lte=ledger.month_end(month_first)).order_by('-period_end').first()


# ---------------------------------------------------------------- the ledger side

class Item:
    """One thing the books say happened on the bank account and the bank has not yet been matched to."""
    def __init__(self, kind, obj, day, amount, ref='', text='', channel=''):
        self.kind, self.obj, self.date, self.amount, self.ref, self.text, self.channel = kind, obj, day, amount, ref, text, channel
        self.key = ('L' if kind == 'line' else 'P') + str(obj.pk)

    @property
    def is_check(self):
        return bool(self.ref)


RECEIPT_CHANNEL = {'check': 'check', 'online': 'online', 'lockbox': 'online', 'ach': 'ach', 'card': 'card', 'cash': 'check', 'other': ''}


def ledger_items(bank_account, month_first):
    """The unmatched ledger items on a bank account, through the end of the month."""
    end = ledger.month_end(month_first)
    assoc = bank_account.association
    qs = (JournalLine.objects.filter(entry__association=assoc, account=bank_account.account, entry__date__lte=end, recon_item__isnull=True)
          .exclude(entry__basis=JournalEntry.Basis.CASH).exclude(entry__source=JournalEntry.Source.OPENING)
          .filter(entry__reversed_by__isnull=True, entry__reverses__isnull=True).select_related('entry', 'owner', 'vendor', 'unit').order_by('entry__date', 'id'))
    opening_day = ledger.profile_for(assoc).books_start
    if opening_day:
        # an entry dated on or before the opening day corrects the opening balance; the bank never saw it, so it is never carried as outstanding
        qs = qs.exclude(entry__date__lte=opening_day)
    entry_ids = [l.entry_id for l in qs]
    pays = {p.entry_id: p for p in Payment.objects.filter(entry_id__in=entry_ids)}
    receipts = {r.entry_id: r for r in Receipt.objects.filter(entry_id__in=entry_ids)}
    items = []
    for l in qs:
        amount = l.debit - l.credit
        pay, rec = pays.get(l.entry_id), receipts.get(l.entry_id)
        who = (l.vendor.name if l.vendor_id else '') or (l.owner.name if l.owner_id else '')
        text = f'{who} {l.memo or l.entry.memo}'
        channel = RECEIPT_CHANNEL.get(rec.method, '') if rec else ('check' if (pay and pay.method == 'check') else ('ach' if pay else ''))
        items.append(Item('line', l, l.entry.date, amount, ref=(pay.number if pay and pay.method == 'check' else ''), text=text, channel=channel))
    for p in Payment.objects.filter(bank_account=bank_account, opening=True, status=Payment.Status.OUTSTANDING, recon_item__isnull=True, date__lte=end):
        items.append(Item('payment', p, p.date, -p.amount, ref=p.number if p.method == 'check' else '', text=p.payee, channel='check' if p.method == 'check' else ''))
    return items


def _tokens(text):
    return {w for w in re.findall(r'[a-z]{3,}', (text or '').lower()) if w not in STOP}


def bank_channel(line):
    d = line.description.lower()
    if line.kind == BankLine.Kind.CHECK or line.check_number:
        return 'check'
    if re.search(r'card dep|yardi card|visa|mastercard|card settle', d):
        return 'card'
    if re.search(r'settlement|\bach\b|eft|web', d):
        return 'ach'
    if re.search(r'bill pay|coupon|consol|lockbox|online', d):
        return 'online'
    if line.amount > 0 and re.search(r'^deposit|branch deposit|\bdeposit\b', d):
        return 'check'
    return ''


def _key_of(line):
    toks = re.findall(r'[A-Za-z]{3,}', line.description.upper())
    return ' '.join(toks[:3])


def _same_sign(a, b):
    return (a > 0) == (b > 0)


def _score(line, item, days):
    ch_b, ch_i = bank_channel(line), item.channel
    channel = 3 if (ch_b and ch_i and ch_b == ch_i) else (-3 if (ch_b and ch_i and ch_b != ch_i) else 0)
    overlap = len(_tokens(line.description) & _tokens(item.text))
    return channel + 2 * min(overlap, 3) + (30 - min(days, 30)) / 10.0, overlap


# ---------------------------------------------------------------- matching

def _commit(bank_account, month_first, bank_lines, items, how, auto=True, note=''):
    m = ReconMatch.objects.create(bank_account=bank_account, month=month_first, auto=auto, how=how, note=note)
    BankLine.objects.filter(pk__in=[b.pk for b in bank_lines]).update(match=m)
    for b in bank_lines:
        b.match_id = m.pk
    first_day = min(b.date for b in bank_lines)
    for it in items:
        if it.kind == 'line':
            ReconLedgerItem.objects.create(match=m, line=it.obj)
            pay = Payment.objects.filter(entry_id=it.obj.entry_id, status=Payment.Status.OUTSTANDING).first()
        else:
            ReconLedgerItem.objects.create(match=m, payment=it.obj)
            pay = it.obj
        if pay is not None and pay.status == Payment.Status.OUTSTANDING:
            pay.status, pay.cleared_on = Payment.Status.CLEARED, first_day
            pay.save(update_fields=['status', 'cleared_on'])
    return m


@transaction.atomic
def auto_match(bank_account, month_first):
    """Line up everything that can be lined up. Returns how many matches were made."""
    stmt = statement_for(bank_account, month_first)
    if stmt is None:
        return 0
    bank = list(stmt.lines.filter(match__isnull=True))
    items = ledger_items(bank_account, month_first)
    made = 0
    take = lambda seq, objs: [x for x in seq if x not in objs]

    # 1. checks by number
    for b in list(bank):
        if not b.check_number:
            continue
        num = b.check_number.lstrip('0')
        for it in items:
            if it.ref and it.ref.lstrip('0') == num and it.amount == b.amount:
                _commit(bank_account, month_first, [b], [it], 'check number')
                bank, items, made = take(bank, [b]), take(items, [it]), made + 1
                break

    # 2. one to one, by amount, then payee, channel and date
    pairs = []
    for b in bank:
        same = [it for it in items if it.amount == b.amount and abs((it.date - b.date).days) <= 40]
        for it in same:
            score, overlap = _score(b, it, abs((it.date - b.date).days))
            pairs.append((score, overlap, len(same), b, it))
    pairs.sort(key=lambda p: -p[0])
    used_b, used_i = set(), set()
    for score, overlap, ambiguity, b, it in pairs:
        if b.pk in used_b or it.key in used_i:
            continue
        how = 'amount and payee' if overlap else ('amount and date' if ambiguity > 1 else 'amount')
        _commit(bank_account, month_first, [b], [it], how)
        used_b.add(b.pk)
        used_i.add(it.key)
        made += 1
    bank = [b for b in bank if b.pk not in used_b]
    items = [it for it in items if it.key not in used_i]

    # 3. several bank lines that add up to one ledger line (same payee on the statement)
    groups = {}
    for b in bank:
        groups.setdefault(_key_of(b), []).append(b)
    for key, lines in groups.items():
        if len(lines) < 2:
            continue
        total = sum((b.amount for b in lines), ZERO)
        for it in items:
            if it.amount == total:
                _commit(bank_account, month_first, lines, [it], 'several bank lines add up')
                bank, items, made = take(bank, lines), take(items, [it]), made + 1
                break

    # 4. several ledger items that add up to one bank line (a deposit of several checks)
    for b in sorted(bank, key=lambda x: -abs(x.amount)):
        cands = [it for it in items if _same_sign(it.amount, b.amount) and b.date - timedelta(days=45) <= it.date <= b.date + timedelta(days=5)]
        if len(cands) < 2 or len(cands) > 14:
            continue
        found = None
        for size in range(2, min(6, len(cands)) + 1):
            for combo in combinations(cands, size):
                if sum((c.amount for c in combo), ZERO) == b.amount:
                    found = combo
                    break
            if found:
                break
        if found:
            _commit(bank_account, month_first, [b], list(found), 'several payments in one deposit')
            bank, items, made = take(bank, [b]), take(items, found), made + 1

    # 5. any few bank lines that add up to one ledger item (one journal entry covering checks and interest)
    if 2 <= len(bank) <= 16:
        for it in sorted(items, key=lambda x: -abs(x.amount)):
            found = None
            for size in range(2, min(7, len(bank)) + 1):
                for combo in combinations(bank, size):
                    if sum((c.amount for c in combo), ZERO) == it.amount:
                        found = combo
                        break
                if found:
                    break
            if found:
                _commit(bank_account, month_first, list(found), [it], 'adds up to one ledger entry')
                bank, items, made = take(bank, found), take(items, [it]), made + 1
    return made


# ---------------------------------------------------------------- by hand

def _item_from_key(bank_account, month_first, key):
    for it in ledger_items(bank_account, month_first):
        if it.key == key:
            return it
    raise StatementError('One of those items is no longer unmatched. Refresh the page.')


@transaction.atomic
def match_manual(bank_account, month_first, bank_line_ids, item_keys, note=''):
    lines = list(BankLine.objects.filter(pk__in=bank_line_ids, statement__bank_account=bank_account, match__isnull=True))
    if len(lines) != len(set(bank_line_ids)) or not lines:
        raise StatementError('Choose at least one bank line that is not matched yet.')
    items = [_item_from_key(bank_account, month_first, k) for k in item_keys]
    if not items:
        raise StatementError('Choose at least one ledger item to match it with.')
    bank_total, item_total = sum((l.amount for l in lines), ZERO), sum((i.amount for i in items), ZERO)
    if bank_total != item_total:
        raise StatementError(f'They do not add up: the bank lines come to ${bank_total:,.2f} and the ledger items to ${item_total:,.2f}.')
    return _commit(bank_account, month_first, lines, items, 'matched by hand', auto=False, note=note)


@transaction.atomic
def unmatch(match):
    for ri in match.ledger_items.select_related('payment', 'line'):
        pay = ri.payment or Payment.objects.filter(entry_id=ri.line.entry_id, status=Payment.Status.CLEARED, cleared_on__isnull=False).first()
        if pay is not None and pay.status == Payment.Status.CLEARED:
            pay.status, pay.cleared_on = Payment.Status.OUTSTANDING, None
            pay.save(update_fields=['status', 'cleared_on'])
    match.bank_lines.update(match=None)
    match.delete()


def accept_timing(bank_account, month_first, item_key, user=None, note=''):
    it = _item_from_key(bank_account, month_first, item_key)
    kwargs = {'line': it.obj} if it.kind == 'line' else {'payment': it.obj}
    ReconAcceptance.objects.get_or_create(bank_account=bank_account, month=month_first, defaults={'note': note[:200], 'accepted_by': user}, **kwargs)


def unaccept(bank_account, month_first, item_key):
    kind, pk = item_key[0], int(item_key[1:])
    ReconAcceptance.objects.filter(bank_account=bank_account, month=month_first, **({'line_id': pk} if kind == 'L' else {'payment_id': pk})).delete()


@transaction.atomic
def add_to_books(bank_line, account, user=None, vendor=None, memo=''):
    """A bank line the books do not have yet (a fee, interest, an autopay): post it to the books, dated as the bank has it, and match it."""
    bank = bank_line.statement.bank_account
    assoc = bank.association
    desc = memo or bank_line.description
    amount = abs(bank_line.amount)
    cash = {'account': bank.account, 'memo': desc, 'vendor': vendor}
    other = {'account': account, 'memo': desc, 'vendor': vendor}
    if bank_line.amount < 0:
        cash['credit'], other['debit'] = amount, amount
    else:
        cash['debit'], other['credit'] = amount, amount
    entry = ledger.post_entry(assoc, bank_line.date, [cash, other], memo=desc, source=JournalEntry.Source.BANK, source_ref=f'bankline:{bank_line.pk}', user=user)
    line = entry.lines.get(account=bank.account)
    month_first = ledger.month_start(bank_line.statement.period_end)
    return _commit(bank, month_first, [bank_line], [Item('line', line, entry.date, line.debit - line.credit)], 'added to the books', auto=False)


@transaction.atomic
def owner_payment_from_line(bank_line, unit, owner, user=None, method=None):
    """A deposit on the statement that is an owner's payment the books do not have: record it, and match it."""
    bank = bank_line.statement.bank_account
    method = method or {'check': Receipt.Method.CHECK, 'online': Receipt.Method.ONLINE, 'ach': Receipt.Method.ACH, 'card': Receipt.Method.CARD}.get(bank_channel(bank_line), Receipt.Method.CHECK)
    receipt = charges.receive_payment(bank.association, unit, owner, bank_line.date, bank_line.amount, method, reference=bank_line.description[:80], bank_account=bank, user=user)
    line = receipt.entry.lines.get(account=bank.account)
    month_first = ledger.month_start(bank_line.statement.period_end)
    return _commit(bank, month_first, [bank_line], [Item('line', line, receipt.date, line.debit - line.credit)], 'recorded from the statement', auto=False)


# ---------------------------------------------------------------- the proof and closing

def summary(bank_account, month_first):
    stmt = statement_for(bank_account, month_first)
    end = ledger.month_end(month_first)
    items = ledger_items(bank_account, month_first)
    accepted = set()
    for a in ReconAcceptance.objects.filter(bank_account=bank_account, month=month_first):
        accepted.add(('L' + str(a.line_id)) if a.line_id else ('P' + str(a.payment_id)))
    rows = []
    for it in items:
        rows.append({'item': it, 'accepted': it.key in accepted, 'age': (end - it.date).days, 'stale': (end - it.date).days > STALE_DAYS and it.amount < 0})
    outstanding = [r for r in rows if r['item'].amount < 0]
    transit = [r for r in rows if r['item'].amount > 0]
    unmatched_bank = list(stmt.lines.filter(match__isnull=True)) if stmt else []
    matches = list(ReconMatch.objects.filter(bank_account=bank_account, bank_lines__statement=stmt).distinct().prefetch_related('bank_lines', 'ledger_items__line__entry', 'ledger_items__payment')) if stmt else []
    for m in matches:
        m.total = sum((b.amount for b in m.bank_lines.all()), ZERO)
    book = ledger.cash_balance(bank_account, end)
    os_total = -sum((r['item'].amount for r in outstanding), ZERO)
    dit_total = sum((r['item'].amount for r in transit), ZERO)
    bank_only = sum((b.amount for b in unmatched_bank), ZERO)
    adjusted_bank = (stmt.end_balance if stmt else ZERO) - os_total + dit_total
    adjusted_book = book + bank_only
    rec = Reconciliation.objects.filter(bank_account=bank_account, month=month_first).first()
    difference = adjusted_bank - adjusted_book
    unaccepted = [r for r in rows if not r['accepted']]
    if rec and rec.status == Reconciliation.Status.CLOSED:
        status = 'done'
    elif stmt is None:
        status = 'not_started'
    elif difference == 0 and not unmatched_bank and not unaccepted:
        status = 'ready'
    else:
        status = 'attention'
    return {'statement': stmt, 'month': month_first, 'month_end': end, 'rows': rows, 'outstanding': outstanding, 'transit': transit, 'unmatched_bank': unmatched_bank, 'matches': matches,
            'book': book, 'os_total': os_total, 'dit_total': dit_total, 'bank_only': bank_only, 'adjusted_bank': adjusted_bank, 'adjusted_book': adjusted_book,
            'difference': difference, 'status': status, 'reconciliation': rec, 'need_count': len(unaccepted) + len(unmatched_bank), 'matched_bank': (stmt.lines.filter(match__isnull=False).count() if stmt else 0),
            'bank_total': (stmt.lines.count() if stmt else 0)}


@transaction.atomic
def close_reconciliation(bank_account, month_first, user=None):
    s = summary(bank_account, month_first)
    if s['statement'] is None:
        raise StatementError('Upload the statement first.')
    if s['difference'] != 0:
        raise StatementError(f'The difference is ${s["difference"]:,.2f}, not zero.')
    if s['unmatched_bank']:
        raise StatementError('Some bank lines are not matched or added to the books yet.')
    if any(not r['accepted'] for r in s['rows']):
        raise StatementError('Say what to do with each check or deposit that has not cleared.')
    rec, _ = Reconciliation.objects.get_or_create(bank_account=bank_account, month=month_first)
    rec.status, rec.statement, rec.closed_by = Reconciliation.Status.CLOSED, s['statement'], user
    from django.utils import timezone
    rec.closed_at = timezone.now()
    rec.snapshot = {'statement_end': str(s['statement'].end_balance), 'book': str(s['book']), 'outstanding': [{'ref': r['item'].ref, 'date': str(r['item'].date), 'text': r['item'].text.strip(), 'amount': str(-r['item'].amount)} for r in s['outstanding']],
                    'in_transit': [{'date': str(r['item'].date), 'text': r['item'].text.strip(), 'amount': str(r['item'].amount)} for r in s['transit']],
                    'os_total': str(s['os_total']), 'dit_total': str(s['dit_total']), 'matched_lines': s['matched_bank'], 'statement_lines': s['bank_total']}
    rec.save()
    return rec


def reopen_reconciliation(bank_account, month_first):
    Reconciliation.objects.filter(bank_account=bank_account, month=month_first).update(status=Reconciliation.Status.OPEN, closed_at=None, closed_by=None)
