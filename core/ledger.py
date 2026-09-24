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

The books. Most rentals keep ONE set of books (the property's two accounts). A
rental can instead keep separate books per unit, each unit with its own two accounts
and its own close; the property is then the sum of its units. The choice can change
at any time: a month keeps the shape it was closed in, months still open follow the
current setting. Everything here works on a `Book` — a property, or one unit of one.

Every month also has an income reconciliation (core/recon.py) that must be clean
before it closes.

Coding (what the team does each month):
  * reimbursable-expense account: every line is an expense unless it is the monthly
    reimbursement (the credit that pays us back out of the trust account);
  * trust account: money in is an income deposit (a booking payout) unless it isn't
    income (a refund, say — code that as an expense); money out is an expense unless
    it is the owner payment, the expense reimbursement to us, or commission.
Defaults (and a few suggestions, such as a transfer between the two accounts being
the reimbursement) mean most lines need no touch; a person still has to look at
each line — "accept as shown" or change it — before the month can close."""
import hashlib
import logging
import re
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.utils import timezone

from . import quickbooks
from .models import ClosedMonthChange, FinancialsSettings, LedgerLine, MonthClose, Property, QuickBooksToken, ReconAcceptance, Unit

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


# --- books ---------------------------------------------------------------------------------------------

class Book:
    """One set of books: a whole property, or one unit of a property that keeps
    unit-level books. It carries the two accounts to read and is the unit of coding,
    reconciling and closing."""

    def __init__(self, prop, unit=None):
        self.property = prop
        self.unit = unit

    @property
    def level(self):
        return Property.FinancialsLevel.UNIT if self.unit is not None else Property.FinancialsLevel.PROPERTY

    @property
    def owner(self):
        return self.unit if self.unit is not None else self.property

    @property
    def name(self):
        return f'{self.property.name} — {self.unit.label}' if self.unit is not None else self.property.name

    @property
    def label(self):
        return self.unit.label if self.unit is not None else self.property.name

    @property
    def qb_expense_account(self):
        return self.owner.qb_expense_account

    @property
    def qb_trust_account(self):
        return self.owner.qb_trust_account

    @property
    def qb_expense_account_id(self):
        return self.owner.qb_expense_account_id

    @property
    def qb_trust_account_id(self):
        return self.owner.qb_trust_account_id

    @property
    def ledger_synced_at(self):
        """Read fresh: a sync stamps the row, whichever object the caller holds."""
        return type(self.owner).objects.filter(pk=self.owner.pk).values_list('ledger_synced_at', flat=True).first()

    @property
    def mapped(self):
        return self.qb_expense_account_id is not None and self.qb_trust_account_id is not None

    def scope(self):
        """Filter/create arguments that pick this book's rows out of the ledger tables."""
        return {'property': self.property, 'unit': self.unit}

    def stamp(self, now):
        type(self.owner).objects.filter(pk=self.owner.pk).update(ledger_synced_at=now)
        self.owner.ledger_synced_at = now

    def url_args(self, month):
        args = [month.strftime('%Y-%m') if hasattr(month, 'strftime') else month, self.property.pk]
        return args + ([self.unit.pk] if self.unit is not None else [])

    def _key(self):
        return (self.property.pk, self.unit.pk if self.unit is not None else None)

    def __eq__(self, other):
        return isinstance(other, Book) and self._key() == other._key()

    def __hash__(self):
        return hash(self._key())

    def __repr__(self):
        return f'<Book {self.name}>'


def _book(target):
    """A Property (the whole-property books, as before) or a Book."""
    return target if isinstance(target, Book) else Book(target)


def month_level(prop, month):
    """The shape a month's books have: what it was closed in, else the property's
    current setting. (A month already closed never changes shape.)"""
    level = MonthClose.objects.filter(property=prop, month=month_of(month)).values_list('level', flat=True).first()
    return level or prop.financials_level


def _units_of(prop):
    return list(prop.units.filter(is_active=True).select_related('qb_expense_account', 'qb_trust_account').order_by('label'))


def books_for(prop, month):
    """The books a month is kept in: the property's one set, or one per unit (the
    active units, plus any that already have a close or lines in that month)."""
    month = month_of(month)
    if month_level(prop, month) != Property.FinancialsLevel.UNIT:
        return [Book(prop)]
    units = {u.pk: u for u in _units_of(prop)}
    extra = set(MonthClose.objects.filter(property=prop, month=month, unit__isnull=False).values_list('unit_id', flat=True))
    extra |= set(LedgerLine.objects.filter(property=prop, month=month, unit__isnull=False).values_list('unit_id', flat=True))
    for u in Unit.objects.filter(pk__in=extra - set(units)).select_related('qb_expense_account', 'qb_trust_account'):
        units[u.pk] = u
    return [Book(prop, u) for u in sorted(units.values(), key=lambda u: u.label)]


def sync_books(prop):
    """The books whose transactions are read from QuickBooks: the current shape's, plus
    any earlier shape that has closed months (kept so a change to those still shows up
    as drift). Only books with at least one account tied."""
    unit_mode = prop.financials_level == Property.FinancialsLevel.UNIT
    books = [Book(prop, u) for u in _units_of(prop)] if unit_mode else [Book(prop)]
    closes = MonthClose.objects.filter(property=prop)
    if unit_mode and closes.filter(level=Property.FinancialsLevel.PROPERTY).exists():
        books.append(Book(prop))
    known = {b.unit.pk for b in books if b.unit is not None}
    closed_units = set(closes.filter(unit__isnull=False).values_list('unit_id', flat=True)) - known
    books += [Book(prop, u) for u in Unit.objects.filter(pk__in=closed_units).select_related('qb_expense_account', 'qb_trust_account')]
    seen, out = set(), []
    for b in books:
        if b not in seen and (b.qb_expense_account_id or b.qb_trust_account_id):
            seen.add(b)
            out.append(b)
    return out


def closed_months(book):
    return set(MonthClose.objects.filter(**_book(book).scope()).values_list('month', flat=True))


def is_closed(book, month):
    return MonthClose.objects.filter(month=month_of(month), **_book(book).scope()).exists()


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
    where the transaction plainly is one of the special ones. `prop` is a Property
    or a Book — whichever owns the two accounts."""
    prop = _book(prop)
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


def guess_settlements(book, month):
    """Which lines are the money that settles LAST month, from what last month's books say is owed.

    What was owed at the end of last month is known to the cent (prior_settlement): the reimbursement to us
    is that month's reimbursable expenses, the commission and the owner payment are theirs. So a money-out
    line of the trust account for exactly one of those amounts is almost surely that payment. And in the
    expense account, credits are usually only the reimbursement: the credit that equals what was owed, or
    else the biggest one when a trust-account line of the very same amount went out. A line a person has
    chosen (or reviewed) is left alone; a guess is a suggestion and still has to be looked at. Returns how
    many lines were changed."""
    book, month = _book(book), month_of(month)
    if is_closed(book, month):
        return 0
    lines = list(month_lines(book, month))
    if not lines:
        return 0
    open_ = lambda l: not l.reviewed and l.category_source != LedgerLine.Source.USER and l.locked_at is None
    changed = []

    def mark(line, category):
        if line.category != category or line.category_source != LedgerLine.Source.SUGGESTED:
            line.category, line.category_source = category, LedgerLine.Source.SUGGESTED
            line.save(update_fields=['category', 'category_source'])
            changed.append(line.pk)

    trust_out = [l for l in lines if l.role == Role.TRUST and l.flow < 0]
    credits = [l for l in lines if l.role == Role.EXPENSE and l.flow < 0]
    claimed = set()
    owed = prior_settlement(book, month)
    if owed['prior_status'] == 'ok':
        for category, amount in ((Category.REIMBURSEMENT, owed['prior_reimbursable']), (Category.COMMISSION, owed['prior_commission']), (Category.OWNER_PAYMENT, owed['prior_owner'])):
            if not amount or amount <= 0:
                continue
            hits = [l for l in trust_out if l.pk not in claimed and open_(l) and -l.flow == amount]
            if len(hits) == 1:
                mark(hits[0], category)
                claimed.add(hits[0].pk)
            if category == Category.REIMBURSEMENT:
                same = [l for l in credits if open_(l) and -l.flow == amount]
                if len(same) == 1:
                    mark(same[0], Category.REIMBURSEMENT)
    if credits:
        biggest = min(credits, key=lambda l: l.flow)
        twins = [l for l in trust_out if l.flow == biggest.flow and l.pk not in claimed]
        if len(twins) == 1 and (twins[0].category == Category.REIMBURSEMENT or open_(twins[0])):
            if open_(biggest):
                mark(biggest, Category.REIMBURSEMENT)
            if open_(twins[0]):
                mark(twins[0], Category.REIMBURSEMENT)
    return len(changed)


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


def _record_drift(book, role, month, txn_type, txn_id, kind, detail):
    change, created = ClosedMonthChange.objects.get_or_create(
        role=role, txn_type=txn_type, txn_id=txn_id, kind=kind, defaults={'month': month, 'detail': detail[:400]}, **book.scope(),
    )
    if not created and change.detail != detail[:400]:
        change.detail, change.resolved = detail[:400], False
        change.save(update_fields=['detail', 'resolved'])
    return change


def _apply(book, role, account, grouped, start, end, closed, now, level_of):
    """Brings the stored lines of one account in step with what QuickBooks shows.
    Months whose books have a different shape from this book's (a month closed before
    the property switched between one set of books and unit-level books) are not this
    book's to touch."""
    counts = {'new': 0, 'updated': 0, 'removed': 0, 'unchanged': 0, 'ignored_closed': 0}
    existing = {(l.txn_type, l.txn_id): l for l in LedgerLine.objects.filter(role=role, **book.scope())}
    seen = set()
    for key, unit in grouped.items():
        line = existing.get(key)
        month = month_of(unit['date'])
        fp = fingerprint(unit)
        if line is None:
            if level_of(month) != book.level:
                continue
            if month in closed:
                _record_drift(book, role, month, unit['txn_type'], unit['txn_id'], 'new', f'{unit["date"]:%b} {unit["date"].day}: {unit["payee"] or unit["memo"] or unit["txn_type"]} ${abs(unit["flow"]):,.2f} added to a closed month')
                counts['ignored_closed'] += 1
                continue
            category, source = suggest(book, role, unit['flow'], unit['split'], unit['payee'], unit['memo'])
            LedgerLine.objects.create(
                role=role, account=account, txn_type=unit['txn_type'], txn_id=unit['txn_id'], txn_date=unit['date'],
                month=month, doc_num=unit['doc_num'][:60], payee=unit['payee'][:300], memo=unit['memo'][:500], split=unit['split'][:300],
                flow=unit['flow'], fingerprint=fp, category=category, category_source=source, last_seen_at=now, **book.scope(),
            )
            counts['new'] += 1
            continue
        seen.add(key)
        frozen = line.locked_at is not None or line.month in closed
        if frozen:
            if fp != line.fingerprint or line.status == LedgerLine.Status.REMOVED:
                old = {'date': line.txn_date, 'flow': line.flow, 'split': line.split}
                _record_drift(book, role, line.month, line.txn_type, line.txn_id, 'changed', _describe(old, unit) or 'reappeared in QuickBooks')
                counts['ignored_closed'] += 1
            else:
                counts['unchanged'] += 1
            continue
        if month in closed and month != line.month:
            # QuickBooks moved it into a month that is already closed: not applied.
            _record_drift(book, role, month, line.txn_type, line.txn_id, 'changed', f'date moved into a closed month ({unit["date"]:%b} {unit["date"].day})')
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
                line.category, line.category_source = suggest(book, role, unit['flow'], unit['split'], unit['payee'], unit['memo'])
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
        if key in seen or line.txn_date < start or line.txn_date > end or line.status != LedgerLine.Status.ACTIVE or level_of(line.month) != book.level:
            continue
        if line.locked_at is not None or line.month in closed:
            _record_drift(book, role, line.month, line.txn_type, line.txn_id, 'removed', f'{line.txn_date:%b} {line.txn_date.day}: {line.payee or line.memo or line.txn_type} ${abs(line.flow):,.2f} is no longer in QuickBooks')
            counts['ignored_closed'] += 1
            continue
        line.status, line.removed_at = LedgerLine.Status.REMOVED, now
        line.save(update_fields=['status', 'removed_at'])
        counts['removed'] += 1
    return counts


