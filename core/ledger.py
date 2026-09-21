"""The month-end close: each rental's QuickBooks transactions, coded by a person,
then closed and locked.

QuickBooks is the system of record. This module pulls each mapped rental's
transactions in (one entry per QuickBooks transaction per account) and keeps them
in step with QuickBooks — but never disturbs work already done:

  * a transaction is identified by QuickBooks's own (account role, type, id), so a
    resync recognises the one it already has. If it changed (amount, date or
    account) it is updated, KEEPS its coding, and is flagged "changed in
    QuickBooks" for a glance; if it left the account (voided, deleted, or re-coded
    to another rental, where it appears as a new line) it is marked removed. Nothing
    already coded has to be coded again.
  * once a rental's month is closed the month is LOCKED: its transactions are
    frozen, whatever QuickBooks does afterwards. Later edits to that month are
    recorded (ClosedMonthChange) so the team can see QuickBooks and the books have
    drifted, but they do not flow in — the closed figures were used to pay the
    owner. An error found later is fixed in the current month.

Coding (what the team does each month):
  * reimbursable-expense account: every line is an expense unless it is the monthly
    reimbursement (the credit that pays us back out of the trust account);
  * trust account: money in is an owner deposit; money out is an expense unless it
    is the owner payment, the expense reimbursement to us, or commission.
Defaults (and a few suggestions, such as a transfer between the two accounts being
the reimbursement) mean most lines need no touch; a person still has to look at
each line — "accept as shown" or change it — before the month can close."""
import hashlib
import logging
import re
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from . import quickbooks
from .models import ClosedMonthChange, FinancialsSettings, LedgerLine, MonthClose, Property, QuickBooksToken

logger = logging.getLogger(__name__)

Category = LedgerLine.Category
Role = LedgerLine.Role
ROLE_CATEGORIES = {
    Role.EXPENSE: (Category.EXPENSE, Category.REIMBURSEMENT),
    Role.TRUST: (Category.EXPENSE, Category.DEPOSIT, Category.OWNER_PAYMENT, Category.REIMBURSEMENT, Category.COMMISSION),
}
STALE_AFTER = timedelta(hours=24)     # a close must rest on a recent sync
ZERO = Decimal('0.00')


class LedgerSyncError(Exception):
    """A rental's transactions couldn't be read from QuickBooks."""


class CloseError(ValueError):
    """An action the books don't allow (say, coding a closed month)."""


def month_of(day):
    return day.replace(day=1)


def next_month(day):
    return date(day.year + (day.month == 12), day.month % 12 + 1, 1)


def previous_month(day):
    return (day.replace(day=1) - timedelta(days=1)).replace(day=1)


def books_start():
    """The first month managed here: the saved start, else last month."""
    return FinancialsSettings.get().books_start or previous_month(timezone.localdate())


def rentals():
    """The rentals that go through the close: active real short-term rentals."""
    return list(
        Property.objects.filter(is_active=True, is_general=False, property_type=Property.Type.SHORT_TERM_RENTAL)
        .select_related('qb_expense_account', 'qb_trust_account').order_by('name'),
    )


def closed_months(prop):
    return set(MonthClose.objects.filter(property=prop).values_list('month', flat=True))


def is_closed(prop, month):
    return MonthClose.objects.filter(property=prop, month=month_of(month)).exists()


# --- turning a QuickBooks report into entries ------------------------------------------------

def _debit_positive(account, entry):
    """The entry as debit-positive, however the report gave it: debit and credit
    columns if present, else the natural signed amount (positive = a debit for asset
    and expense accounts, a credit for liability, equity and revenue ones)."""
    if 'debit' in entry:
        return entry['debit'] - entry['credit']
    natural_debit = account is None or account.classification in ('Asset', 'Expense')
    return entry['amount'] if natural_debit else -entry['amount']


def _flow(role, account, entry):
    """Signed so the expense account is positive for an expense charged and the trust
    account is positive for money coming in."""
    value = _debit_positive(account, entry)
    if role == Role.EXPENSE:
        return value
    is_asset = account is None or account.classification == 'Asset'
    return value if is_asset else -value


