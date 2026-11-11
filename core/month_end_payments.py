"""The month-end payments: paying the owners, and moving the reimbursable expenses and the commissions from the trust account to Proper Realty.

Each is ONE amount in the bank, the rentals ticked added up, so the bank line can be found later by its amount and date and split across the rentals in QuickBooks (see
core/month_end_booking.py). What is due for a rental is what the closed month says is payable at month end (core/ledger.py): the owner payment, the reimbursable expenses and the
commission, each with anything left unpaid from before.

  * Owners are never paid a negative amount: a rental whose owner payable is negative (a bad month) is not offered, and the balance rides to the next month.
  * The reimbursable expenses and commissions CAN be negative (we owe the trust account something we fixed from last month): they are offered, and when ticked they reduce the
    one transfer, which is how the bank sees it.
  * A rental can be left out on purpose (held) or simply not ticked; the step is complete once every rental with something due is paid or held.
  * A rental is paid once per kind and month. Undo a recorded payment, while it is not yet booked in QuickBooks, to change it."""
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import ledger
from .models import MonthEndHold, MonthEndPayment, MonthEndPaymentLine, Property, RentCheck

ZERO = Decimal('0.00')
CENT = Decimal('0.01')
Kind = MonthEndPayment.Kind

FIELD = {Kind.OWNER: 'owner_payable', Kind.REIMBURSEMENT: 'reimb_payable', Kind.COMMISSION: 'comm_payable'}
SECTIONS = {'owners': (Kind.OWNER,), 'proper': (Kind.REIMBURSEMENT, Kind.COMMISSION), 'ltr_owners': (Kind.OWNER,)}
ORDER = {'payable': 0, 'waiting_rent': 0, 'held': 1, 'paid': 2, 'rides': 3, 'not_closed': 4, 'no_figures': 5, 'nothing': 6}     # what needs a decision first, then what is settled, then what is not ready
TITLES = {Kind.OWNER: 'Owner payments', Kind.REIMBURSEMENT: 'Reimbursable expenses', Kind.COMMISSION: 'Commissions'}


class PaymentError(Exception):
    pass


def _d(value):
    return Decimal(str(value)).quantize(CENT)


def _rentals(group='str'):
    """Every rental in the group whose owner is paid out of the trust account (an owner who collects the income and pays us directly has nothing to pay or move). 'str' is the short-term
    rentals, 'ltr' the long-term ones."""
    wanted = Property.Type.LONG_TERM_RENTAL if group == 'ltr' else Property.Type.SHORT_TERM_RENTAL
    return [p for p in ledger.rentals() if p.property_type == wanted and not p.income_collected_by_owner]


def pay_month_for(prop, month):
    """The month whose dues a payment made now settles. A long-term rental whose owner is paid in the month it is collected is paid for the CURRENT month (the one after the month being
    closed); everything else is paid for the month being closed."""
    month = ledger.month_of(month)
    if prop.property_type == Property.Type.LONG_TERM_RENTAL and prop.owner_paid_in == Property.OwnerPaidIn.SAME_MONTH:
        return ledger.next_month(month)
    return month


def _live_figures(prop, month):
    """Like _book_figures for a month that may still be open: what each set of books owes the owner as the month stands now (a closed month: as it was closed)."""
    out = []
    for book in ledger.books_for(prop, month):
        close = ledger.close_of(book, month)
        t = ledger.closed_summary(close) if close is not None else ledger.totals(book, month)
        if not t.get('calc'):
            return [], 'no_figures'
        out.append((book, _d(t['owner_payable'])))
    return out, ('not_closed' if not out else '')


def _book_figures(prop, month, kind):
    """([(book, amount)], reason or ''): what each set of books (the property's, or each of its units') owes for this kind, from its closed month."""
    out = []
    for book in ledger.books_for(prop, month):
        close = ledger.close_of(book, month)
        if close is None:
            return [], 'not_closed'
        totals = ledger.closed_summary(close)
        if not totals.get('calc'):
            return [], 'no_figures'
        out.append((book, _d(totals[FIELD[kind]])))
    return out, ('not_closed' if not out else '')


