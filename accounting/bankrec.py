"""Bank statements and the reconciliation.

The statement's lines are lined up with the ledger's lines on the same bank account, surest matches first, and whatever is left is for a person:
  1. a check, by its number;
  2. one to one, by amount, then by who was paid and how and when;
  3. a withdrawal that is one of our recurring bills (the power company's debit, the insurance installment): the bill is made from the line if it is not there, paid, and matched;
  4. several bank lines that add up to one ledger line (six power-company debits for one bill);
  5. several ledger lines that add up to one bank line (two owners' checks in one deposit);
  6. any few bank lines that add up to one ledger line (a journal entry that lumped checks and interest together).
What cannot be lined up is either a check or deposit that simply has not reached the bank yet (a person marks it as timing, and it carries to next month),
or a bank line the books do not have yet (a person adds it to the books, one click).

The proof: statement balance, less what has not cleared, plus deposits on their way, must equal the books, plus anything the bank has that the books do not.
"""
import base64
import csv
import io
import logging
import re
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from itertools import combinations

from django.conf import settings
from django.db import transaction
from django.db.models import Q

from . import charges, ledger, payables
from .models import (ZERO, BankAccount, BankOpeningItem, BankLine, BankStatement, Bill, JournalEntry, JournalLine, Payment, ReconAcceptance, ReconLedgerItem, ReconMatch, Reconciliation, Receipt,
                     RecurringBill)

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


_LINE_TOOL = {'type': 'object', 'properties': {
    'date': {'type': 'string', 'description': 'YYYY-MM-DD; use the statement period for the year.'},
    'description': {'type': 'string'},
    'amount': {'type': 'number', 'description': 'Positive for money in (deposits, credits, interest); negative for money out (checks, withdrawals, debits, fees).'},
    'check_number': {'type': 'string', 'description': 'For a check, its number; otherwise empty.'},
}, 'required': ['date', 'description', 'amount']}

STATEMENT_TOOL = {
    'name': 'report_statement',
    'description': 'Report every account on a bank statement, each with its own balances and the transactions printed in its own section.',
    'input_schema': {
        'type': 'object',
        'properties': {
            'period_end': {'type': 'string', 'description': 'The last day of the statement period, YYYY-MM-DD.'},
            'accounts': {
                'type': 'array',
                'description': ('One entry for EACH account on the statement. An ordinary statement has one entry. A combined statement (for example a checking account and a money market '
                                'savings account in one PDF) has one entry per account: never merge two accounts into one. A summary page that only adds the accounts together is not an account.'),
                'items': {'type': 'object', 'properties': {
                    'account_name': {'type': 'string', 'description': 'The account as the statement names it, e.g. ASSOC SVCS INTEREST CHECKING.'},
                    'last4': {'type': 'string', 'description': "The last four digits of this account's number as printed, e.g. 9511. Empty only if no account number is printed."},
                    'begin_balance': {'type': ['number', 'null'], 'description': 'The balance of this account at the start of the period.'},
                    'end_balance': {'type': 'number', 'description': 'The balance of this account at the end of the period.'},
                    'lines': {'type': 'array', 'items': _LINE_TOOL},
                }, 'required': ['last4', 'end_balance', 'lines']},
            },
        },
        'required': ['period_end', 'accounts'],
    },
}


def _section(found, period):
    """One account of a statement, as the reader reported it."""
    lines = []
    for l in found.get('lines') or []:
        day, amount = _date(l.get('date')), _money(l.get('amount'))
        if day is None or amount is None:
            continue
        number = str(l.get('check_number') or '').strip()
        lines.append({'date': day, 'description': (l.get('description') or 'Bank line').strip(), 'amount': amount, 'kind': kind_for(l.get('description'), amount, number), 'check_number': number})
    end = _money(found.get('end_balance')) if found.get('end_balance') is not None else None
    begin = _money(found.get('begin_balance')) if found.get('begin_balance') is not None else None
    last4 = re.sub(r'\D', '', str(found.get('last4') or ''))[-4:]
    return {'name': (found.get('account_name') or '').strip(), 'last4': last4, 'lines': lines, 'end_balance': end, 'begin_balance': begin,
            'period_end': period or (max(l['date'] for l in lines) if lines else None)}