def group_entries(role, account, entries):
    """One dict per QuickBooks transaction (a transaction can put several lines
    into the same account): flow summed, the first date, and the descriptive text."""
    grouped = {}
    for entry in entries:
        key = (entry['txn_type'], entry['txn_id'])
        flow = _flow(role, account, entry).quantize(Decimal('0.01'))
        current = grouped.get(key)
        if current is None:
            grouped[key] = {
                'txn_type': entry['txn_type'], 'txn_id': entry['txn_id'], 'date': datetime.strptime(entry['date'], '%Y-%m-%d').date(),
                'doc_num': entry['doc_num'], 'payee': entry['name'], 'memo': entry['memo'], 'split': entry['split'], 'flow': flow,
            }
        else:
            current['flow'] += flow
            for src, dst in (('name', 'payee'), ('memo', 'memo'), ('split', 'split'), ('doc_num', 'doc_num')):
                if entry[src] and entry[src] not in (current[dst] or '').split(' / '):
                    current[dst] = f'{current[dst]} / {entry[src]}' if current[dst] else entry[src]
    return grouped


def fingerprint(unit):
    """What counts as QuickBooks having CHANGED a transaction: its date, its amount
    or the account on the other side. (Wording edits update quietly.)"""
    return hashlib.sha1(f'{unit["date"]}|{unit["flow"]}|{unit["split"]}'.encode()).hexdigest()


# --- default and suggested coding ---------------------------------------------------------------

def _norm(text):
    return re.sub(r'[^a-z0-9]+', ' ', (text or '').lower()).strip()


def _names_account(text, account):
    """Does this split text (QuickBooks's other-account label) name that account?"""
    if account is None or not text:
        return False
    t = _norm(text)
    if not t or t == 'split':
        return False
    return t == _norm(account.fully_qualified_name) or t == _norm(account.name) or t.endswith(_norm(account.name))


def suggest(prop, role, flow, split, payee, memo):
    """(category, source) for a new line: what most lines are, plus a suggestion
    where the transaction plainly is one of the special ones."""
    text = _norm(f'{payee} {memo} {split}')
    if role == Role.EXPENSE:
        if flow < 0 and (_names_account(split, prop.qb_trust_account) or 'reimburs' in text):
            return Category.REIMBURSEMENT, LedgerLine.Source.SUGGESTED
        return Category.EXPENSE, LedgerLine.Source.DEFAULT
    if flow > 0:
        return Category.DEPOSIT, LedgerLine.Source.DEFAULT
    if _names_account(split, prop.qb_expense_account) or 'reimburs' in text:
        return Category.REIMBURSEMENT, LedgerLine.Source.SUGGESTED
    if 'commission' in text:
        return Category.COMMISSION, LedgerLine.Source.SUGGESTED
    if re.search(r'owner (payment|draw|distribution|payout)|distribution', text):
        return Category.OWNER_PAYMENT, LedgerLine.Source.SUGGESTED
    return Category.EXPENSE, LedgerLine.Source.DEFAULT


# --- the sync ---------------------------------------------------------------------------------------

def _describe(old, new):
    parts = []
    if old['date'] != new['date']:
        parts.append(f'date {old["date"]:%b} {old["date"].day} → {new["date"]:%b} {new["date"].day}')
    if old['flow'] != new['flow']:
        parts.append(f'amount ${abs(old["flow"]):,.2f} → ${abs(new["flow"]):,.2f}')
    if old['split'] != new['split']:
        parts.append(f'account {old["split"] or "—"} → {new["split"] or "—"}')
    return '; '.join(parts)[:290]


def _record_drift(prop, role, month, txn_type, txn_id, kind, detail):
    change, created = ClosedMonthChange.objects.get_or_create(
        property=prop, role=role, txn_type=txn_type, txn_id=txn_id, kind=kind, defaults={'month': month, 'detail': detail[:400]},
    )
    if not created and change.detail != detail[:400]:
        change.detail, change.resolved = detail[:400], False
        change.save(update_fields=['detail', 'resolved'])
    return change