def _level_lookup(prop):
    """month -> the shape its books have, looked up once per sync."""
    cache = {}

    def level_of(month):
        month = month_of(month)
        if month not in cache:
            cache[month] = month_level(prop, month)
        return cache[month]
    return level_of


def sync_book(token, book, start=None, end=None, now=None):
    """Pulls one set of books' transactions from its mapped accounts and reconciles
    them with what is stored (see the module docstring). Returns per-role counts;
    raises LedgerSyncError if QuickBooks can't be read (nothing is changed then)."""
    now = now or timezone.now()
    start = start or books_start()
    end = end or timezone.localdate()
    fetched = {}
    for role, account in ((Role.EXPENSE, book.qb_expense_account), (Role.TRUST, book.qb_trust_account)):
        if account is None:
            continue
        entries, error = quickbooks.fetch_ledger(token, account.qb_id, start, end)
        if error:
            raise LedgerSyncError(error)
        fetched[role] = (account, group_entries(role, account, entries))
    closed = closed_months(book)
    level_of = _level_lookup(book.property)
    summary = {}
    with transaction.atomic():
        for role, (account, grouped) in fetched.items():
            summary[role] = _apply(book, role, account, grouped, start, end, closed, now, level_of)
        for month in sorted(set(LedgerLine.objects.filter(status=LedgerLine.Status.ACTIVE, locked_at__isnull=True, **book.scope()).values_list('month', flat=True)) - closed):
            guess_settlements(book, month)      # oldest first: a month's guesses rest on what the month before it came to
        book.stamp(now)
    return summary