def parse_statement_found(found):
    """What the reader reported, as {'accounts': [one section per account], 'period_end'}; a statement with one account also carries that account's lines and balances at the top,
    which is what a CSV gives, so one-account statements go through `import_statement` as they always did."""
    accounts = found.get('accounts')
    if accounts is None and 'lines' in found:                      # an answer in the older one-account shape
        accounts = [found]
    period = _date(found.get('period_end'))
    sections = [_section(a, period) for a in (accounts or [])]
    sections = [s for s in sections if s['lines'] or s['end_balance'] is not None]
    if not sections:
        raise StatementError('Nothing could be read from that statement.')
    if not any(s['lines'] for s in sections):
        raise StatementError('No transactions were found on that statement.')
    period = period or max(l['date'] for s in sections for l in s['lines'])
    for s in sections:
        s['period_end'] = period
    parsed = {'accounts': sections, 'period_end': period}
    if len(sections) == 1:
        parsed.update({k: sections[0][k] for k in ('lines', 'end_balance', 'begin_balance')})
    return parsed


def _printed_account_tails(data, wanted):
    """Which of the wanted last 4s the file itself prints as the end of an account number ("Account Number: 000004406333635"). Read from the PDF's own text, not by the reader, so it is
    an independent check on what the reader reports. A scan with no text, or a bank that prints no such label, says nothing (an empty set)."""
    if not wanted:
        return set()
    try:
        from pypdf import PdfReader
        text = ' '.join((p.extract_text() or '') for p in PdfReader(io.BytesIO(data)).pages)
    except Exception:
        return set()
    tails = {m[-4:] for m in re.findall(r'(?:account|acct)[^\d\n]{0,40}(\d{6,})', text, flags=re.IGNORECASE)}
    return {w for w in wanted if w in tails}


def _ask_reader(data, hint=''):
    """One reading of a PDF statement by Claude, as the raw {'period_end', 'accounts'} answer."""
    if not settings.ANTHROPIC_API_KEY:
        raise StatementError('Reading a PDF statement needs the Claude connection, which is not set up. Upload the bank\'s CSV download instead.')
    import anthropic
    from core.app_settings import sanitized_setting
    client = anthropic.Anthropic(api_key=sanitized_setting('ANTHROPIC_API_KEY'))
    try:
        message = client.messages.create(
            model=STATEMENT_MODEL, max_tokens=16000, tools=[STATEMENT_TOOL], tool_choice={'type': 'tool', 'name': 'report_statement'},
            messages=[{'role': 'user', 'content': [
                {'type': 'document', 'source': {'type': 'base64', 'media_type': 'application/pdf', 'data': base64.b64encode(data).decode()}},
                {'type': 'text', 'text': 'This is a bank statement. It may hold more than one account (for example a checking account and a money market savings account in one combined PDF): '
                                         'report each account separately, with the last four digits of its account number, its own beginning and ending balance, and only the transactions printed in '
                                         "that account's own section. A page that only adds the accounts together (a consolidated or combined balance summary with a Total line) is not an account: "
                                         'never report it, and never put one account\'s transactions under another. For each account list EVERY transaction (each check, each debit, '
                                         'each deposit and credit, interest, fees) exactly as printed, one line each, with the check number for checks. Do not combine lines. '
                                         'Money out is negative, money in is positive.' + hint}]}])
    except Exception:
        logger.exception('Claude could not read the bank statement')
        raise StatementError('The statement could not be read right now. Upload the bank\'s CSV download, or try again.')
    block = next((b for b in message.content if b.type == 'tool_use'), None)
    if block is None:
        raise StatementError('Nothing could be read from that statement.')
    return block.input


def read_pdf_statement(data, known=None):
    """Read a PDF statement. `known` is the association's bank accounts as [(last4, name)]: they are told to the reader, and the file's own printed account numbers are used to check the
    answer, so a combined statement read as one account is read again, and refused if it still is, instead of being filed on the wrong account."""
    known = [(l4, n) for l4, n in (known or []) if l4]
    hint = (' The association has these bank accounts: ' + '; '.join(f'{n} (ending {l4})' for l4, n in known) + '. A statement may hold several of them.') if known else ''
    printed = _printed_account_tails(data, [l4 for l4, _ in known])
    parsed = parse_statement_found(_ask_reader(data, hint))
    missing = sorted(printed - {s['last4'] for s in parsed['accounts']})
    if missing:
        again = ' The file prints account numbers ending ' + ', '.join(sorted(printed)) + ': report a separate account for each, each with its own beginning and ending balance and only its own transactions.'
        parsed = parse_statement_found(_ask_reader(data, hint + again))
        missing = sorted(printed - {s['last4'] for s in parsed['accounts']})
        if missing:
            raise StatementError('That file prints the account ending ' + ' and '.join(missing) + ' but it could not be read as its own account, so nothing was filed (a combined statement must not be filed as one '
                                 'account). Try again, or upload the bank\'s CSV download.')
    return parsed


def read_statement(name, data, known=None):
    ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    if ext == 'csv':
        return read_csv_statement(data), 'csv'
    if ext == 'pdf':
        return read_pdf_statement(data, known), 'claude'
    raise StatementError('Upload the statement as a PDF or a CSV.')