def _apply(prop, role, account, grouped, start, end, closed, now):
    """Brings the stored lines of one account in step with what QuickBooks shows."""
    counts = {'new': 0, 'updated': 0, 'removed': 0, 'unchanged': 0, 'ignored_closed': 0}
    existing = {(l.txn_type, l.txn_id): l for l in LedgerLine.objects.filter(property=prop, role=role)}
    seen = set()
    for key, unit in grouped.items():
        line = existing.get(key)
        month = month_of(unit['date'])
        fp = fingerprint(unit)
        if line is None:
            if month in closed:
                _record_drift(prop, role, month, unit['txn_type'], unit['txn_id'], 'new', f'{unit["date"]:%b} {unit["date"].day}: {unit["payee"] or unit["memo"] or unit["txn_type"]} ${abs(unit["flow"]):,.2f} added to a closed month')
                counts['ignored_closed'] += 1
                continue
            category, source = suggest(prop, role, unit['flow'], unit['split'], unit['payee'], unit['memo'])
            LedgerLine.objects.create(
                property=prop, role=role, account=account, txn_type=unit['txn_type'], txn_id=unit['txn_id'], txn_date=unit['date'],
                month=month, doc_num=unit['doc_num'][:60], payee=unit['payee'][:300], memo=unit['memo'][:500], split=unit['split'][:300],
                flow=unit['flow'], fingerprint=fp, category=category, category_source=source, last_seen_at=now,
            )
            counts['new'] += 1
            continue
        seen.add(key)
        frozen = line.locked_at is not None or line.month in closed
        if frozen:
            if fp != line.fingerprint or line.status == LedgerLine.Status.REMOVED:
                old = {'date': line.txn_date, 'flow': line.flow, 'split': line.split}
                _record_drift(prop, role, line.month, line.txn_type, line.txn_id, 'changed', _describe(old, unit) or 'reappeared in QuickBooks')
                counts['ignored_closed'] += 1
            else:
                counts['unchanged'] += 1
            continue
        if month in closed and month != line.month:
            # QuickBooks moved it into a month that is already closed: not applied.
            _record_drift(prop, role, month, line.txn_type, line.txn_id, 'changed', f'date moved into a closed month ({unit["date"]:%b} {unit["date"].day})')
            counts['ignored_closed'] += 1
            continue
        changed_fields = []
        was_removed = line.status == LedgerLine.Status.REMOVED
        if was_removed:
            line.status, line.removed_at = LedgerLine.Status.ACTIVE, None
            changed_fields += ['status', 'removed_at']
        if fp != line.fingerprint:
            old = {'date': line.txn_date, 'flow': line.flow, 'split': line.split}
            note = _describe(old, unit)
            sign_flipped = (line.flow > 0) != (unit['flow'] > 0)
            line.txn_date, line.month, line.flow, line.split, line.fingerprint = unit['date'], month, unit['flow'], unit['split'][:300], fp
            line.changed_in_qb, line.change_note = True, note
            if sign_flipped:
                line.category, line.category_source = suggest(prop, role, unit['flow'], unit['split'], unit['payee'], unit['memo'])
                line.reviewed = False
                line.change_note = (note + '; direction reversed, category reset')[:300]
            changed_fields += ['txn_date', 'month', 'flow', 'split', 'fingerprint', 'changed_in_qb', 'change_note', 'category', 'category_source', 'reviewed']
        elif was_removed:
            line.changed_in_qb, line.change_note = True, 'back in QuickBooks'
            changed_fields += ['changed_in_qb', 'change_note']
        for attr, value in (('doc_num', unit['doc_num'][:60]), ('payee', unit['payee'][:300]), ('memo', unit['memo'][:500])):
            if getattr(line, attr) != value:
                setattr(line, attr, value)
                changed_fields.append(attr)
        did_update = bool(changed_fields)
        line.last_seen_at = now
        changed_fields.append('last_seen_at')
        line.save(update_fields=sorted(set(changed_fields)))
        counts['updated' if did_update else 'unchanged'] += 1

    for key, line in existing.items():
        if key in seen or line.txn_date < start or line.txn_date > end or line.status != LedgerLine.Status.ACTIVE:
            continue
        if line.locked_at is not None or line.month in closed:
            _record_drift(prop, role, line.month, line.txn_type, line.txn_id, 'removed', f'{line.txn_date:%b} {line.txn_date.day}: {line.payee or line.memo or line.txn_type} ${abs(line.flow):,.2f} is no longer in QuickBooks')
            counts['ignored_closed'] += 1
            continue
        line.status, line.removed_at = LedgerLine.Status.REMOVED, now
        line.save(update_fields=['status', 'removed_at'])
        counts['removed'] += 1
    return counts


def sync_property(token, prop, start=None, end=None, now=None):
    """Pulls this rental's transactions from both mapped accounts and reconciles
    them with what is stored (see the module docstring). Returns per-role counts;
    raises LedgerSyncError if QuickBooks can't be read (nothing is changed then)."""
    now = now or timezone.now()
    start = start or books_start()
    end = end or timezone.localdate()
    fetched = {}
    for role, account in ((Role.EXPENSE, prop.qb_expense_account), (Role.TRUST, prop.qb_trust_account)):
        if account is None:
            continue
        entries, error = quickbooks.fetch_ledger(token, account.qb_id, start, end)
        if error:
            raise LedgerSyncError(error)
        fetched[role] = (account, group_entries(role, account, entries))
    closed = closed_months(prop)
    summary = {}
    with transaction.atomic():
        for role, (account, grouped) in fetched.items():
            summary[role] = _apply(prop, role, account, grouped, start, end, closed, now)
        Property.objects.filter(pk=prop.pk).update(ledger_synced_at=now)
    return summary