def sync_property(token, prop, start=None, end=None, now=None):
    """Syncs every set of books a property keeps (its one set, or one per unit, plus
    any earlier shape that still has closed months). Returns per-role counts added up
    across them; raises LedgerSyncError if QuickBooks can't be read."""
    if isinstance(prop, Book):
        return sync_book(token, prop, start, end, now)
    now = now or timezone.now()
    total = {}
    for book in sync_books(prop):
        for role, counts in sync_book(token, book, start, end, now).items():
            bucket = total.setdefault(role, {k: 0 for k in counts})
            for k, v in counts.items():
                bucket[k] += v
    prop.ledger_synced_at = Property.objects.filter(pk=prop.pk).values_list('ledger_synced_at', flat=True).first()
    return total


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
        if not sync_books(prop):
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


# --- switching between one set of books and unit-level books --------------------------------------------

def set_financials_level(prop, level):
    """Changes how a property's books are kept from now on: one set for the property,
    or one per unit. Months already closed keep the shape they were closed in. Months
    still open take the new shape, so what was pulled in and coded for them under the
    old shape is discarded and re-read from the new accounts on the next sync. Refused
    while a month is part-closed (some of its books closed, others not). Returns how
    many open lines were discarded (and how many of those a person had reviewed)."""
    if level not in Property.FinancialsLevel.values:
        raise CloseError('Choose one set of books for the property, or separate books for each unit.')
    if prop.financials_level == level:
        return {'discarded': 0, 'reviewed': 0}
    for month in sorted(set(MonthClose.objects.filter(property=prop).values_list('month', flat=True))):
        books = books_for(prop, month)
        closed = sum(1 for b in books if is_closed(b, month))
        if closed < len(books):
            raise CloseError(f'{month:%B %Y} is closed for {closed} of its {len(books)} sets of books. Finish closing it (or set aside the unit that has no accounts) before changing how the books are kept.')
    with transaction.atomic():
        closed_months_set = set(MonthClose.objects.filter(property=prop).values_list('month', flat=True))
        stale = LedgerLine.objects.filter(property=prop, locked_at__isnull=True).exclude(month__in=closed_months_set)
        stale = stale.filter(unit__isnull=True) if level == Property.FinancialsLevel.UNIT else stale.filter(unit__isnull=False)
        reviewed = stale.filter(reviewed=True).count()
        discarded = stale.count()
        ReconAcceptance.objects.filter(property=prop).exclude(month__in=closed_months_set).delete()
        stale.delete()
        prop.financials_level = level
        prop.ledger_synced_at = None
        prop.save(update_fields=['financials_level', 'ledger_synced_at'])
        Unit.objects.filter(property=prop).update(ledger_synced_at=None)
    return {'discarded': discarded, 'reviewed': reviewed}