@transaction.atomic
def import_statement(bank_account, parsed, file=None, read_by='csv', end_balance=None):
    """Save a read statement of ONE account. A statement already saved for that period is replaced, unless some of its lines have been matched."""
    if len(parsed.get('accounts') or []) > 1:
        raise StatementError('That statement holds several accounts. File it with file_statement, which puts each on its own bank account.')
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


def known_accounts(association):
    """[(last 4, name)] of the association's bank accounts that have a last 4: what the reader is told, and what it is checked against."""
    return [(b.last4, b.name) for b in BankAccount.objects.filter(association=association, is_active=True) if b.last4]


def _where(section):
    return (section.get('name') or 'an account') + (f' ending {section["last4"]}' if section.get('last4') else '')


@transaction.atomic
def file_statement(bank_account, parsed, file=None, read_by='csv', end_balance=None):
    """Put each account on a statement on the bank account it belongs to, matching by the last 4 digits of the account number, among the bank accounts of the same association.
    One account that is not the one the user uploaded to but whose last 4 belong to another bank account of the association goes to that one. Several accounts in one file (a checking
    and a savings account on one PDF) each get their own statement, their own lines and balances, all pointing at the one file; an account that no bank account claims is left out and
    reported, never guessed. Returns {'filed': [(bank account, statement, section)], 'skipped': [section], 'moved': bool, 'multi': bool}."""
    sections = parsed.get('accounts') or [parsed]
    banks = list(BankAccount.objects.filter(association_id=bank_account.association_id, is_active=True))
    if bank_account.pk not in {b.pk for b in banks}:
        banks.append(bank_account)
    plan, skipped, moved, claimed = [], [], False, set()
    if len(sections) == 1:
        sec = sections[0]
        target = bank_account
        last4 = sec.get('last4') or ''
        if last4 and bank_account.last4 and bank_account.last4 != last4:
            others = [b for b in banks if b.pk != bank_account.pk and b.last4 == last4]
            if len(others) != 1:
                raise StatementError(f'That statement is for the account ending {last4}, not {bank_account.label} (ending {bank_account.last4}), and no other bank account of this association ends in '
                                     f'{last4}. Nothing was filed. Upload it from the right bank account, or put the right last 4 on the bank account in the settings.')
            target, moved = others[0], True
        plan.append((target, sec))
    else:
        if end_balance not in (None, ''):
            raise StatementError('Type the ending balance only for a statement with one account. This one holds several.')
        for sec in sections:
            last4 = sec.get('last4') or ''
            mine = [b for b in banks if last4 and b.last4 == last4]
            if len(mine) == 1 and mine[0].pk not in claimed:
                claimed.add(mine[0].pk)
                plan.append((mine[0], sec))
            else:
                skipped.append(sec)
        if not plan:
            raise StatementError(f'That statement holds {len(sections)} accounts ({"; ".join(_where(s) for s in sections)}) and none matches a bank account of this association by its last 4 digits. '
                                 'Put each bank account\'s last 4 in the settings, then upload the statement again.')
    warnings = []
    replaced = {t.pk for t, _ in plan}
    for target, sec in plan:
        twin = _filed_on_another_account(target, sec, replaced)
        if twin is not None:
            other, common = twin
            raise StatementError(f'This looks like the statement already filed on {other.bank_account.label} for {other.period_end:%B %Y}: the same ' + (f'{common} lines' if common else 'balances') +
                                 f' came up for {target.label}. Nothing was filed. A statement that holds several accounts has to be read one account at a time; try the upload again, or upload the bank\'s CSV.')
        warnings += _balance_notes(target, sec)
    filed, stored = [], None
    for target, sec in plan:
        if file is not None and hasattr(file, 'seek'):
            file.seek(0)
        try:
            st = import_statement(target, sec, file=file if stored is None else None, read_by=read_by, end_balance=end_balance if len(sections) == 1 else None)
        except StatementError as e:
            raise StatementError(f'{target.label}: {e}' if len(sections) > 1 else str(e))
        if stored is None and st.file:
            stored = st.file.name
        elif stored is not None:
            st.file.name = stored                                   # the one stored file is behind every statement filed from it
            st.save(update_fields=['file'])
        filed.append((target, st, sec))
    return {'filed': filed, 'skipped': skipped, 'moved': moved, 'multi': len(sections) > 1, 'warnings': warnings}