def sync_all(now=None):
    """Syncs every mapped rental; returns (properties synced, error text). One
    rental failing doesn't stop the others, and its own last-sync time stays old so
    it can't be closed on stale data."""
    token = QuickBooksToken.objects.first()
    if token is None:
        return 0, 'QuickBooks is not connected.'
    now = now or timezone.now()
    done, errors = 0, []
    for prop in rentals():
        if prop.qb_expense_account_id is None and prop.qb_trust_account_id is None:
            continue
        try:
            sync_property(token, prop, now=now)
            done += 1
        except LedgerSyncError as exc:
            errors.append(f'{prop.name}: {exc}')
        except Exception:
            logger.exception('Ledger sync failed for %s', prop.name)
            errors.append(f'{prop.name}: something went wrong reading its transactions.')
    token.ledger_sync_error = ' | '.join(errors)[:255]
    if not errors:
        token.ledger_synced_at = now
    token.save(update_fields=['ledger_sync_error', 'ledger_synced_at'])
    return done, token.ledger_sync_error


# --- coding ----------------------------------------------------------------------------------------------

def month_lines(prop, month, include_removed=False):
    qs = LedgerLine.objects.filter(property=prop, month=month_of(month))
    if not include_removed:
        qs = qs.filter(status=LedgerLine.Status.ACTIVE)
    return qs.order_by('role', 'txn_date', 'txn_type', 'txn_id')


def _guard_open(prop, month):
    if is_closed(prop, month):
        raise CloseError(f'{month_of(month):%B %Y} is closed for {prop.name}. It can no longer be changed; put a correction in the current month.')


@transaction.atomic
def code_lines(prop, month, user, assignments):
    """Apply {line id: category} chosen on the coding screen. A line whose category
    changes becomes "coded by a person"; every line included is marked reviewed.
    Returns how many changed category."""
    _guard_open(prop, month)
    lines = {l.pk: l for l in month_lines(prop, month)}
    changed, now = 0, timezone.now()
    for pk, category in assignments.items():
        line = lines.get(pk)
        if line is None:
            continue
        if category not in ROLE_CATEGORIES[line.role]:
            raise CloseError(f'"{category}" is not a category for the {line.get_role_display().lower()}.')
        update = ['reviewed', 'coded_by', 'coded_at']
        if category != line.category:
            line.category, line.category_source = category, LedgerLine.Source.USER
            update += ['category', 'category_source']
            changed += 1
        line.reviewed, line.coded_by, line.coded_at = True, user, now
        line.save(update_fields=update)
    return changed


@transaction.atomic
def accept_all(prop, month, user):
    """Marks every line of the month reviewed as it stands (defaults included).
    Returns how many were newly reviewed."""
    _guard_open(prop, month)
    return month_lines(prop, month).filter(reviewed=False).update(reviewed=True, coded_by=user, coded_at=timezone.now())


@transaction.atomic
def acknowledge_changes(prop, month):
    """The team has looked at what QuickBooks changed."""
    _guard_open(prop, month)
    return month_lines(prop, month).filter(changed_in_qb=True).update(changed_in_qb=False)


# --- the month's figures and checks ----------------------------------------------------------------------

def totals(prop, month, lines=None):
    """The month in dollars, from the lines as currently coded (positive numbers)."""
    lines = list(month_lines(prop, month)) if lines is None else lines
    total = {k: ZERO for k in ('deposits', 'owner_payment', 'commission', 'reimbursement_trust', 'reimbursement_expense', 'expenses_reimbursable', 'expenses_direct', 'trust_net')}
    for l in lines:
        if l.role == Role.EXPENSE:
            if l.category == Category.REIMBURSEMENT:
                total['reimbursement_expense'] += -l.flow
            else:
                total['expenses_reimbursable'] += l.flow
        else:
            total['trust_net'] += l.flow
            if l.category == Category.DEPOSIT:
                total['deposits'] += l.flow
            elif l.category == Category.OWNER_PAYMENT:
                total['owner_payment'] += -l.flow
            elif l.category == Category.COMMISSION:
                total['commission'] += -l.flow
            elif l.category == Category.REIMBURSEMENT:
                total['reimbursement_trust'] += -l.flow
            else:
                total['expenses_direct'] += -l.flow
    total['expenses_total'] = total['expenses_reimbursable'] + total['expenses_direct']
    return total