# --- coding ----------------------------------------------------------------------------------------------

def month_lines(book, month, include_removed=False):
    qs = LedgerLine.objects.filter(month=month_of(month), **_book(book).scope())
    if not include_removed:
        qs = qs.filter(status=LedgerLine.Status.ACTIVE)
    return qs.order_by('role', 'txn_date', 'txn_type', 'txn_id')


def _guard_open(book, month):
    if is_closed(book, month):
        raise CloseError(f'{month_of(month):%B %Y} is closed for {_book(book).name}. It can no longer be changed; put a correction in the current month.')


@transaction.atomic
def code_lines(book, month, user, assignments):
    """Apply {line id: category} chosen on the coding screen. A line whose category
    changes becomes "coded by a person"; every line included is marked reviewed.
    Returns how many changed category."""
    _guard_open(book, month)
    lines = {l.pk: l for l in month_lines(book, month)}
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
def describe_lines(book, month, edits):
    """Apply {line id: text} typed into the description column of the coding screen. Most lines keep what
    QuickBooks says; where it needs correcting our wording is kept beside QuickBooks's memo (never overwritten by
    a sync). Text equal to QuickBooks's memo, or nothing at all, puts QuickBooks's back. Returns how many changed."""
    _guard_open(book, month)
    lines = {l.pk: l for l in month_lines(book, month)}
    changed = 0
    for pk, text in edits.items():
        line = lines.get(pk)
        if line is None:
            continue
        text = ' '.join((text or '').split())[:500]
        new = '' if (not text or text == line.memo) else text
        if new != line.description:
            line.description = new
            line.save(update_fields=['description'])
            changed += 1
    return changed


@transaction.atomic
def accept_all(book, month, user):
    """Marks every line of the month reviewed as it stands (defaults included).
    Returns how many were newly reviewed."""
    _guard_open(book, month)
    return month_lines(book, month).filter(reviewed=False).update(reviewed=True, coded_by=user, coded_at=timezone.now())


@transaction.atomic
def acknowledge_changes(book, month):
    """The team has looked at what QuickBooks changed."""
    _guard_open(book, month)
    return month_lines(book, month).filter(changed_in_qb=True).update(changed_in_qb=False)


# --- the month's figures and checks ----------------------------------------------------------------------

TOTAL_KEYS = ('deposits', 'owner_payment', 'commission', 'reimbursement_trust', 'reimbursement_expense', 'expenses_reimbursable', 'expenses_direct', 'trust_net',
              'commission_due', 'owner_due')
CENT = Decimal('0.01')


def totals(book, month, lines=None, with_prior=True, memo=None):
    """The month in dollars, from the lines as currently coded (positive numbers).
    `deposits` is income: only trust-account money in that is coded as a booking
    payout — a refund coded as an expense reduces expenses instead.

    THE MONTH'S CALCULATION: our commission is the property's commission rate of the income
    deposits (the top line: we are paid for our work whether or not the month is profitable);
    the income deposits less that commission, less the reimbursable expenses (paid by us, out of
    the expense account) and the expenses paid from trust, is the owner payment (`owner_due`).
    The reimbursement to us and the commission actually taken out of the trust account this
    month (`reimbursement_trust`, `commission`) do NOT enter this month's calculation: they settle
    LAST month's, so they are compared with last month's figures (`prior_*`, see
    prior_settlement) instead. `owner_payment` is what was coded as paid to the owner."""
    lines = list(month_lines(book, month)) if lines is None else lines
    total = {k: ZERO for k in TOTAL_KEYS}
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
    prop = _book(book).property
    rate = Decimal(prop.commission_rate if prop.commission_rate is not None else Decimal('10.00'))
    due = (max(total['deposits'], ZERO) * rate / 100).quantize(CENT, rounding=ROUND_HALF_UP)
    total.update(commission_rate=rate, commission_due=due, owner_due=total['deposits'] - due - total['expenses_total'], calc=True)
    if with_prior:
        total.update(prior_settlement(book, month, memo))
    derive(total)
    if memo is not None:
        memo[(_book(book)._key(), month_of(month))] = total
    return total


def derive(t):
    """The figures the screens show that are simple sums of others.

    Everything paid out of the trust account this month settles LAST month's dues: the reimbursement and the
    commission to us, and the owner payment. So each is compared with last month's figure (`*_diff`), and what
    was not settled carries on. The PAYABLES at the end of the month are what this month earned and will be
    paid early next month (the owner payment; the reimbursable expenses and commission to us), plus anything of
    last month's that is still unpaid (`*_carry`); the aim is to pay out 100% every month, so the carry is zero."""
    if not t.get('calc'):
        return t
    t['taken_to_us'] = t['reimbursement_trust'] + t['commission']
    t['still_to_us'] = t['expenses_reimbursable'] + t['commission_due']
    ok = t.get('prior_status') == 'ok'
    owner_ok = ok and t.get('prior_owner') is not None
    t['reimbursement_diff'] = t['reimbursement_trust'] - t['prior_reimbursable'] if ok else None
    t['commission_diff'] = t['commission'] - t['prior_commission'] if ok else None
    t['owner_diff'] = t['owner_payment'] - t['prior_owner'] if owner_ok else None
    t['reimb_carry'] = (t['prior_reimbursable'] - t['reimbursement_trust']) if ok else ZERO
    t['comm_carry'] = (t['prior_commission'] - t['commission']) if ok else ZERO
    t['owner_carry'] = (t['prior_owner'] - t['owner_payment']) if owner_ok else ZERO
    t['us_carry'] = t['reimb_carry'] + t['comm_carry']
    t['reimb_payable'] = t['expenses_reimbursable'] + t['reimb_carry']
    t['comm_payable'] = t['commission_due'] + t['comm_carry']
    t['us_payable'] = t['reimb_payable'] + t['comm_payable']
    t['owner_payable'] = t['owner_due'] + t['owner_carry']
    return t