def _filed_on_another_account(target, sec, replaced):
    """(statement, number of lines in common) if what was read for `target` is the same statement as one already filed on a DIFFERENT bank account of the association for about the same
    period: mostly the same dated lines, or the same beginning and ending balance. Accounts about to be replaced by this same upload are left out. A real account never has another account's
    lines; this is what a combined statement read as one account looks like."""
    day = sec['period_end']
    lines = sec.get('lines') or []
    mine = Counter((l['date'], ledger.money(l['amount'])) for l in lines)
    begin, end = sec.get('begin_balance'), sec.get('end_balance')
    others = (BankStatement.objects.filter(bank_account__association_id=target.association_id, period_end__gte=day - timedelta(days=5), period_end__lte=day + timedelta(days=5))
              .exclude(bank_account_id__in=replaced).select_related('bank_account').prefetch_related('lines'))
    for st in others:
        common = sum((mine & Counter((l.date, l.amount) for l in st.lines.all())).values())
        same_lines = len(lines) >= 3 and common >= 0.8 * len(lines)
        same_balances = begin is not None and end and st.begin_balance is not None and begin == st.begin_balance and end == st.end_balance
        if same_lines or same_balances:
            return st, common
    return None


def _balance_notes(target, sec):
    """Things worth a person's look, never a refusal: the statement does not begin where the last one on file ended, or its lines do not add up to its own balances."""
    notes = []
    begin, end, day = sec.get('begin_balance'), sec.get('end_balance'), sec['period_end']
    if begin is not None:
        prev = (BankStatement.objects.filter(bank_account=target, period_end__lt=day - timedelta(days=15), period_end__gte=day - timedelta(days=50)).order_by('-period_end').first())
        if prev is not None and ledger.money(prev.end_balance) != ledger.money(begin):
            notes.append(f'{target.label}: this statement begins at ${ledger.money(begin):,.2f} but the last one on file ended at ${prev.end_balance:,.2f}. Make sure it is the right account.')
    lines = sec.get('lines') or []
    if begin is not None and end is not None and lines:
        total = sum((ledger.money(l['amount']) for l in lines), ZERO)
        if ledger.money(begin) + total != ledger.money(end):
            notes.append(f'{target.label}: the {len(lines)} lines add up to ${total:,.2f}, but the statement\'s balances move by ${ledger.money(end) - ledger.money(begin):,.2f}. A line may have been missed or misread.')
    return notes


def split_combined_statement(statement):
    """Read again the file stored on a statement that was read as ONE account though it holds several, and file each account on its own bank account (each replaces the wrong
    statement for the period, unless some of its lines are already matched). The bank accounts need their last 4 set first. Meant to be run by hand, once, for La Pensee's two
    October Truist statements; nothing calls it."""
    from django.core.files.base import ContentFile
    if not statement.file:
        raise StatementError('That statement has no stored file to read again.')
    statement.file.open('rb')
    try:
        data = statement.file.read()
    finally:
        statement.file.close()
    parsed = read_pdf_statement(data, known_accounts(statement.bank_account.association))
    return file_statement(statement.bank_account, parsed, file=ContentFile(data, name=statement.file.name.rsplit('/', 1)[-1] or 'statement.pdf'), read_by='claude')


def statement_for(bank_account, month_first):
    return BankStatement.objects.filter(bank_account=bank_account, period_end__gte=month_first, period_end__lte=ledger.month_end(month_first)).order_by('-period_end').first()


# ---------------------------------------------------------------- the ledger side

class Item:
    """One thing the books say happened on the bank account and the bank has not yet been matched to."""
    def __init__(self, kind, obj, day, amount, ref='', text='', channel=''):
        self.kind, self.obj, self.date, self.amount, self.ref, self.text, self.channel = kind, obj, day, amount, ref, text, channel
        self.key = {'line': 'L', 'payment': 'P', 'opening': 'O'}[kind] + str(obj.pk)

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
    for o in BankOpeningItem.objects.filter(bank_account=bank_account, recon_item__isnull=True, date__lte=end):
        items.append(Item('opening', o, o.date, o.amount, text=o.description))
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
    first_day = min((b.date for b in bank_lines), default=month_first)
    for it in items:
        if it.kind == 'line':
            ReconLedgerItem.objects.create(match=m, line=it.obj)
            pay = Payment.objects.filter(entry_id=it.obj.entry_id, status=Payment.Status.OUTSTANDING).first()
        elif it.kind == 'opening':
            ReconLedgerItem.objects.create(match=m, opening_item=it.obj)
            pay = None
        else:
            ReconLedgerItem.objects.create(match=m, payment=it.obj)
            pay = it.obj
        if pay is not None and pay.status == Payment.Status.OUTSTANDING:
            pay.status, pay.cleared_on = Payment.Status.CLEARED, first_day
            pay.save(update_fields=['status', 'cleared_on'])
    return m


def _squash(text):
    return re.sub(r'[^A-Z0-9]', '', (text or '').upper())


