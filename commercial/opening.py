"""Opening balances for a commercial property, from the Yardi trial balance.

A trial balance file (xlsx or csv) lists each account with its balance on the day the books begin. Loading it posts one opening entry (per book, when the accrual and the cash
trial balances are both given) that makes every account equal the file, dated the day the balances are as of, and sets that day as the day the books begin: nothing is posted into
that day by hand, the property's own months start the day after. Nothing is loaded unless the file adds up (debits equal credits).

What the trial balance cannot say is who owes what: the receivable total comes over as one line. Each tenant's balance forward is entered by hand afterwards (a charge on the tenant's page).
"""
import csv
import io
import re
from decimal import Decimal, InvalidOperation

from django.db import transaction

from accounting import ledger
from accounting.models import ZERO, Account, JournalEntry

CODE = re.compile(r'^\s*(\d{3,6})\b')


class OpeningError(Exception):
    pass


def kind_for_code(code):
    n = int(code)
    return 'asset' if n < 2000 else 'liability' if n < 3000 else 'equity' if n < 4000 else 'income' if n < 5000 else 'expense'


def _amount(raw):
    """A Yardi amount as a signed Decimal (debits positive): 1,234.56  (1,234.56)  -1234.56  1,234.56CR  1234.56DR. Blank is zero."""
    if raw is None:
        return ZERO
    if isinstance(raw, (int, float, Decimal)):
        return ledger.money(raw)
    text = str(raw).strip().replace('$', '').replace(',', '')
    if not text:
        return ZERO
    sign = 1
    upper = text.upper()
    if upper.endswith('CR'):
        sign, text = -1, text[:-2]
    elif upper.endswith('DR'):
        text = text[:-2]
    text = text.strip()
    if text.startswith('(') and text.endswith(')'):
        sign, text = -sign, text[1:-1]
    try:
        return ledger.money(Decimal(text) * sign)
    except InvalidOperation:
        raise OpeningError(f'"{raw}" is not an amount.')


def _rows_of(data, filename):
    name = (filename or '').lower()
    if name.endswith('.csv') or name.endswith('.txt'):
        return [list(r) for r in csv.reader(io.StringIO(data.decode('utf-8-sig', errors='replace') if isinstance(data, bytes) else data))]
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data) if isinstance(data, bytes) else data, read_only=True, data_only=True)
    return [list(r) for r in wb.active.iter_rows(values_only=True)]


def parse_trial_balance(data, filename='trial_balance.xlsx'):
    """[(code, name, signed amount)] with a nonzero balance, from a Yardi trial balance file. Raises OpeningError when the layout is not recognised or the file does not balance."""
    rows = _rows_of(data, filename)
    head = None
    for i, r in enumerate(rows[:25]):
        cells = [str(c or '').strip().lower() for c in r]
        if any('account' in c for c in cells) and any(k in c for c in cells for k in ('debit', 'credit', 'balance', 'ending')):
            head = (i, cells)
            break
    if head is None:
        raise OpeningError('That does not look like a trial balance: no row with an account column and debit, credit or balance columns was found.')
    at, cells = head

    def col(*keys, exclude=()):
        for j, c in enumerate(cells):
            if any(k in c for k in keys) and not any(x in c for x in exclude):
                return j
        return None
    c_acct = col('account')
    c_name = col('name', 'description', exclude=('property',))
    c_end = col('ending', 'end balance', 'balance', exclude=('forward', 'begin'))
    c_deb, c_cred = col('debit'), col('credit')
    if c_acct is None or (c_end is None and (c_deb is None or c_cred is None)):
        raise OpeningError('The trial balance needs an account column and either an ending balance column or debit and credit columns.')
    out, total = [], ZERO
    for r in rows[at + 1:]:
        if c_acct >= len(r):
            continue
        m = CODE.match(str(r[c_acct] or ''))
        if not m:
            continue
        code = m.group(1)
        label = (str(r[c_name]).strip() if c_name is not None and c_name < len(r) and r[c_name] else str(r[c_acct]).strip()[len(code):].lstrip(' -:\t')) or code
        if c_end is not None and c_end < len(r) and r[c_end] not in (None, ''):
            amount = _amount(r[c_end])
        else:
            amount = _amount(r[c_deb] if c_deb < len(r) else None) - _amount(r[c_cred] if c_cred < len(r) else None)
        if amount == 0:
            continue
        out.append((code, label[:120], amount))
        total += amount
    if not out:
        raise OpeningError('No account with a balance was found in the file.')
    if total != 0:
        raise OpeningError(f'The trial balance does not add up: debits and credits differ by ${abs(total):,.2f}. Nothing was loaded.')
    return out


@transaction.atomic
def load_opening(prop, accrual_rows, cash_rows=None, as_of=None, user=None):
    """Post the opening balances. `*_rows` are parse_trial_balance results. Returns {'entries': n, 'accounts_made': n, 'accounts': n}. Refuses a property that already has entries."""
    if as_of is None:
        raise OpeningError('Say the day the balances are as of.')
    if JournalEntry.objects.filter(association=prop).exists():
        raise OpeningError(f'{prop.name} already has entries in its books: opening balances are only loaded into empty books.')
    ledger.ensure_chart(prop)
    made = 0
    have = {a.code: a for a in Account.objects.filter(association=prop)}

    def account(code, name):
        nonlocal made
        a = have.get(code)
        if a is None:
            a = Account.objects.create(association=prop, code=code, name=name, kind=kind_for_code(code))
            have[code] = a
            made += 1
        return a

    entries = []
    books = [(accrual_rows, JournalEntry.Basis.ACCRUAL if cash_rows else JournalEntry.Basis.BOTH)] + ([(cash_rows, JournalEntry.Basis.CASH)] if cash_rows else [])
    for rows, basis in books:
        lines = [{'account': account(code, name), 'debit': amt if amt > 0 else ZERO, 'credit': -amt if amt < 0 else ZERO, 'memo': 'Opening balance'} for code, name, amt in rows]
        entries.append(ledger.post_entry(prop, as_of, lines, memo='Opening balances, moved from Yardi', basis=basis, source=JournalEntry.Source.OPENING, source_ref='yardi-opening', user=user, allow_locked=True))
    profile = ledger.profile_for(prop)
    profile.books_start = as_of
    profile.save(update_fields=['books_start'])
    return {'entries': len(entries), 'accounts_made': made, 'accounts': len(accrual_rows)}