def rows(month, kind, group='str'):
    """One row per rental for this kind and month: {'property', 'amount', 'books', 'state', 'lines', 'hold', 'multi'}. States: payable, paid, held, not_closed (the rental's month is
    not closed yet), no_figures (closed before payables were worked out), rides (an owner whose amount is negative: it carries to next month), nothing (zero)."""
    month = ledger.month_of(month)
    months = {month, ledger.next_month(month)} if group == 'ltr' else {month}
    paid = {}
    for line in MonthEndPaymentLine.objects.filter(month__in=months, kind=kind).select_related('payment'):
        paid.setdefault((line.property_id, line.month), []).append(line)
    held = {(h.property_id, h.month): h for h in MonthEndHold.objects.filter(month__in=months, kind=kind)}
    out = []
    for prop in _rentals(group):
        pay_month = pay_month_for(prop, month) if group == 'ltr' else month
        same_month = group == 'ltr' and pay_month != month
        figures, why = _live_figures(prop, pay_month) if same_month else _book_figures(prop, pay_month, kind)
        amount = sum((a for _, a in figures), ZERO)
        row = {'property': prop, 'amount': amount, 'books': figures, 'state': '', 'lines': paid.get((prop.pk, pay_month), []), 'hold': held.get((prop.pk, pay_month)), 'multi': len(figures) > 1,
               'pay_month': pay_month, 'timing': ('Same month' if same_month else 'In arrears') if group == 'ltr' else ''}
        check = RentCheck.objects.filter(property=prop, month=pay_month).first() if same_month else None
        if same_month:
            row['rent_check'] = check
        if row['lines']:
            row['state'] = 'paid'
            row['payment'] = row['lines'][0].payment
            row['paid_amount'] = sum((l.amount for l in row['lines']), ZERO)
        elif same_month and (check is None or not check.counts_as_collected):
            row['state'] = 'waiting_rent'          # paid the month it is collected: not before that month's rent is confirmed
        elif why:
            row['state'] = why
        elif kind == Kind.OWNER and amount < 0:
            row['state'] = 'rides'
        elif amount == 0:
            row['state'] = 'nothing'
        elif row['hold']:
            row['state'] = 'held'
        else:
            row['state'] = 'payable'
        out.append(row)
    return sorted(out, key=lambda r: (ORDER.get(r['state'], 9), r['property'].name))


def summary(month, kinds, group='str'):
    """For the checklist and the page header: how many rentals still need something, and the status of the step."""
    needed = done = open_ = 0
    for kind in kinds:
        for r in rows(month, kind, group):
            if r['state'] in ('payable', 'not_closed', 'no_figures', 'waiting_rent'):
                needed += 1
                open_ += 1
            elif r['state'] in ('paid', 'held'):
                needed += 1
                done += 1
    if needed == 0:
        return {'status': 'done', 'detail': 'Nothing is due this month.', 'done': 0, 'needed': 0}
    if open_ == 0:
        return {'status': 'done', 'detail': f'{done} of {needed} paid or held.', 'done': done, 'needed': needed}
    if done == 0:
        return {'status': 'not_started', 'detail': f'{needed} to pay.', 'done': 0, 'needed': needed}
    return {'status': '', 'detail': f'{done} of {needed} paid or held.', 'done': done, 'needed': needed}


def _split_total(books, total):
    """A property kept unit by unit is paid as one amount, but its units are books of their own, so the amount is shared out over them. In proportion to what each is due when every unit
    is owed something; otherwise the whole difference goes to the unit that is owed the most. The shares add up to exactly the amount."""
    dues = [due for _, due in books]
    shares = [due for due in dues]
    if sum(dues, ZERO) == total:
        return shares
    if all(due > 0 for due in dues):
        shares = [(total * due / sum(dues, ZERO)).quantize(CENT) for due in dues]
        biggest = dues.index(max(dues))
    else:
        shares = list(dues)
        biggest = dues.index(max(dues))
    shares[biggest] += total - sum(shares, ZERO)
    return shares