def _recurring_for(line, recs):
    """The one recurring bill a withdrawal on the statement is, or None when it is not clear (the line then stays for a person).
    A bill of a fixed amount is the line that has its text and its exact amount; a bill whose amount changes is the line that has its text. A check has no payee on the statement,
    so it is the one fixed check bill of that exact amount."""
    amount = -line.amount
    if line.kind == BankLine.Kind.CHECK or line.check_number:
        same = [r for r in recs if r.is_fixed and r.pay_method == 'check' and r.amount == amount]
        return same[0] if len(same) == 1 else None
    text = _squash(line.description)
    named = [r for r in recs if _squash(r.statement_match) and _squash(r.statement_match) in text]
    by_vendor = [r for r in recs if not _squash(r.statement_match) and r.is_fixed and r.pay_method == 'ach' and _tokens(r.vendor.name) & _tokens(line.description)]
    fixed = [r for r in named + by_vendor if r.is_fixed and r.amount == amount]
    if fixed:
        return fixed[0] if len(fixed) == 1 else None
    varies = [r for r in named if not r.is_fixed]
    return varies[0] if len(varies) == 1 else None


def _bill_paid_by_line(bank_account, rec, line):
    """Make (or find) the bill the statement line pays, pay it as the bank shows, and return the ledger item of that payment. None when this line is not that bill's payment."""
    assoc, amount = bank_account.association, -line.amount
    check = line.kind == BankLine.Kind.CHECK or bool(line.check_number)
    bill = None
    if rec.is_fixed:
        # an open bill of that amount from this month or before: the bank may take the money a few days before the day the bill is set for
        bill = next((b for b in rec.bills.filter(status=Bill.Status.APPROVED, date__lte=ledger.month_end(line.date)).prefetch_related('payments') if b.balance == amount), None)
        if bill is None and payables._has_bill_in_month(rec, line.date):
            return None       # that month's bill is already paid: this line is something else
    if bill is None:
        bill = payables.create_bill(assoc, line.date, [(rec.account, amount, rec.description)], vendor=rec.vendor, number=f'{line.date:%Y-%m}', due=line.date, kind=Bill.Kind.ROUTINE,
                                    source=Bill.Source.STATEMENT, recurring=rec, memo=rec.description)
        payables.skip_past(rec, line.date)
    pay = payables.pay_bills(assoc, bank_account, [(bill, amount)], line.date, method=Payment.Method.CHECK if check else Payment.Method.ACH, number=line.check_number if check else '',
                             memo='From the bank statement')
    ledger_line = pay.entry.lines.get(account=bank_account.account)
    return Item('line', ledger_line, pay.entry.date, ledger_line.debit - ledger_line.credit)


def _read_recurring(bank_account, month_first, bank):
    """Every withdrawal on the statement that is one of the association's recurring bills becomes that bill, paid, and matched. Returns the bank lines it took."""
    recs = list(RecurringBill.objects.filter(association=bank_account.association, is_active=True).select_related('vendor', 'account'))
    took = []
    if not recs:
        return took
    for line in bank:
        if line.amount >= 0:
            continue
        rec = _recurring_for(line, recs)
        if rec is None:
            continue
        try:
            with transaction.atomic():
                item = _bill_paid_by_line(bank_account, rec, line)
                if item is not None:
                    _commit(bank_account, month_first, [line], [item], f'recurring bill: {rec.description}'[:40])
        except (ledger.LedgerError, payables.PayableError):
            continue      # a month that is closed, or an entry the books refuse: leave the line for a person
        if item is not None:
            took.append(line)
    return took


