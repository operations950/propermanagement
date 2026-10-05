"""Step 1 of the month-end close: does the bank tie out?

Three numbers should agree at month end - the bank's statement, QuickBooks's balance for the property management bank
account, and QuickBooks's total for every property trust account under the trust parent. Two comparisons:

  statement vs QuickBooks bank     differences are timing items (outstanding checks, deposits in transit) a person enters
  QuickBooks bank vs trust total   differences are transactions that moved one side and not the other - found here by lining
                                   up, transaction by transaction, what each did to the bank and to the trust accounts

Whatever a person has explained is kept (TieOutItem); what is left is "unexplained", and the step is done when nothing is."""
import csv
import io
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from . import ledger, quickbooks
from .models import BankStatement, BankTieOut, FinancialsSettings, QuickBooksAccount, TieOutItem

ZERO = Decimal('0.00')
PENNY = Decimal('0.005')


def month_end(month):
    return ledger.next_month(month) - timedelta(days=1)


def money(value):
    return Decimal(str(value)).quantize(Decimal('0.01'))


# ---------------------------------------------------------------- which QuickBooks accounts

def suggest_accounts():
    """(bank, trust parent) guessed from the synced chart of accounts: the Bank account with "property management" in its
    name (not a trust one), and the account named "Property Management Trust". Either may be None."""
    accounts = list(QuickBooksAccount.objects.filter(active=True))
    banks = [a for a in accounts if a.account_type == 'Bank' and 'property management' in a.fully_qualified_name.lower() and 'trust' not in a.fully_qualified_name.lower()]
    parent = next((a for a in accounts if a.fully_qualified_name.lower() == 'property management trust'), None)
    return (banks[0] if len(banks) == 1 else None), parent


def configured_accounts():
    """(bank, trust parent): the saved choice, else the guess."""
    saved = FinancialsSettings.get()
    guess_bank, guess_parent = suggest_accounts()
    return saved.bank_account or guess_bank, saved.trust_parent_account or guess_parent


# ---------------------------------------------------------------- reading the statement

_AMOUNT = r'\(?-?\$?\s*\d[\d,]*\.\d{2}\)?-?'
_BALANCE_LABELS = [
    r'ending\s+(?:ledger\s+)?balance', r'closing\s+(?:ledger\s+)?balance', r'statement\s+ending\s+balance',
    r'balance\s+at\s+end\s+of\s+(?:statement\s+)?period', r'new\s+balance',
]


def _amount(text):
    raw = (text or '').strip()
    negative = raw.startswith('(') or raw.startswith('-') or raw.endswith('-') or raw.endswith(')')
    digits = re.sub(r'[^\d.]', '', raw)
    try:
        value = Decimal(digits)
    except InvalidOperation:
        return None
    return -value if negative else value


def read_statement_balance(uploaded_file):
    """(balance or None, note): a best guess at the statement's ending balance from a PDF or CSV. It only suggests - the
    person always confirms the number - so anything unreadable just returns (None, why)."""
    name = (getattr(uploaded_file, 'name', '') or '').lower()
    data = uploaded_file.read()
    uploaded_file.seek(0)
    if name.endswith('.csv'):
        return _balance_from_csv(data)
    if name.endswith('.pdf'):
        return _balance_from_pdf(data)
    return None, 'Only PDF and CSV statements can be read automatically - type the ending balance.'


def _balance_from_pdf(data):
    try:
        from pypdf import PdfReader
    except ImportError:
        return None, 'PDF reading is not installed - type the ending balance.'
    try:
        text = '\n'.join((page.extract_text() or '') for page in PdfReader(io.BytesIO(data)).pages)
    except Exception:
        return None, "Couldn't read that PDF - type the ending balance."
    for label in _BALANCE_LABELS:
        match = re.search(label + r'[^\d\-\(\$]{0,40}(' + _AMOUNT + ')', text, re.IGNORECASE)
        if match:
            value = _amount(match.group(1))
            if value is not None:
                return value, f'Read from the PDF ("{match.group(0).strip()[:60]}").'
    return None, "Couldn't find an ending balance in that PDF - type it."


