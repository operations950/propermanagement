"""Palm Beach County tourist development tax (6%) on short-term rental receipts.

The rule we follow, as decided by the owner:
  * the tax is on the money we actually receive, counted in the month it reaches the owner trust account (the bank deposit's date),
    not the platform's payout date or the check-in date;
  * the base is what the guest paid for the stay: the platform payout PLUS the host service fee the platform kept, so cleaning and other
    mandatory fees are inside it. The platform's guest service fee was never ours, and the state lodging tax a platform collects is not
    rent, so neither is in it;
  * stays of more than six months are not transient rentals and are left out; so are rentals whose owner collects the booking income
    (it never reaches our trust account).

The reconciliation already ties each income deposit in a month to the reservations it is made of (core/recon.py): a payout that landed
in the bank two days into the next month is matched there, not in the month the platform dated it. So a month's taxable receipts are
simply the reservation payouts of that month's matched deposits, plus the host fee that belongs to each. A deposit nothing was matched
to is counted at face value (we cannot know its host fee) and flagged, so the total never quietly comes up short."""
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone

from . import ledger, recon
from .models import LedgerLine, Property

ZERO = Decimal('0.00')
RATE = Decimal('0.06')
TRANSIENT_MAX_DAYS = 180          # a rental of more than six months is not taxed


def _money(value):
    return Decimal(str(value)).quantize(Decimal('0.01'))


def _deposits(book, month):
    return [l for l in ledger.book_month_lines(book, month) if l.role == LedgerLine.Role.TRUST and l.category == LedgerLine.Category.DEPOSIT and l.flow != 0]


def _events_of(match):
    """(code, kind, amount, booking-or-None, guest, source) for each payout in a match, whether it is a live one or a closed month's frozen copy."""
    out = []
    if 'event_rows' in match:         # live reconciliation: _Event objects
        for ev in match['events']:
            b = ev.booking
            out.append((b.external_uid, ev.kind, _money(ev.amount), b, b.guest_name or 'Guest', recon._source_label(b.source)))
    else:                             # a closed month's snapshot: plain dicts
        for e in match.get('events', []):
            out.append((e.get('code', ''), e.get('kind', 'reservation'), _money(e.get('amount', 0)), None, e.get('guest') or 'Guest', e.get('source', '')))
    return out


def _lines_of(match):
    if 'line_rows' in match:
        return [(r['date'], _money(r['amount'])) for r in match['line_rows']]
    return [(date.fromisoformat(r['date']), _money(r['amount'])) for r in match.get('lines', [])]


def host_fee_share(booking, amount):
    """What belongs to `amount` of this reservation's payout, besides the payout itself: (host service fee, platform-collected tax, known).
    The fee is gross - payout when the report gave the gross, else the platform's own deduction; the lodging tax some platforms leave inside
    the payout (VRBO) comes back out. Both are shared out by how much of the payout this amount is, so an installment of a long stay carries its share."""
    if booking is None or not booking.payout_amount or booking.payout_amount <= 0:
        return ZERO, ZERO, False
    share = Decimal(amount) / booking.payout_amount
    if booking.gross_amount is not None:
        fee = booking.gross_amount - booking.payout_amount
    elif booking.platform_fee is not None:
        fee = booking.platform_fee
    else:
        return ZERO, ZERO, False
    tax = booking.tax_amount or ZERO
    return _money(max(fee, ZERO) * share), _money(tax * share), True


def _stay_days(booking):
    if booking is None:
        return 0
    return (timezone.localtime(booking.check_out).date() - timezone.localtime(booking.check_in).date()).days


def _book_receipts(book, month):
    """One set of books' month: the lines (one per reservation payout deposited in the month, plus any unmatched deposit) and the flags."""
    from onsite.models import Booking
    close = ledger.close_of(book, month)
    rec = recon.from_close(close) if close else (recon.reconcile(book, month) if book.mapped else None)
    deposits = _deposits(book, month)
    deposits_total = sum((l.flow for l in deposits), ZERO)
    matches = (rec or {}).get('matches', []) if rec else []
    parts = [(match, _events_of(match), _lines_of(match)) for match in matches]
    codes = {code for _m, evs, _l in parts for code, _k, _a, b, _g, _s in evs if b is None and code}
    found = {}
    if codes:
        found = {b.external_uid: b for b in Booking.objects.filter(property=book.property, external_uid__in=codes)}
    lines, matched_deposits, missing_fee = [], ZERO, 0
    for match, evs, mlines in parts:
        matched_deposits += sum((a for _d, a in mlines), ZERO)
        landed = min((d for d, _a in mlines), default=month)
        for code, kind, amount, booking, guest, source in evs:
            if kind != 'reservation':
                continue                      # pass-through tax, resolutions and adjustments are not rent
            booking = booking or found.get(code)
            if _stay_days(booking) > TRANSIENT_MAX_DAYS:
                continue
            fee, tax, known = host_fee_share(booking, amount)
            if booking is not None and booking.payout_amount and not known:
                missing_fee += 1
            lines.append({'date': landed, 'guest': guest, 'code': code, 'source': source or (booking.get_source_display() if booking else ''),
                          'deposited': amount - tax, 'fee': fee, 'taxable': amount - tax + fee, 'flag': ''})
    unmatched = (deposits_total - matched_deposits).quantize(Decimal('0.01'))
    if unmatched != 0:
        first = min((l.txn_date for l in deposits), default=month)
        lines.append({'date': first, 'guest': 'Deposit not matched to a reservation', 'code': '', 'source': '', 'deposited': unmatched, 'fee': ZERO, 'taxable': unmatched, 'flag': 'unmatched'})
    return lines, {'unmatched': unmatched, 'missing_fee': missing_fee}