@transaction.atomic
def auto_match(bank_account, month_first, report=None):
    """Line up everything that can be lined up. Returns how many matches were made; `report`, if given, is filled with how many of them were recurring bills read off the statement."""
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

    # 3. a withdrawal that is one of our recurring bills: the bill is read off the line
    read = _read_recurring(bank_account, month_first, bank)
    bank, made = take(bank, read), made + len(read)
    if report is not None:
        report['recurring'] = len(read)

    # 4. several bank lines that add up to one ledger line (same payee on the statement)
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

    # 5. several ledger items that add up to one bank line (a deposit of several checks)
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

    # 6. any few bank lines that add up to one ledger item (one journal entry covering checks and interest)
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
    lines = list(BankLine.objects.select_for_update(of=('self',)).filter(pk__in=bank_line_ids, statement__bank_account=bank_account, match__isnull=True))
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
def clear_selected(bank_account, month_first, bank_line_ids, item_keys, note='', user=None, account=None, day=None, memo=''):
    """Clear what a person ticked: bank lines and ledger items that are the same money, or that cancel each other out (a deposit and its returned item). Nothing is cleared on its own.
    When the ticked things are not equal, the difference is posted as one journal entry to `account`, dated `day` (the end of the month by default), and cleared with them."""
    lines = list(BankLine.objects.select_for_update(of=('self',)).filter(pk__in=bank_line_ids, statement__bank_account=bank_account, match__isnull=True))
    if len(lines) != len(set(bank_line_ids)):
        raise StatementError('One of those bank lines is already matched. Refresh the page.')
    items = [_item_from_key(bank_account, month_first, k) for k in item_keys]
    if not lines and not items:
        raise StatementError('Tick at least one thing to clear.')
    diff = sum((l.amount for l in lines), ZERO) - sum((i.amount for i in items), ZERO)
    how = 'cleared by hand'
    if diff != 0:
        if account is None:
            raise StatementError(f'What you ticked is ${abs(diff):,.2f} apart. Choose the account to post that difference to, or tick the rest of what belongs with it.')
        end = ledger.month_end(month_first)
        day = day or end
        if day > end:
            raise StatementError(f'Date the entry on or before {end:%B} {end.day}, {end.year}, so it belongs to the month you are reconciling.')
        amount = abs(diff)
        text = memo or 'Reconciling difference'
        cash = {'account': bank_account.account, 'memo': text}
        other = {'account': account, 'memo': text}
        if diff > 0:
            cash['debit'], other['credit'] = amount, amount
        else:
            cash['credit'], other['debit'] = amount, amount
        entry = ledger.post_entry(bank_account.association, day, [cash, other], memo=text, source=JournalEntry.Source.BANK, source_ref=f'reconciling:{month_first:%Y-%m}', user=user)
        line = entry.lines.get(account=bank_account.account)
        items.append(Item('line', line, entry.date, line.debit - line.credit, text=text))
        how = 'difference posted'
    return _commit(bank_account, month_first, lines, items, how, auto=False, note=note)


@transaction.atomic
def unmatch(match):
    for ri in match.ledger_items.select_related('payment', 'line'):
        if ri.opening_item_id:
            continue
        pay = ri.payment or Payment.objects.filter(entry_id=ri.line.entry_id, status=Payment.Status.CLEARED, cleared_on__isnull=False).first()
        if pay is not None and pay.status == Payment.Status.CLEARED:
            pay.status, pay.cleared_on = Payment.Status.OUTSTANDING, None
            pay.save(update_fields=['status', 'cleared_on'])
    match.bank_lines.update(match=None)
    match.delete()


def accept_timing(bank_account, month_first, item_key, user=None, note=''):
    it = _item_from_key(bank_account, month_first, item_key)
    kwargs = {'line': it.obj} if it.kind == 'line' else ({'opening_item': it.obj} if it.kind == 'opening' else {'payment': it.obj})
    ReconAcceptance.objects.get_or_create(bank_account=bank_account, month=month_first, defaults={'note': note[:200], 'accepted_by': user}, **kwargs)


def unaccept(bank_account, month_first, item_key):
    kind, pk = item_key[0], int(item_key[1:])
    field = {'L': 'line_id', 'P': 'payment_id', 'O': 'opening_item_id'}[kind]
    ReconAcceptance.objects.filter(bank_account=bank_account, month=month_first, **{field: pk}).delete()


def _words(text):
    """What a statement description says, without its numbers and punctuation, so 'ACH 1234 Interest Paid' and 'ACH 5678 Interest Paid' read alike."""
    return ' '.join(re.sub(r'[^a-z]+', ' ', (text or '').lower()).split())


def suggested_accounts(bank_lines, accounts):
    """{bank line id: the account it most likely goes to}, for the lines that have a good guess; the others are left out. Only ever a suggestion: a person still presses the button.
    The guess is what the same words on an earlier line were put to (the most recent time, in this association), else Bank Fees for a bank fee."""
    bank_lines = list(bank_lines)
    if not bank_lines:
        return {}
    bank = bank_lines[0].statement.bank_account
    pool = {a.pk: a for a in accounts if a.pk != bank.account_id and a.kind in ('expense', 'income', 'equity')}
    past = {}
    for entry in JournalEntry.objects.filter(association=bank.association, source=JournalEntry.Source.BANK, reverses__isnull=True).order_by('-date', '-id')[:500].prefetch_related('lines', 'reversed_by'):
        if entry.reversed_by.all():
            continue
        words = _words(entry.memo)
        for line in entry.lines.all():
            if words and line.account_id in pool:
                past.setdefault((words, line.credit > 0), pool[line.account_id])    # credit on the other side: money came in
    fees = [a for a in pool.values() if a.kind == 'expense' and a.name.lower() in ('bank fees', 'bank charges', 'bank service charges')]
    out = {}
    for b in bank_lines:
        guess = past.get((_words(b.description), b.amount > 0))
        if guess is None and b.kind == BankLine.Kind.FEE and b.amount < 0 and len(fees) == 1:
            guess = fees[0]
        if guess is not None:
            out[b.pk] = guess
    return out