def prior_settlement(book, month, memo=None):
    """What last month left to be paid in this one: the accounts payable at the end of last month, per
    item: the reimbursable expenses (reimbursed to us out of the trust account), our commission and the
    owner payment. That is what last month earned plus whatever was still unpaid from the month before it,
    so an amount not paid stays owed until it is. From its frozen figures if it was closed under this
    calculation, else worked out from its lines (`memo` holds months already worked out).
    `prior_status` says why there is nothing to compare with: 'not_in_books' (before the books start),
    'other_shape' (kept as property books then, unit books now, or the reverse), 'no_data' (nothing
    pulled in for it yet)."""
    book = _book(book)
    month = month_of(month)
    prior = previous_month(month)
    out = {'prior_month': prior, 'prior_reimbursable': None, 'prior_commission': None, 'prior_owner': None, 'prior_status': 'ok', 'prior_closed': False}
    if prior < month_of(books_start()):
        out['prior_status'] = 'not_in_books'
        return out
    if month_level(book.property, prior) != book.level:
        out['prior_status'] = 'other_shape'
        return out
    close = MonthClose.objects.filter(month=prior, **book.scope()).first()
    if close is None and not month_lines(book, prior).exists():
        out['prior_status'] = 'no_data'
        return out
    if close is not None and 'reimb_payable' in close.totals:
        t = closed_summary(close)
    elif memo is not None and (book._key(), prior) in memo:
        t = memo[(book._key(), prior)]
    else:
        t = totals(book, prior, memo=memo)          # closed before the balances were kept, or open: from its lines, and on back
    out.update(prior_reimbursable=t.get('reimb_payable', t['expenses_reimbursable']), prior_commission=t.get('comm_payable', t['commission_due']),
               prior_owner=t.get('owner_payable', t['owner_due']), prior_closed=close is not None)
    return out


def sum_totals(parts):
    """The property's figures: its units' added up."""
    out = {k: ZERO for k in TOTAL_KEYS + ('expenses_total',)}
    for part in parts:
        for k in out:
            out[k] += part.get(k, ZERO)
    out['calc'] = bool(parts) and all(p.get('calc') for p in parts)
    out['commission_rate'] = next((p['commission_rate'] for p in parts if p.get('commission_rate') is not None), None)
    priors = [p.get('prior_reimbursable') for p in parts]
    ok = bool(parts) and all(v is not None for v in priors)
    out['prior_reimbursable'] = sum(priors, ZERO) if ok else None
    out['prior_commission'] = sum((p['prior_commission'] for p in parts), ZERO) if ok else None
    out['prior_owner'] = sum((p['prior_owner'] for p in parts), ZERO) if ok and all(p.get('prior_owner') is not None for p in parts) else None
    out['prior_status'] = 'ok' if ok else next((p.get('prior_status') for p in parts if p.get('prior_status') not in (None, 'ok')), 'no_data')
    out['prior_month'] = next((p.get('prior_month') for p in parts if p.get('prior_month')), None)
    out['prior_closed'] = ok and all(p.get('prior_closed') for p in parts)
    return derive(out)


STATEMENT_KEYS = ('deposits', 'expenses_reimbursable', 'expenses_direct', 'commission_due', 'owner_due', 'owner_payment', 'taken_to_us')


def _zero_totals():
    keys = TOTAL_KEYS + ('expenses_total', 'taken_to_us', 'still_to_us', 'reimb_carry', 'comm_carry', 'owner_carry', 'us_carry', 'reimb_payable', 'comm_payable', 'us_payable', 'owner_payable')
    out = {k: ZERO for k in keys}
    out.update(calc=True, commission_rate=ZERO, prior_status='not_in_books', prior_month=None, prior_reimbursable=None, prior_commission=None, prior_owner=None, prior_closed=False)
    return out


def _detail(book, month):
    """The transactions behind the statement's income, reimbursable expenses and paid-from-trust figures, as they
    add up to them (each amount signed as it counts: a refund is negative)."""
    out = {'deposits': [], 'reimbursable': [], 'direct': []}
    label = book.unit.label if book.unit is not None else ''
    for l in month_lines(book, month).order_by('txn_date', 'pk'):
        if l.role == Role.TRUST and l.category == Category.DEPOSIT:
            group, amount = 'deposits', l.flow
        elif l.role == Role.EXPENSE and l.category != Category.REIMBURSEMENT:
            group, amount = 'reimbursable', l.flow
        elif l.role == Role.TRUST and l.category == Category.EXPENSE:
            group, amount = 'direct', -l.flow
        else:
            continue
        out[group].append({'date': l.txn_date, 'text': l.shown_memo or l.txn_type, 'amount': amount, 'unit': label})
    return out