def _balance_from_csv(data):
    try:
        rows = list(csv.DictReader(io.StringIO(data.decode('utf-8-sig', errors='replace'))))
    except csv.Error:
        return None, "Couldn't read that CSV - type the ending balance."
    if not rows:
        return None, 'That CSV has no rows - type the ending balance.'
    balance_col = next((c for c in rows[0] if c and 'balance' in c.lower()), None)
    date_col = next((c for c in rows[0] if c and 'date' in c.lower()), None)
    if not balance_col:
        return None, 'That CSV has no balance column - type the ending balance.'

    def when(row):
        for fmt in ('%m/%d/%Y', '%Y-%m-%d', '%m/%d/%y'):
            try:
                return datetime.strptime((row.get(date_col) or '').strip(), fmt).date()
            except ValueError:
                continue
        return None

    dated = [(when(r), i, r) for i, r in enumerate(rows)] if date_col else []
    if dated and all(d for d, _, _ in dated):
        latest = max(d for d, _, _ in dated)
        same_day = [i for d, i, _ in dated if d == latest]
        # a day's last balance is its last row in an oldest-first file and its first row in a newest-first one
        newest_first = dated[0][0] >= dated[-1][0]
        row = rows[min(same_day) if newest_first else max(same_day)]
        value = _amount(row.get(balance_col))
        if value is not None:
            return value, f'Read from the CSV row dated {latest:%b} {latest.day}: confirm it is the last balance of the month.'
    value = _amount(rows[-1].get(balance_col))
    return (value, 'Read from the last row of the CSV: confirm it.') if value is not None else (None, "Couldn't read a balance from that CSV - type it.")


# ---------------------------------------------------------------- the QuickBooks comparison

def txn_key(entry):
    return f'{entry["txn_type"]}:{entry["txn_id"]}'


def _effect(entry, account):
    """What a ledger line did to an account's balance: an asset (the bank) rises on a debit, a liability (trust) on a credit."""
    debit, credit = entry.get('debit', ZERO), entry.get('credit', ZERO)
    return debit - credit if (account is None or account.classification != 'Liability') else credit - debit


def diff_transactions(bank_entries, trust_entries, bank_account=None, trust_account=None):
    """Every transaction whose effect on the bank account differs from its effect on the trust accounts, biggest first:
    [{key, type, date, name, memo, split, bank, trust, diff}] (diff = bank - trust; amounts as strings)."""
    rows = {}

    def row(e):
        key = txn_key(e)
        if key not in rows:
            rows[key] = {'key': key, 'type': e['txn_type'], 'date': e['date'], 'name': e.get('name', ''), 'memo': e.get('memo', ''),
                         'split': e.get('split', ''), 'bank': ZERO, 'trust': ZERO}
        return rows[key]

    for e in bank_entries:
        row(e)['bank'] += _effect(e, bank_account)
    for e in trust_entries:
        row(e)['trust'] += _effect(e, trust_account)
    out = []
    for r in rows.values():
        diff = r['bank'] - r['trust']
        if abs(diff) > PENNY:
            out.append({**r, 'bank': str(r['bank']), 'trust': str(r['trust']), 'diff': str(diff)})
    return sorted(out, key=lambda r: -abs(Decimal(r['diff'])))


def _balances(token, as_of, bank, trust_parent):
    sheet, error = quickbooks.fetch_balance_sheet(token, as_of)
    if error:
        return None, error
    own, total = sheet['own'], sheet['total']
    bank_balance = own.get(bank.qb_id) if own.get(bank.qb_id) is not None else total.get(bank.qb_id)
    trust_balance = total.get(trust_parent.qb_id) if total.get(trust_parent.qb_id) is not None else own.get(trust_parent.qb_id)
    if bank_balance is None or trust_balance is None:
        return None, f'QuickBooks\'s balance sheet at {as_of} has no line for {"the bank account" if bank_balance is None else "the trust accounts"} - check the accounts chosen above.'
    return (money(bank_balance), money(trust_balance)), ''