def claim_line(bank_line):
    """Lock a bank line for the one action that will deal with it, and refuse if something already has: a click that arrives twice (a double click, a slow page pressed again) must not put the same
    money in the books twice. Call it inside the transaction, before anything is posted; it returns the fresh, locked line."""
    line = BankLine.objects.select_for_update(of=('self',)).select_related('statement__bank_account__association').get(pk=bank_line.pk)
    if line.match_id:
        raise StatementError('That bank line is already matched. Refresh the page.')
    if JournalEntry.objects.filter(association=line.statement.bank_account.association, source_ref=f'bankline:{line.pk}', reverses__isnull=True).exclude(reversed_by__isnull=False).exists():
        raise StatementError('That bank line was already added to the books. Refresh the page.')
    return line


@transaction.atomic
def add_to_books(bank_line, account, user=None, vendor=None, memo=''):
    """A bank line the books do not have yet (a fee, interest, an autopay): post it to the books, dated as the bank has it, and match it."""
    bank_line = claim_line(bank_line)
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


DUPLICATE_DAYS = 7


def receipt_candidates(bank_line, items=None):
    """The unmatched ledger items ('checks and deposits not on the statement') that could already BE this deposit: the same amount, dated within a week of it, owner receipts first, then the
    nearest in date. `items` may be the ledger items already read for the screen (so a page of deposits reads them once)."""
    bank = bank_line.statement.bank_account
    if items is None:
        items = ledger_items(bank, ledger.month_start(bank_line.statement.period_end))
    found = [it for it in items if it.amount == bank_line.amount and abs((it.date - bank_line.date).days) <= DUPLICATE_DAYS]
    found.sort(key=lambda it: (0 if getattr(it.obj, 'owner_id', None) else 1, abs((it.date - bank_line.date).days), it.date))
    return found


@transaction.atomic
def owner_payment_from_line(bank_line, unit, owner, user=None, method=None, force=False):
    """A deposit on the statement that is an owner's payment the books do not have: record it, and match it.
    Refused when the books already hold a payment of the same amount from the same unit, within a week, that is not on the statement yet (a Yardi receipt, say): that is this deposit, and
    recording it again would count it twice. `force=True` records it anyway."""
    bank_line = claim_line(bank_line)
    bank = bank_line.statement.bank_account
    if not force:
        same = [it for it in receipt_candidates(bank_line) if getattr(it.obj, 'unit_id', None) == unit.pk]
        if same:
            it = same[0]
            raise StatementError(f'The books already have a ${it.amount:,.2f} payment for {unit.label} dated {it.date:%b} {it.date.day} that is not on the statement yet. Match this deposit to it instead of recording a new one.')
    method = method or {'check': Receipt.Method.CHECK, 'online': Receipt.Method.ONLINE, 'ach': Receipt.Method.ACH, 'card': Receipt.Method.CARD}.get(bank_channel(bank_line), Receipt.Method.CHECK)
    receipt = charges.receive_payment(bank.association, unit, owner, bank_line.date, bank_line.amount, method, reference=bank_line.description[:80], bank_account=bank, user=user)
    line = receipt.entry.lines.get(account=bank.account)
    month_first = ledger.month_start(bank_line.statement.period_end)
    return _commit(bank, month_first, [bank_line], [Item('line', line, receipt.date, line.debit - line.credit)], 'recorded from the statement', auto=False)


# ---------------------------------------------------------------- sorting a deposit

def _receipt_method(bank_line):
    return {'check': Receipt.Method.CHECK, 'online': Receipt.Method.ONLINE, 'ach': Receipt.Method.ACH, 'card': Receipt.Method.CARD}.get(bank_channel(bank_line), Receipt.Method.CHECK)


def deposit_candidates(bank_account, month_first):
    """Money the books already show coming in on this account that no bank deposit is tied to yet: owner payments recorded ahead of the deposit."""
    return [it for it in ledger_items(bank_account, month_first) if it.amount > 0]


def _positive(amount, what):
    if amount is None or amount <= 0:
        raise StatementError(f'{what} needs an amount above zero.')
    return ledger.money(amount)