def _amounts_for(row, wanted):
    """[(book, amount to pay, amount due)] for one rental's pick."""
    books = row['books']
    if wanted is None or _d(wanted) == row['amount']:
        return [(book, due, due) for book, due in books]
    wanted = _d(wanted)
    if len(books) == 1:
        return [(books[0][0], wanted, books[0][1])]
    return [(book, share, due) for (book, due), share in zip(books, _split_total(books, wanted))]


@transaction.atomic
def record(month, kind, picks, paid_on, reference, user, group='str'):
    """Saves one lump payment. `picks` is {property id: amount or None}: None pays what is due, an amount pays that instead (the difference is simply still owed next month). The amount is
    the property's: for a property kept unit by unit it is shared out over its units for the books (see _split_total). Returns the MonthEndPayment."""
    month = ledger.month_of(month)
    if kind not in FIELD:
        raise PaymentError('Unknown payment type.')
    if not picks:
        raise PaymentError('Tick at least one rental.')
    by_property = {r['property'].pk: r for r in rows(month, kind, group)}
    lines = []
    for pid, wanted in picks.items():
        row = by_property.get(pid)
        if row is None or row['state'] != 'payable':
            name = row['property'].name if row else f'Property {pid}'
            raise PaymentError(f'{name} is not payable right now.')
        parts = _amounts_for(row, wanted)
        paid = sum((p for _, p, _ in parts), ZERO)
        if kind == Kind.OWNER and paid <= 0:
            raise PaymentError(f'{row["property"].name}: an owner payment has to be more than $0.00.')
        if paid == 0:
            raise PaymentError(f'{row["property"].name}: the amount can not be $0.00 (leave it unticked instead).')
        lines += [(row, book, amount, due) for book, amount, due in parts if amount != 0]
    total = sum((l[2] for l in lines), ZERO)
    if total == 0:
        raise PaymentError('The ticked rentals add up to $0.00.')
    payment = MonthEndPayment.objects.create(kind=kind, month=month, paid_on=paid_on, reference=(reference or '').strip()[:60], total=total, created_by=user if getattr(user, 'pk', None) else None)
    try:
        for row, book, amount, due in lines:
            MonthEndPaymentLine.objects.create(payment=payment, kind=kind, month=row.get('pay_month', month), property=row['property'], unit=book.unit, amount=amount, due=due)
    except IntegrityError:
        raise PaymentError('One of those rentals was already paid for this month - reload the page.')
    MonthEndHold.objects.filter(kind=kind, property_id__in=[l[0]['property'].pk for l in lines], month__in=[month, ledger.next_month(month)]).delete()
    return payment


@transaction.atomic
def undo(payment):
    """Deletes a recorded payment so it can be done again. Not once it is booked in QuickBooks: that was done from its amounts."""
    if payment.booked_at or payment.bookings.exists():
        raise PaymentError('This payment is already booked in QuickBooks, so it can not be undone here.')
    payment.delete()


def hold(month, kind, prop, user, on=True):
    month = pay_month_for(prop, month)
    if on:
        MonthEndHold.objects.get_or_create(month=month, kind=kind, property=prop, defaults={'created_by': user if getattr(user, 'pk', None) else None})
    else:
        MonthEndHold.objects.filter(month=month, kind=kind, property=prop).delete()


def mark_booked(payment, user):
    """The bookkeeping was done by hand in QuickBooks: nothing more for the program to do."""
    payment.booked_at = timezone.now()
    payment.booked_by = user if getattr(user, 'pk', None) else None
    payment.save(update_fields=['booked_at', 'booked_by'])