def report(month):
    """The month's tourist tax: a row per property (its units added up) with the reservation payouts behind it, the total and the tax due."""
    month = ledger.month_of(month)
    props = [p for p in ledger.rentals() if p.property_type == Property.Type.SHORT_TERM_RENTAL and not p.income_collected_by_owner]
    ledger.preload(props)
    rows, unmapped = [], []
    for prop in props:
        lines, flags = [], {'unmatched': ZERO, 'missing_fee': 0}
        for book in ledger.books_for(prop, month):
            if not book.mapped:
                unmapped.append(book.name)
                continue
            got, f = _book_receipts(book, month)
            lines += got
            flags['unmatched'] += f['unmatched']
            flags['missing_fee'] += f['missing_fee']
        if not lines:
            continue
        lines.sort(key=lambda l: (l['date'], l['guest']))
        taxable = sum((l['taxable'] for l in lines), ZERO)
        rows.append({'property': prop, 'lines': lines, 'deposited': sum((l['deposited'] for l in lines), ZERO), 'fees': sum((l['fee'] for l in lines), ZERO),
                     'taxable': taxable, 'tax': (taxable * RATE).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
                     'unmatched': flags['unmatched'], 'missing_fee': flags['missing_fee']})
    rows.sort(key=lambda r: r['property'].name)
    taxable_total = sum((r['taxable'] for r in rows), ZERO)
    due_on = ledger.next_month(month)
    today = timezone.localdate()
    flags = []
    unmatched_n = sum(1 for r in rows for l in r['lines'] if l['flag'] == 'unmatched')
    if unmatched_n:
        flags.append(f'{unmatched_n} deposit{"" if unmatched_n == 1 else "s"} not matched to a reservation (counted without a host fee)')
    missing_n = sum(r['missing_fee'] for r in rows)
    if missing_n:
        flags.append(f'{missing_n} reservation{"" if missing_n == 1 else "s"} with no host fee on file (counted without it)')
    if unmapped:
        flags.append(f'{len(unmapped)} rental{"" if len(unmapped) == 1 else "s"} without QuickBooks accounts: {", ".join(sorted(set(unmapped))[:4])}{"…" if len(set(unmapped)) > 4 else ""}')
    return {
        'month': month, 'rows': rows, 'deposited': sum((r['deposited'] for r in rows), ZERO), 'fees': sum((r['fees'] for r in rows), ZERO),
        'taxable': taxable_total, 'tax': sum((r['tax'] for r in rows), ZERO), 'flags': flags, 'due_on': due_on, 'late_after': due_on + timedelta(days=19),
        'in_progress': today < due_on, 'rate': RATE,
    }


def csv_text(rep):
    import csv
    import io
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(['Property', 'Date in trust account', 'Guest', 'Confirmation code', 'Platform', 'Deposited', 'Host fee', 'Taxable receipts', 'Note'])
    for r in rep['rows']:
        for l in r['lines']:
            w.writerow([r['property'].name, l['date'].isoformat(), l['guest'], l['code'], l['source'], f'{l["deposited"]:.2f}', f'{l["fee"]:.2f}', f'{l["taxable"]:.2f}',
                        'not matched to a reservation' if l['flag'] else ''])
        w.writerow([r['property'].name, 'TOTAL', '', '', '', f'{r["deposited"]:.2f}', f'{r["fees"]:.2f}', f'{r["taxable"]:.2f}', f'tax {r["tax"]:.2f}'])
    w.writerow(['ALL PROPERTIES', 'TOTAL', '', '', '', f'{rep["deposited"]:.2f}', f'{rep["fees"]:.2f}', f'{rep["taxable"]:.2f}', f'tax {rep["tax"]:.2f}'])
    return out.getvalue()
