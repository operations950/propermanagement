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
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from django.utils import timezone

from . import ledger, recon
from .models import LedgerLine, Property, TouristTaxFiling, TouristTaxPayment

ZERO = Decimal('0.00')
RATE = Decimal('0.06')
TRANSIENT_MAX_DAYS = 180          # a rental of more than six months is not taxed
# The dealer's collection allowance (Florida s. 212.12(1), which the county ordinance applies to TDT dealers): 2.5% of the tax, with none on tax
# above $1,200 a return, so at most $30; for a return filed and paid electronically on time. The county's own return form rounds the way the
# figures below do: the tax up to the next cent, the allowance down (its sample return: $1,192.70 -> tax $71.57, allowance $1.78, due $69.79).
ALLOWANCE_RATE = Decimal('0.025')
ALLOWANCE_TAX_CAP = Decimal('1200.00')


def tax_for(taxable):
    return (max(taxable, ZERO) * RATE).quantize(Decimal('0.01'), rounding=ROUND_CEILING)


def allowance_for(tax):
    return (min(tax, ALLOWANCE_TAX_CAP) * ALLOWANCE_RATE).quantize(Decimal('0.01'), rounding=ROUND_FLOOR)


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


def days_rented(props, month, today=None):
    """{property id: days rented in the month}: the nights its units were booked, from the reservations' own dates, never from the payouts. A
    platform lists a long booking on every payout line, but the money comes in pieces, so the money says nothing about the days. Counted like
    the occupancy figures: a night two reservations share at one unit counts once; a stay only a payment report knows about counts only for
    nights already past (the calendar says what is booked ahead); a stay of more than six months is not a transient rental and is left out.
    A building's days are its units' days added up (unit-days)."""
    from onsite.services import performance
    today = today or timezone.localdate()
    start = ledger.month_of(month)
    end = ledger.next_month(start)
    units, _unattributed = performance._collect_units(props, today)
    out = {}
    for u in units.values():
        nights = set()
        for b in u.active:
            ns = performance._nights(b)
            if len(ns) > TRANSIENT_MAX_DAYS:
                continue
            if b.pk in u.non_operational:
                ns = [n for n in ns if n < today]
            nights.update(n for n in ns if start <= n < end)
        out[u.prop.pk] = out.get(u.prop.pk, 0) + len(nights)
    return out


def attach_filings(rep):
    """Put each property's filing on its row: state is 'filed' (marked filed for the amount due now), 'changed' (filed for a different amount) or 'open'."""
    filings = {f.property_id: f for f in TouristTaxFiling.objects.filter(month=rep['month'])}
    for r in rep['rows']:
        f = filings.get(r['property'].pk)
        r['filing'] = f
        r['state'] = 'open' if f is None else ('filed' if f.amount == r['due'] else 'changed')
    rep['filed'] = sum(1 for r in rep['rows'] if r['state'] == 'filed')
    rep['unfiled'] = len(rep['rows']) - rep['filed']
    rep['changed'] = sum(1 for r in rep['rows'] if r['state'] == 'changed')
    return rep


def attach_payment(rep):
    """Put the county payment on the report and say where the month stands (the last step of the process):
    'none'       nothing is marked filed yet (or there is nothing to file);
    'partial'    some returns are filed, not all;
    'changed'    a filed return's amount changed since it was filed (file it again first);
    'unpaid'     every return is filed and tax is due, but the payment is not recorded: the one that is easy to forget;
    'nothing'    every return is filed and nothing is due, so there is nothing to pay;
    'paid'       the payment is recorded for the amount due now;
    'paid_changed'  the payment is recorded, but the amount due is no longer what it was when it was recorded."""
    pay = TouristTaxPayment.objects.filter(month=rep['month'], cleared_at__isnull=True).first()
    total = len(rep['rows'])
    if total == 0 or (rep['filed'] == 0 and not rep['changed']):
        state = 'none'
    elif rep['changed']:
        state = 'changed'
    elif rep['filed'] < total:
        state = 'partial'
    elif pay is not None:
        state = 'paid' if pay.amount_due == rep['due'] else 'paid_changed'
    elif rep['due'] <= ZERO:
        state = 'nothing'
    else:
        state = 'unpaid'
    rep['payment'] = pay
    rep['pay_state'] = state
    rep['can_pay'] = state in ('unpaid', 'paid_changed')
    return rep


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
                     'taxable': taxable, 'unmatched': flags['unmatched'], 'missing_fee': flags['missing_fee']})
    days = days_rented(props, month)
    shown = {r['property'].pk for r in rows}
    for prop in props:
        if prop.pk not in shown and days.get(prop.pk):
            rows.append({'property': prop, 'lines': [], 'deposited': ZERO, 'fees': ZERO, 'taxable': ZERO, 'unmatched': ZERO, 'missing_fee': 0})
    for r in rows:
        r['days'] = days.get(r['property'].pk, 0)
        r['tax'] = tax_for(r['taxable'])                  # each property is its own return
        r['allowance'] = allowance_for(r['tax'])
        r['due'] = r['tax'] - r['allowance']
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
    return attach_payment(attach_filings({
        'month': month, 'rows': rows, 'deposited': sum((r['deposited'] for r in rows), ZERO), 'fees': sum((r['fees'] for r in rows), ZERO),
        'taxable': taxable_total, 'tax': sum((r['tax'] for r in rows), ZERO), 'allowance': sum((r['allowance'] for r in rows), ZERO),
        'due': sum((r['due'] for r in rows), ZERO), 'days': sum(r['days'] for r in rows), 'flags': flags, 'due_on': due_on, 'late_after': due_on + timedelta(days=19),
        'in_progress': today < due_on, 'late': today > due_on + timedelta(days=19), 'rate': RATE,
    }))