def statement(prop, end=None, months=12, year=None):
    """A property's months side by side, one column each. With `year` it is that calendar year, January to
    December (the screen's view; the arrows move by year); otherwise `months` of them ending at `end` (default
    the last complete month). Always all of them, so the layout is there even when there is nothing to show: a month with no
    transactions, before the books start, or still to come is a column of zeros. Down the page: the calculation (income
    deposits less commission, reimbursable expenses and expenses paid from trust = the owner payment); the
    accounts payable at month end (to the owner and to us); what was paid out this month for last month; and two
    checks: last month's payables cleared, and all income accounted for. Behind the income, reimbursable and
    paid-from-trust figures are the transactions they add up. A closed month is as closed (its transactions are
    locked); an open one as it is coded now. For a property kept unit by unit each column is its units added up.

    Returns {'columns': [{'month', 'state', 't', 'accounted', 'cleared', 'detail'}], 'total': {...sums...}}."""
    start = month_of(books_start())
    this_month = month_of(timezone.localdate())
    if year is not None:
        span = [date(year, i, 1) for i in range(1, 13)]
    else:
        last = month_of(end) if end else previous_month(timezone.localdate())
        span, m = [], last
        for _ in range(months):
            span.append(m)
            m = previous_month(m)
        span.reverse()
    columns = []
    memo = {}
    for m in span:
        parts, closed, detail = [], 0, {'deposits': [], 'reimbursable': [], 'direct': []}
        books = books_for(prop, m) if start <= m <= this_month else []
        for b in books:
            close = MonthClose.objects.filter(month=m, **b.scope()).first()
            if close is not None and 'reimb_payable' in close.totals:
                parts.append(closed_summary(close))
            elif close is not None or month_lines(b, m).exists():
                parts.append(totals(b, m, memo=memo))       # closed before the balances were kept, or open: from its lines
            else:
                continue
            closed += close is not None
            for group, rows in _detail(b, m).items():
                detail[group] += rows
        if not parts:
            columns.append({'month': m, 'state': 'future' if m > this_month else ('empty' if m >= start else 'before'), 't': _zero_totals(), 'accounted': None, 'cleared': None, 'detail': detail})
            continue
        t = parts[0] if len(parts) == 1 else sum_totals(parts)
        accounted = t['deposits'] - (t['expenses_reimbursable'] + t['expenses_direct'] + t['commission_due'] + t['owner_due'])
        cleared = None if t.get('prior_status') != 'ok' else (t['us_carry'] == 0 and t['owner_carry'] == 0)
        columns.append({'month': m, 'state': 'closed' if closed == len(parts) else 'open', 't': t, 'accounted': accounted, 'cleared': cleared, 'detail': detail})
    total = {k: sum((c['t'][k] for c in columns), ZERO) for k in STATEMENT_KEYS}
    return {'columns': columns, 'total': total, 'property': prop, 'first': span[0], 'last': span[-1]}


def checks(book, month, now=None, rec=None):
    """What stands between this set of books' month and being closed. Each item is
    {'key', 'level': 'ok'|'warn'|'block', 'text'}: a 'block' has to be fixed, a
    'warn' can be acknowledged (it is recorded with the close)."""
    from . import recon
    book = _book(book)
    now = now or timezone.now()
    month = month_of(month)
    items = []

    def add(key, level, text):
        items.append({'key': key, 'level': level, 'text': text})

    if is_closed(book, month):
        add('closed', 'block', f'{month:%B %Y} is already closed.')
        return items
    if not book.mapped:
        add('accounts', 'block', f'{"This unit" if book.unit else "This rental"} is not tied to both QuickBooks accounts yet.')
    if timezone.localdate() < next_month(month):
        add('month_over', 'block', f'{month:%B %Y} is not over yet.')
    synced = book.ledger_synced_at
    if synced is None:
        add('sync', 'block', 'Its transactions have never been pulled in from QuickBooks — sync first.')
    elif now - synced > STALE_AFTER:
        add('sync', 'block', f'The last sync was {timezone.localtime(synced):%b} {timezone.localtime(synced).day}; sync again so you close what QuickBooks shows now.')
    lines = list(month_lines(book, month))
    if not lines:
        add('empty', 'warn', 'No transactions in either account this month.')
    unreviewed = sum(1 for l in lines if not l.reviewed)
    if unreviewed:
        add('unreviewed', 'block', f'{unreviewed} line{"" if unreviewed == 1 else "s"} still to review — accept them as shown or change them.')
    changed = sum(1 for l in lines if l.changed_in_qb)
    if changed:
        add('changed', 'block', f'{changed} line{"" if changed == 1 else "s"} changed in QuickBooks since coded — take a look and confirm.')
    if lines:
        t = totals(book, month, lines, with_prior=False)
        if t['reimbursement_trust'] != t['reimbursement_expense']:
            add('reimbursement_mismatch', 'warn', f'The reimbursement out of the trust account (${t["reimbursement_trust"]:,.2f}) does not equal the reimbursement credited to the expense account (${t["reimbursement_expense"]:,.2f}).')
    add_settlement_checks(book, month, lines, add)
    if book.mapped:
        add_recon_checks(rec if rec is not None else recon.reconcile(book, month), add)
    return items


