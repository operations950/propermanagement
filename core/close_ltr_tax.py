"""Two month-end close steps with a little bookkeeping of their own: checking that the long-term rents came in, and the sales tax returns to file.

Rent collection is for the CURRENT month (the one after the month being closed): a long-term owner who is paid the month it is collected is paid for it only once its rent is confirmed here.
Sales tax is a list kept by hand: the returns we are responsible for, ticked off for the month's period.
"""
from decimal import Decimal

from . import ledger
from .models import Property, RentCheck, SalesTaxFiling, SalesTaxReturn

ZERO = Decimal('0.00')
CENT = Decimal('0.01')


def rent_month(close_month):
    """The month whose rent is being checked: the one after the month being closed."""
    return ledger.next_month(ledger.month_of(close_month))


def ltr_rentals():
    return [p for p in ledger.rentals() if p.property_type == Property.Type.LONG_TERM_RENTAL and not p.income_collected_by_owner]


def rent_rows(close_month):
    """One row per long-term rental: the rent we expect, what the books show came in this month, the difference, and the person's confirmation."""
    month = rent_month(close_month)
    checks = {c.property_id: c for c in RentCheck.objects.filter(month=month)}
    rows = []
    for prop in ltr_rentals():
        seen = ZERO
        for book in ledger.books_for(prop, month):
            seen += Decimal(str(ledger.totals(book, month, with_prior=False)['deposits']))
        expected = prop.monthly_rent
        if expected is None:
            kind = 'no_rent'
        elif seen == 0:
            kind = 'missing'
        elif abs(seen - expected) < CENT:
            kind = 'matches'
        elif seen < expected:
            kind = 'short'
        else:
            kind = 'over'
        check = checks.get(prop.pk)
        rows.append({'property': prop, 'expected': expected, 'seen': seen, 'diff': (seen - expected) if expected is not None else None, 'kind': kind, 'check': check,
                     'timing': 'Same month' if prop.owner_paid_in == Property.OwnerPaidIn.SAME_MONTH else 'In arrears'})
    return rows


def confirm_rent(prop, close_month, status, note, user):
    """Save the person's confirmation of one rental's rent for the month, with what the books showed."""
    month = rent_month(close_month)
    if status not in RentCheck.Status.values:
        raise ValueError('Choose Received, Partly received or Not received.')
    row = next((r for r in rent_rows(close_month) if r['property'].pk == prop.pk), None)
    if row is None:
        raise ValueError('That is not a long-term rental we collect rent for.')
    if status == RentCheck.Status.RECEIVED and row['kind'] not in ('matches', 'no_rent') and not (note or '').strip():
        raise ValueError(f'The books show {row["seen"]:,.2f}, not the rent of {row["expected"]:,.2f}: say why it is right (a note), or choose Partly received.')
    obj, _ = RentCheck.objects.update_or_create(property=prop, month=month, defaults={'expected': prop.monthly_rent, 'seen': row['seen'], 'status': status, 'note': (note or '').strip()[:300],
                                                                                   'confirmed_by': user if getattr(user, 'pk', None) else None})
    return obj


def rent_summary(close_month):
    rows = rent_rows(close_month)
    month = rent_month(close_month)
    if not rows:
        return {'status': 'done', 'detail': 'No long-term rentals.', 'done': 0, 'needed': 0}
    done = sum(1 for r in rows if r['check'])
    if done == len(rows):
        return {'status': 'done', 'detail': f'{month:%B} rent confirmed for all {len(rows)}.', 'done': done, 'needed': len(rows)}
    return {'status': 'not_started' if done == 0 else '', 'detail': f'{done} of {len(rows)} confirmed for {month:%B}.', 'done': done, 'needed': len(rows)}


# ---------------------------------------------------------------- sales tax

def tax_rows(close_month):
    """The returns due for the period that ends in the month being closed, and whether each is filed."""
    month = ledger.month_of(close_month)
    filings = {f.tax_return_id: f for f in SalesTaxFiling.objects.filter(month=month)}
    rows = []
    for r in SalesTaxReturn.objects.filter(is_active=True):
        if r.due_in(month):
            rows.append({'ret': r, 'filing': filings.get(r.pk)})
    return rows


def tax_summary(close_month):
    rows = tax_rows(close_month)
    if not rows:
        return {'status': 'done', 'detail': 'No sales tax returns are due.', 'done': 0, 'needed': 0}
    done = sum(1 for r in rows if r['filing'])
    if done == len(rows):
        return {'status': 'done', 'detail': f'All {len(rows)} filed.', 'done': done, 'needed': len(rows)}
    return {'status': 'not_started' if done == 0 else '', 'detail': f'{done} of {len(rows)} filed.', 'done': done, 'needed': len(rows)}