def run(token, month, user=None):
    """Pulls QuickBooks's balances (at the end of this month and of the one before) and this month's transactions on both
    sides, and saves the result. Returns (BankTieOut, '') or (None, why)."""
    bank, trust_parent = configured_accounts()
    if bank is None or trust_parent is None:
        return None, 'Choose the bank account and the trust parent account first.'
    start, end = month, month_end(month)
    now_bal, error = _balances(token, end, bank, trust_parent)
    if error:
        return None, error
    prior_bal, error = _balances(token, start - timedelta(days=1), bank, trust_parent)
    if error:
        return None, error
    bank_entries, error = quickbooks.fetch_ledger_family(token, bank.qb_id, start, end)
    if error:
        return None, error
    trust_entries, error = quickbooks.fetch_ledger_family(token, trust_parent.qb_id, start, end)
    if error:
        return None, error
    diffs = diff_transactions(bank_entries, trust_entries, bank, trust_parent)
    obj, _ = BankTieOut.objects.update_or_create(month=month, defaults={
        'run_by': user if getattr(user, 'pk', None) else None, 'qb_bank': now_bal[0], 'qb_trust': now_bal[1],
        'prior_bank': prior_bal[0], 'prior_trust': prior_bal[1], 'differences': diffs,
    })
    return obj, ''


# ---------------------------------------------------------------- where it stands

def summary(month):
    """Everything the page and the checklist need about one month's tie-out. `status` is 'not_started' (no statement or no run
    yet), 'attention' (something is unexplained) or 'done'."""
    statement = BankStatement.objects.filter(month=month).first()
    tieout = BankTieOut.objects.filter(month=month).first()
    items = list(TieOutItem.objects.filter(month__lte=month).order_by('month', 'created_at'))
    out = {'month': month, 'statement': statement, 'tieout': tieout, 'items': items,
           'statement_items': [i for i in items if i.side == TieOutItem.Side.STATEMENT and i.month == month],
           'trust_items': [i for i in items if i.side == TieOutItem.Side.TRUST], 'status': 'not_started'}
    if tieout is None:
        return out
    accepted_keys = {i.txn_key for i in out['trust_items'] if i.txn_key and i.month == month}
    out['differences'] = [{**d, 'accepted': d['key'] in accepted_keys} for d in tieout.differences]
    out['unaccepted'] = [d for d in out['differences'] if not d['accepted']]
    trust_gap = tieout.qb_bank - tieout.qb_trust
    trust_explained = sum((i.amount for i in out['trust_items']), ZERO)
    out['trust_gap'], out['trust_explained'] = trust_gap, trust_explained
    out['trust_unexplained'] = trust_gap - trust_explained
    out['prior_gap'] = tieout.prior_bank - tieout.prior_trust
    out['month_diffs_total'] = sum((Decimal(d['diff']) for d in tieout.differences), ZERO)
    # the gap at month end should be the gap at the end of the month before plus this month's differing transactions
    out['untraced'] = trust_gap - out['prior_gap'] - out['month_diffs_total']
    if statement is not None:
        out['statement_gap'] = statement.ending_balance - tieout.qb_bank
        out['statement_explained'] = sum((i.amount for i in out['statement_items']), ZERO)
        out['statement_unexplained'] = out['statement_gap'] - out['statement_explained']
        balanced = abs(out['statement_unexplained']) < PENNY and abs(out['trust_unexplained']) < PENNY
        out['status'] = 'done' if balanced else 'attention'
    else:
        out['status'] = 'not_started'
    return out


def status(month):
    return summary(month)['status']