def add_settlement_checks(book, month, lines, add):
    """What is paid out of the trust account this month settles LAST month's dues: the reimbursement to us and
    the commission (last month's reimbursable expenses and commission) and the owner payment (last month's).
    Compare each with last month's figure."""
    t = totals(book, month, lines)
    prior = t['prior_month']
    name = f'{prior:%B}'
    status = t['prior_status']
    if status == 'not_in_books':
        add('settle_prior', 'ok', f'{name} is before the books start, so this month\'s reimbursement, commission and owner payment can\'t be checked against it.')
        return
    if status == 'other_shape':
        add('settle_prior', 'ok', f'{name} was kept in a different shape (property books against unit books), so this month\'s reimbursement, commission and owner payment can\'t be checked against it.')
        return
    if status == 'no_data':
        taken = t['reimbursement_trust'] + t['commission'] + t['owner_payment']
        if taken:
            add('settle_prior', 'warn', f'Nothing has been pulled in for {name}, so the ${taken:,.2f} paid out this month (reimbursement, commission and owner payment) can\'t be checked against it.')
        return
    for key, label, taken, owed, what in (
        ('reimbursement_vs_prior', 'Reimbursement to us', t['reimbursement_trust'], t['prior_reimbursable'], 'reimbursable expenses'),
        ('commission_vs_prior', 'Commission', t['commission'], t['prior_commission'], 'commission'),
        ('owner_vs_prior', 'Owner payment', t['owner_payment'], t['prior_owner'], 'owner payment'),
    ):
        if owed is None:
            continue
        diff = taken - owed
        if diff != 0:
            add(key, 'warn', f'{label} taken this month (${taken:,.2f}) does not match {name}\'s {what} (${owed:,.2f}): ${abs(diff):,.2f} {"more" if diff > 0 else "less"}.')


def add_recon_checks(rec, add):
    """The income reconciliation's part of the checklist."""
    from . import recon
    if rec is None:
        return
    open_deposits, open_payouts = recon.problems(rec)
    if open_deposits:
        total = sum((i['amount'] for i in open_deposits), ZERO)
        add('recon_deposits', 'block', f'{len(open_deposits)} income deposit{"" if len(open_deposits) == 1 else "s"} (${total:,.2f}) in the trust account {"has" if len(open_deposits) == 1 else "have"} no matching platform payout — match {"it" if len(open_deposits) == 1 else "them"} to a payout, re-code {"it" if len(open_deposits) == 1 else "them"} (a refund is an expense), fix QuickBooks, or explain {"it" if len(open_deposits) == 1 else "them"} as a reconciling item with a note.')
    if open_payouts:
        total = sum((i['amount'] for i in open_payouts), ZERO)
        add('recon_payouts', 'block', f'{len(open_payouts)} platform payout{"" if len(open_payouts) == 1 else "s"} (${total:,.2f}) {"has" if len(open_payouts) == 1 else "have"} not been matched to a deposit — match {"it" if len(open_payouts) == 1 else "them"} to a bank line, or explain {"it" if len(open_payouts) == 1 else "them"} (paid next month, or a bookkeeping error) with a note.')
    if rec['unassigned']:
        add('recon_unassigned', 'block', f'{rec["unassigned"]} platform payout{"" if rec["unassigned"] == 1 else "s"} belong to no unit yet — assign each reservation to its unit so the money can be reconciled.')
    if rec['undated']:
        add('recon_undated', 'warn', f'{rec["undated"]} reservation{"" if rec["undated"] == 1 else "s"} have a payout amount (${rec["undated_total"]:,.2f}) but no payout date, so {"it" if rec["undated"] == 1 else "they"} can\'t be matched to a deposit.')
    accepted = [i for i in rec['items'] if i['accepted']]
    if accepted:
        add('recon_accepted', 'ok', f'{len(accepted)} reconciling item{"" if len(accepted) == 1 else "s"} accepted.')
    if not open_deposits and not open_payouts and not rec['unassigned']:
        add('recon_ok', 'ok', f'Income reconciles: ${rec["deposits_total"]:,.2f} of income deposits against platform payouts{"" if rec["pairs"] or rec["deposits_total"] else " (none this month)"}.')


def can_close(items):
    return not any(i['level'] == 'block' for i in items)


@transaction.atomic
def close_month(book, month, user, acknowledged=(), note=''):
    """Closes and locks a set of books' month. Refused while anything blocks it, or a
    warning hasn't been acknowledged."""
    from . import recon
    book = _book(book)
    month = month_of(month)
    items = checks(book, month)
    blocks = [i for i in items if i['level'] == 'block']
    if blocks:
        raise CloseError(blocks[0]['text'])
    warns = [i['key'] for i in items if i['level'] == 'warn']
    missing = [k for k in warns if k not in set(acknowledged)]
    if missing:
        raise CloseError('Acknowledge each warning before closing: ' + ', '.join(missing))
    lines = list(month_lines(book, month))
    t = totals(book, month, lines)
    snapshot = {k: (str(v) if isinstance(v, Decimal) else (v.isoformat() if isinstance(v, date) else v)) for k, v in t.items()}
    snapshot['lines'] = len(lines)
    close = MonthClose.objects.create(
        property=book.property, unit=book.unit, level=book.level, month=month, closed_by=user, totals=snapshot,
        recon=recon.snapshot(recon.reconcile(book, month)), warnings_acknowledged=warns, note=note.strip()[:500],
    )
    LedgerLine.objects.filter(month=month, **book.scope()).update(locked_at=timezone.now())
    return close


def closed_summary(close):
    """A closed month's frozen figures as Decimals. A month closed before the calculation existed
    has no `calc` (nor net income, commission due or last-month figures) and is shown as it was."""
    out = {}
    for k, v in close.totals.items():
        if k == 'lines':
            out[k] = int(v)
        elif k in ('calc', 'prior_closed'):
            out[k] = bool(v)
        elif k == 'prior_status':
            out[k] = v
        elif k == 'prior_month':
            out[k] = date.fromisoformat(v) if v else None
        elif v is None:
            out[k] = None
        else:
            out[k] = Decimal(v)
    out.setdefault('calc', False)
    return derive(out)