def checks(prop, month, now=None):
    """What stands between this rental-month and being closed. Each item is
    {'key', 'level': 'ok'|'warn'|'block', 'text'}: a 'block' has to be fixed, a
    'warn' can be acknowledged (it is recorded with the close)."""
    now = now or timezone.now()
    month = month_of(month)
    items = []

    def add(key, level, text):
        items.append({'key': key, 'level': level, 'text': text})

    if is_closed(prop, month):
        add('closed', 'block', f'{month:%B %Y} is already closed.')
        return items
    if prop.qb_expense_account_id is None or prop.qb_trust_account_id is None:
        add('accounts', 'block', 'This rental is not tied to both QuickBooks accounts yet.')
    if timezone.localdate() < next_month(month):
        add('month_over', 'block', f'{month:%B %Y} is not over yet.')
    synced = prop.ledger_synced_at
    if synced is None:
        add('sync', 'block', 'Its transactions have never been pulled in from QuickBooks — sync first.')
    elif now - synced > STALE_AFTER:
        add('sync', 'block', f'The last sync was {timezone.localtime(synced):%b} {timezone.localtime(synced).day}; sync again so you close what QuickBooks shows now.')
    lines = list(month_lines(prop, month))
    if not lines:
        add('empty', 'warn', 'No transactions in either account this month.')
    unreviewed = sum(1 for l in lines if not l.reviewed)
    if unreviewed:
        add('unreviewed', 'block', f'{unreviewed} line{"" if unreviewed == 1 else "s"} still to review — accept them as shown or change them.')
    changed = sum(1 for l in lines if l.changed_in_qb)
    if changed:
        add('changed', 'block', f'{changed} line{"" if changed == 1 else "s"} changed in QuickBooks since coded — take a look and confirm.')
    if lines:
        t = totals(prop, month, lines)
        if t['reimbursement_trust'] != t['reimbursement_expense']:
            add('reimbursement_mismatch', 'warn', f'The reimbursement out of the trust account (${t["reimbursement_trust"]:,.2f}) does not equal the reimbursement credited to the expense account (${t["reimbursement_expense"]:,.2f}).')
        if t['owner_payment'] == 0 and t['deposits'] > 0:
            add('no_owner_payment', 'warn', 'No owner payment is coded this month although there were deposits.')
        if t['expenses_reimbursable'] > 0 and t['reimbursement_trust'] == 0:
            add('no_reimbursement', 'warn', 'There are reimbursable expenses but no reimbursement is coded from the trust account.')
    return items


def can_close(items):
    return not any(i['level'] == 'block' for i in items)


@transaction.atomic
def close_month(prop, month, user, acknowledged=(), note=''):
    """Closes and locks a rental's month. Refused while anything blocks it, or a
    warning hasn't been acknowledged."""
    month = month_of(month)
    items = checks(prop, month)
    blocks = [i for i in items if i['level'] == 'block']
    if blocks:
        raise CloseError(blocks[0]['text'])
    warns = [i['key'] for i in items if i['level'] == 'warn']
    missing = [k for k in warns if k not in set(acknowledged)]
    if missing:
        raise CloseError('Acknowledge each warning before closing: ' + ', '.join(missing))
    lines = list(month_lines(prop, month))
    t = totals(prop, month, lines)
    snapshot = {k: str(v) for k, v in t.items()}
    snapshot['lines'] = len(lines)
    close = MonthClose.objects.create(
        property=prop, month=month, closed_by=user, totals=snapshot, warnings_acknowledged=warns, note=note.strip()[:500],
    )
    LedgerLine.objects.filter(property=prop, month=month).update(locked_at=timezone.now())
    return close


def closed_summary(close):
    """A closed month's frozen figures as Decimals."""
    return {k: (Decimal(v) if k != 'lines' else int(v)) for k, v in close.totals.items()}


def status_row(prop, month, now=None):
    """Everything the close overview shows for one rental-month."""
    month = month_of(month)
    close = MonthClose.objects.filter(property=prop, month=month).first()
    lines = list(month_lines(prop, month))
    items = checks(prop, month, now) if close is None else []
    drift = ClosedMonthChange.objects.filter(property=prop, month=month, resolved=False).count()
    if close:
        state = 'closed'
    elif prop.qb_expense_account_id is None or prop.qb_trust_account_id is None:
        state = 'needs_accounts'
    elif can_close(items):
        state = 'ready'
    else:
        state = 'open'
    return {
        'property': prop, 'state': state, 'close': close, 'lines': len(lines), 'unreviewed': sum(1 for l in lines if not l.reviewed),
        'changed': sum(1 for l in lines if l.changed_in_qb), 'checks': items, 'drift': drift,
        'totals': closed_summary(close) if close else (totals(prop, month, lines) if lines else None),
    }