@transaction.atomic
def sort_deposit(bank_line, owner_rows, other_rows, item_keys=(), user=None):
    """One bank deposit, broken out. It is made of any of: owner payments already recorded (`item_keys`), new owner payments (`owner_rows`: unit, owner, amount, ref), and money that is
    not an owner's (`other_rows`: account, amount, memo), which is posted to the account chosen. The pieces must add up to the deposit exactly. Everything is posted and the deposit
    matched in one step, or nothing happens."""
    bank_line = BankLine.objects.select_for_update(of=('self',)).select_related('statement__bank_account__association').get(pk=bank_line.pk)
    bank = bank_line.statement.bank_account
    assoc = bank.association
    if bank_line.match_id:
        raise StatementError('That deposit is already matched.')
    claim_line(bank_line)
    if bank_line.amount <= 0:
        raise StatementError('Only a deposit can be sorted here.')
    month_first = ledger.month_start(bank_line.statement.period_end)
    items = [_item_from_key(bank, month_first, k) for k in dict.fromkeys(item_keys)]
    if any(it.amount <= 0 for it in items):
        raise StatementError('Only money coming in can be part of a deposit.')
    if not (items or owner_rows or other_rows):
        raise StatementError('Say where the deposit came from.')
    owner_rows = [dict(r, amount=_positive(r.get('amount'), 'Each owner payment')) for r in owner_rows]
    other_rows = [dict(r, amount=_positive(r.get('amount'), 'Each other item')) for r in other_rows]
    total = sum((it.amount for it in items), ZERO) + sum((r['amount'] for r in owner_rows), ZERO) + sum((r['amount'] for r in other_rows), ZERO)
    if total != bank_line.amount:
        raise StatementError(f'The pieces come to ${total:,.2f} but the deposit is ${bank_line.amount:,.2f}.')
    for r in other_rows:
        acct = r['account']
        if acct.association_id != assoc.pk or not acct.is_active:
            raise StatementError('Choose an account from this association\'s chart.')
        if acct.pk == bank.account_id:
            raise StatementError('Choose an account other than the bank account itself.')
    method = _receipt_method(bank_line)
    tied = list(items)
    for r in owner_rows:
        if r.get('owner') is None:
            raise StatementError(f'Unit {r["unit"].label} has no owner on record.')
        receipt = charges.receive_payment(assoc, r['unit'], r['owner'], bank_line.date, r['amount'], method, reference=((r.get('ref') or '').strip() or bank_line.description)[:80], bank_account=bank, user=user)
        line = receipt.entry.lines.get(account=bank.account)
        tied.append(Item('line', line, receipt.date, line.debit - line.credit))
    for r in other_rows:
        desc = (r.get('memo') or '').strip() or bank_line.description
        entry = ledger.post_entry(assoc, bank_line.date, [{'account': bank.account, 'debit': r['amount'], 'memo': desc}, {'account': r['account'], 'credit': r['amount'], 'memo': desc}], memo=desc,
                                  source=JournalEntry.Source.BANK, source_ref=f'bankline:{bank_line.pk}', user=user)
        line = entry.lines.get(account=bank.account)
        tied.append(Item('line', line, entry.date, line.debit - line.credit))
    return _commit(bank, month_first, [bank_line], tied, 'sorted deposit', auto=False)


@transaction.atomic
def record_owner_payments(bank_account, day, rows, method=Receipt.Method.CHECK, user=None):
    """Owner payments in hand that have not reached the bank: each is recorded now (`rows`: unit, owner, amount, ref) and waits as a deposit on its way until a bank deposit is tied to it."""
    if not rows:
        raise StatementError('Add at least one owner.')
    made = []
    for r in rows:
        if r.get('owner') is None:
            raise StatementError(f'Unit {r["unit"].label} has no owner on record.')
        made.append(charges.receive_payment(bank_account.association, r['unit'], r['owner'], day, _positive(r.get('amount'), 'Each owner payment'), method, reference=(r.get('ref') or '').strip()[:80],
                                            bank_account=bank_account, user=user))
    return made


# ---------------------------------------------------------------- the proof and closing

def summary(bank_account, month_first):
    stmt = statement_for(bank_account, month_first)
    end = ledger.month_end(month_first)
    items = ledger_items(bank_account, month_first)
    accepted = set()
    for a in ReconAcceptance.objects.filter(bank_account=bank_account, month=month_first):
        accepted.add(('L' + str(a.line_id)) if a.line_id else (('O' + str(a.opening_item_id)) if a.opening_item_id else ('P' + str(a.payment_id))))
    rows = []
    for it in items:
        rows.append({'item': it, 'accepted': it.key in accepted, 'age': (end - it.date).days, 'stale': (end - it.date).days > STALE_DAYS and it.amount < 0})
    outstanding = [r for r in rows if r['item'].amount < 0]
    transit = [r for r in rows if r['item'].amount > 0]
    unmatched_bank = list(stmt.lines.filter(match__isnull=True)) if stmt else []
    matches = list(ReconMatch.objects.filter(Q(bank_lines__statement=stmt) | Q(month=month_first, bank_lines__isnull=True), bank_account=bank_account).distinct()
                   .prefetch_related('bank_lines', 'ledger_items__line__entry', 'ledger_items__payment', 'ledger_items__opening_item')) if stmt else []
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