def split_rows(rep):
    """The county takes one payment for every property's return; the books need it split by property. One row per property with a return this month:
    its tourist tax account number, the tax, the collection allowance and the amount due (its share of the payment). The shares add up to the payment."""
    out = []
    for r in rep['rows']:
        f = r.get('filing')
        out.append({'property': r['property'], 'tdt': r['property'].tdt_account_number, 'taxable': r['taxable'], 'tax': r['tax'], 'allowance': r['allowance'], 'due': r['due'],
                    'state': r.get('state', 'open'), 'confirmation': f.confirmation if f else ''})
    return out


SPLIT_HEADER = ['Property', 'Tourist tax account', 'Taxable receipts', 'Tax (6%)', 'Collection allowance', 'Amount due (its share of the county payment)', 'Marked filed', 'Confirmation']


def _split_table(rep):
    rows = [[s['property'].name, s['tdt'], f'{s["taxable"]:.2f}', f'{s["tax"]:.2f}', f'{s["allowance"]:.2f}', f'{s["due"]:.2f}', 'yes' if s['state'] == 'filed' else 'no', s['confirmation']]
            for s in split_rows(rep)]
    rows.append(['TOTAL: the county payment', '', f'{rep["taxable"]:.2f}', f'{rep["tax"]:.2f}', f'{rep["allowance"]:.2f}', f'{rep["due"]:.2f}', '', ''])
    return rows


def split_csv_text(rep):
    import csv
    import io
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([f'Tourist tax for {rep["month"]:%B %Y}: split of the county payment by property'])
    w.writerow(SPLIT_HEADER)
    for row in _split_table(rep):
        w.writerow(row)
    return out.getvalue()


def split_xlsx_bytes(rep):
    import io

    import openpyxl
    from openpyxl.styles import Font
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f'{rep["month"]:%b %Y}'[:31]
    ws.append([f'Tourist tax for {rep["month"]:%B %Y}: split of the county payment by property'])
    ws['A1'].font = Font(bold=True)
    ws.append(SPLIT_HEADER)
    for c in ws[2]:
        c.font = Font(bold=True)
    table = _split_table(rep)
    for i, row in enumerate(table):
        ws.append([row[0], row[1], *[Decimal(x) for x in row[2:6]], row[6], row[7]])
        if i == len(table) - 1:
            for c in ws[ws.max_row]:
                c.font = Font(bold=True)
    for row in ws.iter_rows(min_row=3, min_col=3, max_col=6):
        for c in row:
            c.number_format = '#,##0.00'
    for col, width in zip('ABCDEFGH', (34, 20, 17, 12, 20, 40, 12, 18)):
        ws.column_dimensions[col].width = width
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def csv_text(rep):
    import csv
    import io
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(['Property', 'Date in trust account', 'Guest', 'Confirmation code', 'Platform', 'Deposited', 'Host fee', 'Taxable receipts', 'Days rented', 'Note'])
    for r in rep['rows']:
        for l in r['lines']:
            w.writerow([r['property'].name, l['date'].isoformat(), l['guest'], l['code'], l['source'], f'{l["deposited"]:.2f}', f'{l["fee"]:.2f}', f'{l["taxable"]:.2f}', '',
                        'not matched to a reservation' if l['flag'] else ''])
        w.writerow([r['property'].name, 'TOTAL', '', '', '', f'{r["deposited"]:.2f}', f'{r["fees"]:.2f}', f'{r["taxable"]:.2f}', r['days'], f'tax {r["tax"]:.2f}, collection allowance {r["allowance"]:.2f}, due {r["due"]:.2f}'])
    w.writerow(['ALL PROPERTIES', 'TOTAL', '', '', '', f'{rep["deposited"]:.2f}', f'{rep["fees"]:.2f}', f'{rep["taxable"]:.2f}', rep['days'], f'tax {rep["tax"]:.2f}, collection allowance {rep["allowance"]:.2f}, due {rep["due"]:.2f}'])
    return out.getvalue()