def status_row(book, month, now=None):
    """Everything the close overview shows for one set of books' month."""
    from . import recon
    book = _book(book)
    month = month_of(month)
    close = MonthClose.objects.filter(month=month, **book.scope()).first()
    lines = list(month_lines(book, month))
    live = recon.reconcile(book, month) if (close is None and book.mapped) else None
    items = checks(book, month, now, rec=live) if close is None else []
    drift = ClosedMonthChange.objects.filter(month=month, resolved=False, **book.scope()).count()
    if close:
        state = 'closed'
    elif not book.mapped:
        state = 'needs_accounts'
    elif can_close(items):
        state = 'ready'
    else:
        state = 'open'
    if close:
        rec = recon.from_close(close)
        recon_ok = True
    elif book.mapped:
        rec = live
        recon_ok = recon.is_clean(rec)
    else:
        rec, recon_ok = None, None
    return {
        'property': book.property, 'book': book, 'unit': book.unit, 'label': book.label, 'state': state, 'close': close, 'lines': len(lines),
        'unreviewed': sum(1 for l in lines if not l.reviewed), 'changed': sum(1 for l in lines if l.changed_in_qb), 'checks': items, 'drift': drift,
        'totals': closed_summary(close) if close else (totals(book, month, lines) if lines else None),
        'recon': rec, 'recon_ok': recon_ok,
    }


def overview(month, now=None):
    """The close overview: for each rental, its set of books for the month (one row, or
    one per unit) and — for a unit-level property — the consolidated figures, which are
    just its units added up."""
    month = month_of(month)
    groups = []
    for prop in rentals():
        level = month_level(prop, month)
        rows = [status_row(b, month, now) for b in books_for(prop, month)]
        group = {'property': prop, 'level': level, 'rows': rows, 'consolidated': None}
        if level == Property.FinancialsLevel.UNIT:
            group['consolidated'] = consolidate(rows)
            group['state'] = group['consolidated']['state']
        elif rows:
            group['state'] = rows[0]['state']
        groups.append(group)
    return groups


def consolidate(rows):
    """A unit-level property's month from its units' rows."""
    states = [r['state'] for r in rows]
    if not rows:
        state = 'needs_accounts'
    elif all(s == 'closed' for s in states):
        state = 'closed'
    elif any(s == 'needs_accounts' for s in states):
        state = 'needs_accounts'
    elif all(s in ('ready', 'closed') for s in states):
        state = 'ready'
    else:
        state = 'open'
    parts = [r['totals'] for r in rows if r['totals']]
    return {
        'state': state, 'totals': sum_totals(parts) if parts else None, 'closed_units': states.count('closed'), 'units': len(rows),
        'lines': sum(r['lines'] for r in rows), 'unreviewed': sum(r['unreviewed'] for r in rows), 'changed': sum(r['changed'] for r in rows),
        'drift': sum(r['drift'] for r in rows), 'recon_ok': all(r['recon_ok'] for r in rows if r['recon_ok'] is not None) if rows else None,
        'payouts_matched': sum((r['recon']['payouts_matched'] for r in rows if r['recon']), ZERO),
        'deposits_total': sum((r['recon']['deposits_total'] for r in rows if r['recon']), ZERO),
    }


# --- reopening ---------------------------------------------------------------------------------------------

@transaction.atomic
def reopen_month(prop, month, user, clear_recon=True):
    """Undo a close so the month can be done again: the frozen figures are copied to ReopenedClose (nothing signed off is
    ever lost), the close is removed, the month's transactions are unlocked (QuickBooks changes flow in again) and its
    drift log is cleared. clear_recon also removes the month's accepted reconciling items and matches made by hand, so
    the income reconciliation starts over. A month cannot be reopened while a LATER month is still closed (that
    month's figures were built on this one): reopen the later one first."""
    from .models import ReconAcceptance, ReconMatch, ReopenedClose
    month = month_of(month)
    closes = list(MonthClose.objects.filter(property=prop, month=month))
    if not closes:
        raise CloseError(f'{month:%B %Y} is not closed.')
    later = MonthClose.objects.filter(property=prop, month__gt=month).order_by('month').first()
    if later is not None:
        raise CloseError(f'{later.month:%B %Y} is closed after {month:%B %Y} — reopen that month first.')
    for c in closes:
        ReopenedClose.objects.create(
            property=prop, unit=c.unit, level=c.level, month=month, closed_by=c.closed_by, closed_at=c.closed_at, totals=c.totals,
            recon=c.recon, warnings_acknowledged=c.warnings_acknowledged, note=c.note, reopened_by=user,
        )
    MonthClose.objects.filter(pk__in=[c.pk for c in closes]).delete()
    LedgerLine.objects.filter(property=prop, month=month).update(locked_at=None)
    ClosedMonthChange.objects.filter(property=prop, month=month).delete()
    if clear_recon:
        ReconAcceptance.objects.filter(property=prop, month=month).delete()
        ReconMatch.objects.filter(property=prop, month=month).delete()
    return len(closes)


@transaction.atomic
def reopen_everything(user):
    """Start over: reopen every closed month of every property (newest first, so each is allowed) and clear every
    accepted reconciling item and hand match, so the whole income reconciliation is done again in the new format.
    Returns (months reopened, properties)."""
    from .models import ReconAcceptance, ReconMatch
    months = props = 0
    for prop_id in sorted(set(MonthClose.objects.values_list('property_id', flat=True))):
        prop = Property.objects.get(pk=prop_id)
        for month in sorted(set(MonthClose.objects.filter(property=prop).values_list('month', flat=True)), reverse=True):
            reopen_month(prop, month, user)
            months += 1
        props += 1
    ReconAcceptance.objects.all().delete()
    ReconMatch.objects.all().delete()
    return months, props
