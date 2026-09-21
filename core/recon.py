"""The income reconciliation that sits in front of every month-end close.

What the owner is paid from is the money that actually reached the trust account, so
before a month closes the deposits coded as income have to be proved against what the
booking platforms say they paid out: every payout the platforms show has to have
cleared into the bank, and every income deposit in the bank has to be a platform
payout — bar a small number of reconciling items a person accepts, with a reason.

How it works
  * a platform payout is what the imports know: a reservation's payout amount and
    payout date (Airbnb, VRBO). Reservations paid out together (same platform, same
    payout date) are one payout; that is what shows up as one deposit.
  * an INCOME DEPOSIT is a trust-account line coded "Income deposit (booking payout)".
    A refund or anything else that isn't platform income is coded as an expense (a
    credit to expenses) instead, and is not part of this check.
  * a deposit whose memo names a reservation's confirmation code (Airbnb's deposits list
    them: "AIRBNB PAYMENTS- ...| $1138.01| HMWZXBF5JF") is matched to that reservation by the
    code, whatever the amounts. What the platform actually deposits for a reservation is more
    than its payout: Airbnb also pays the host the pass-through occupancy tax (and sometimes
    a resolution payout or an adjustment) in the same deposit. Those are kept apart from the
    payout (they are not revenue) and added back here (Booking.cash_amount). If the deposit
    still differs from what is on file, it is shown as a difference to look at — with the
    likely reason — instead of "no matching payout".
  * every other deposit is matched, to the cent, to a payout (or a couple of payouts that
    landed together) dated shortly before it. Deposits arrive a day or a few days after
    the payout date, so a payout dated at the very end of a month that arrives in the
    next is normal: it is shown as "in transit" — not a problem — and matches the
    deposit next month, because a payout stays in line until it has cleared.
  * whatever is left over blocks the close until it is fixed (re-code the deposit, fix
    the amount in QuickBooks, upload the missing report) or accepted as a reconciling
    item with a note. An accepted item is remembered for that month at that amount.
  * a month that closes freezes its reconciliation, including which reservations it
    cleared, so a later month never re-counts them.

Alongside that, the reservations view: which reservations CHECK IN during the month and
how much of their payouts arrived within it — a five-night stay that starts on the
31st belongs to that month even though its money lands in the next."""
import re
from datetime import timedelta
from decimal import Decimal
from itertools import combinations

from django.utils import timezone

from . import ledger
from .models import LedgerLine, MonthClose, ReconAcceptance

ZERO = Decimal('0.00')
EARLY = timedelta(days=3)      # a deposit may be booked a little before the payout's own date
LATE = timedelta(days=10)      # ... and usually lands within a few days after it
TRANSIT = timedelta(days=5)    # a payout dated this close to month end is expected to land next month
LOOKBACK = timedelta(days=10)  # payouts this far before the books start can still land inside the first month
MAX_MONTHS = 36


def _bookings(book):
    """The platform payouts this set of books answers for."""
    from onsite.models import Booking
    qs = Booking.objects.filter(property=book.property, source__in=(Booking.Source.AIRBNB, Booking.Source.VRBO)).exclude(payout_amount__isnull=True).exclude(payout_amount=0)
    if book.unit is not None:
        qs = qs.filter(unit=book.unit)
    return qs


def _source_label(source):
    return {'airbnb': 'Airbnb', 'vrbo': 'VRBO'}.get(source, source)


def _groups(bookings):
    """Reservations paid out together (same platform, same payout date) are one payout."""
    groups = {}
    for b in bookings:
        key = f'{b.source}:{b.payout_date.isoformat()}'
        g = groups.setdefault(key, {'key': key, 'source': b.source, 'date': b.payout_date, 'total': ZERO, 'ids': [], 'guests': []})
        g['total'] += b.cash_amount()
        g['ids'].append(b.pk)
        if b.guest_name and len(g['guests']) < 3:
            g['guests'].append(b.guest_name)
    for g in groups.values():
        g['count'] = len(g['ids'])
        g['label'] = f'{_source_label(g["source"])} payout dated {g["date"]:%b} {g["date"].day}'
        g['total'] = g['total'].quantize(Decimal('0.01'))
    return sorted(groups.values(), key=lambda g: (g['date'], g['source']))


def _income_deposits(book, month):
    return [l for l in ledger.month_lines(book, month) if l.role == LedgerLine.Role.TRUST and l.category == LedgerLine.Category.DEPOSIT and l.flow > 0]


def _match_month(book, month, cleared, scope):
    """One month's matching. `scope` is the book's platform-payout reservations."""
    month_end = ledger.next_month(month) - timedelta(days=1)
    lower = ledger.month_of(ledger.books_start()) - LOOKBACK
    deposits = sorted(_income_deposits(book, month), key=lambda l: (l.txn_date, l.pk))
    # First, deposits that name their reservation(s) by confirmation code.
    by_code = [b for b in scope if b.external_uid and b.pk not in cleared]
    code_pairs, coded_ids, plain = [], set(), []
    for dep in deposits:
        text = f'{dep.payee} {dep.memo}'
        hits = [b for b in by_code if b.pk not in coded_ids and re.search(r'(?<![A-Za-z0-9])' + re.escape(b.external_uid) + r'(?![A-Za-z0-9])', text, re.IGNORECASE)]
        if not hits:
            plain.append(dep)
            continue
        expected = sum((b.cash_amount() for b in hits), ZERO).quantize(Decimal('0.01'))
        codes = ', '.join(b.external_uid for b in hits)
        group = {
            'key': 'codes:' + ','.join(sorted(b.external_uid for b in hits)), 'source': hits[0].source, 'date': hits[0].payout_date or dep.txn_date,
            'total': expected, 'ids': [b.pk for b in hits], 'guests': [b.guest_name for b in hits if b.guest_name][:3], 'count': len(hits),
            'label': f'{_source_label(hits[0].source)} payout for {codes}',
            'on_file': [(b.external_uid, b.payout_amount, b.pass_through_amount, b.other_payout_amount) for b in hits],
        }
        code_pairs.append({'line': dep, 'groups': [group], 'amount': dep.flow, 'expected': expected, 'difference': (dep.flow - expected).quantize(Decimal('0.01')), 'by_code': True})
        coded_ids |= set(group['ids'])
    deposits = plain
    pool = [b for b in scope if b.payout_date and lower <= b.payout_date <= month_end + EARLY and b.pk not in cleared and b.pk not in coded_ids]
    free = _groups(pool)
    pairs, open_deposits = list(code_pairs), []
    for dep in deposits:
        cands = [g for g in free if g['date'] - EARLY <= dep.txn_date <= g['date'] + LATE]
        cands.sort(key=lambda g: abs((dep.txn_date - g['date']).days))
        chosen = None
        for size in (1, 2, 3):
            for combo in combinations(cands[:14], size):
                if sum((g['total'] for g in combo), ZERO) == dep.flow:
                    chosen = combo
                    break
            if chosen:
                break
        if chosen:
            pairs.append({'line': dep, 'groups': list(chosen), 'amount': dep.flow})
            free = [g for g in free if g not in chosen]
        else:
            open_deposits.append(dep)
    matched_ids = {i for p in pairs for g in p['groups'] for i in g['ids']}
    open_groups = [g for g in free if g['date'] <= month_end]
    return {'pairs': pairs, 'open_deposits': open_deposits, 'open_groups': open_groups, 'cleared_out': cleared | matched_ids, 'month_end': month_end,
            'mismatched': [p for p in code_pairs if p['difference'] != 0]}


def _cleared_by_closes(prop, month):
    closes = list(MonthClose.objects.filter(property=prop, month=month))
    if not closes:
        return None
    ids = set()
    for close in closes:
        ids |= set((close.recon or {}).get('cleared_booking_ids', []))
    return ids


def _month_sequence(month):
    start = ledger.month_of(ledger.books_start())
    months, m = [], ledger.month_of(month)
    while m >= start and len(months) < MAX_MONTHS:
        months.append(m)
        m = ledger.previous_month(m)
    if not months:
        months = [ledger.month_of(month)]
    return list(reversed(months))


def _items(book, month, result):
    accepted = {(a.kind, a.key): a for a in ReconAcceptance.objects.filter(month=month, **book.scope())}
    items = []
    for pair in result.get('mismatched', []):
        dep, diff, group = pair['line'], pair['difference'], pair['groups'][0]
        who = dep.payee or dep.memo or dep.txn_type
        hint = ''
        if diff > 0 and not any((pt or 0) for _c, _p, pt, _o in group['on_file']):
            hint = " Airbnb's transactions file lists a separate 'Pass Through Tot' line (tax it pays the host) for each stay: upload the latest transactions file and it is included."
        a = accepted.get(('deposit', f'line:{dep.pk}'))
        items.append({
            'kind': 'deposit', 'key': f'line:{dep.pk}', 'amount': dep.flow, 'date': dep.txn_date, 'in_transit': False, 'mismatch': True,
            'text': f'{dep.txn_date:%b} {dep.txn_date.day}: ${dep.flow:,.2f} deposited ({who}) names {group["label"].split(" for ")[-1]}, but is ${abs(diff):,.2f} {"more" if diff > 0 else "less"} '
                    f'than the ${pair["expected"]:,.2f} on file for {"it" if group["count"] == 1 else "them"}.{hint}',
            'accepted': a if a and a.amount == dep.flow else None,
        })
    for dep in result['open_deposits']:
        who = dep.payee or dep.memo or dep.txn_type
        a = accepted.get(('deposit', f'line:{dep.pk}'))
        items.append({
            'kind': 'deposit', 'key': f'line:{dep.pk}', 'amount': dep.flow, 'date': dep.txn_date, 'in_transit': False,
            'text': f'{dep.txn_date:%b} {dep.txn_date.day}: ${dep.flow:,.2f} deposited ({who}) with no matching platform payout',
            'accepted': a if a and a.amount == dep.flow else None,
        })
    for g in result['open_groups']:
        a = accepted.get(('payout', g['key']))
        transit = g['date'] > result['month_end'] - TRANSIT
        items.append({
            'kind': 'payout', 'key': g['key'], 'amount': g['total'], 'date': g['date'], 'in_transit': transit,
            'text': f'{g["label"]}: ${g["total"]:,.2f} for {g["count"]} reservation{"" if g["count"] == 1 else "s"}'
                    f'{" (" + ", ".join(g["guests"]) + ")" if g["guests"] else ""} has not reached the trust account',
            'accepted': a if a and a.amount == g['total'] else None,
        })
    return items


def _reservations_view(book, month, scope):
    """The reservations that check in during the month and when their money arrives."""
    from onsite.models import Booking
    month_end = ledger.next_month(month) - timedelta(days=1)
    stays = Booking.objects.filter(property=book.property, source__in=(Booking.Source.AIRBNB, Booking.Source.VRBO))
    if book.unit is not None:
        stays = stays.filter(unit=book.unit)
    rows = [b for b in stays if month <= timezone.localtime(b.check_in).date() <= month_end and (b.status == Booking.Status.ACTIVE or b.payout_amount)]
    paid_now = paid_later = ZERO
    later = []
    for b in rows:
        amount = b.payout_amount or ZERO
        if b.payout_date and b.payout_date <= month_end:
            paid_now += amount
        else:
            paid_later += amount
            if amount:
                later.append({'guest': b.guest_name, 'check_in': timezone.localtime(b.check_in).date(), 'amount': amount, 'payout_date': b.payout_date})
    carried = sum((b.payout_amount for b in scope if b.payout_date and month <= b.payout_date <= month_end and timezone.localtime(b.check_in).date() < month), ZERO)
    return {
        'count': len(rows), 'payout_total': paid_now + paid_later, 'paid_in_month': paid_now, 'paid_later': paid_later,
        'paid_later_items': later, 'carried_in': carried,
    }


def reconcile(book, month):
    """The live reconciliation of an open month: matches, what's left over, what has been
    accepted, and the reservations view. None if the month is already closed."""
    month = ledger.month_of(month)
    if ledger.is_closed(book, month):
        return None
    scope = list(_bookings(book))
    cleared = set()
    result = None
    for m in _month_sequence(month):
        if m == month:
            result = _match_month(book, m, cleared, scope)
            break
        frozen = _cleared_by_closes(book.property, m)
        if frozen is not None:
            cleared |= frozen
        else:
            cleared = _match_month(book, m, cleared, scope)['cleared_out']
    if result is None:
        result = _match_month(book, month, cleared, scope)
    items = _items(book, month, result)
    deposits = _income_deposits(book, month)
    month_end = result['month_end']
    matched_payouts = sum((p.get('expected', p['amount']) for p in result['pairs']), ZERO)
    undated = [b for b in _undated(book) if b.pk not in result['cleared_out'] and month_end >= timezone.localtime(b.check_in).date() >= ledger.month_of(ledger.books_start()) - LOOKBACK]
    unassigned = 0
    if book.unit is not None:
        from onsite.models import Booking
        lower = ledger.month_of(ledger.books_start()) - LOOKBACK
        unassigned = Booking.objects.filter(
            property=book.property, unit__isnull=True, source__in=(Booking.Source.AIRBNB, Booking.Source.VRBO),
            payout_date__gte=lower, payout_date__lte=month_end, payout_amount__isnull=False,
        ).exclude(payout_amount=0).exclude(pk__in=cleared).count()
    return {
        'closed': False, 'month': month, 'pairs': result['pairs'], 'items': items, 'cleared_out': result['cleared_out'],
        'payouts_matched': matched_payouts, 'deposits_total': sum((d.flow for d in deposits), ZERO),
        'undated': len(undated), 'undated_total': sum((b.payout_amount for b in undated), ZERO), 'unassigned': unassigned,
        'reservations': _reservations_view(book, month, scope),
    }


def _undated(book):
    from onsite.models import Booking
    qs = Booking.objects.filter(property=book.property, source__in=(Booking.Source.AIRBNB, Booking.Source.VRBO), payout_date__isnull=True, payout_amount__isnull=False).exclude(payout_amount=0)
    if book.unit is not None:
        qs = qs.filter(unit=book.unit)
    return list(qs.exclude(status='cancelled'))


def problems(rec):
    """(open deposits, open payouts that are not just in transit) still needing a fix or an acceptance."""
    deposits = [i for i in rec['items'] if i['kind'] == 'deposit' and not i['accepted']]
    payouts = [i for i in rec['items'] if i['kind'] == 'payout' and not i['accepted'] and not i['in_transit']]
    return deposits, payouts


def is_clean(rec):
    d, p = problems(rec)
    return not d and not p and not rec['unassigned']


# --- accepting reconciling items ----------------------------------------------------------------

def accept_item(book, month, user, kind, key, note):
    """A person accepts one open item as a reconciling item, with the reason."""
    month = ledger.month_of(month)
    if ledger.is_closed(book, month):
        raise ledger.CloseError(f'{month:%B %Y} is closed; it can no longer be changed.')
    note = (note or '').strip()
    if not note:
        raise ledger.CloseError('Say why this is a reconciling item — a short note is required.')
    rec = reconcile(book, month)
    item = next((i for i in rec['items'] if i['kind'] == kind and i['key'] == key), None)
    if item is None:
        raise ledger.CloseError('That item is no longer open — the page has been refreshed.')
    ReconAcceptance.objects.update_or_create(
        month=month, kind=kind, key=key, **book.scope(),
        defaults={'amount': item['amount'], 'description': item['text'][:300], 'note': note[:300], 'accepted_by': user},
    )
    return item


def unaccept_item(book, month, kind, key):
    month = ledger.month_of(month)
    if ledger.is_closed(book, month):
        raise ledger.CloseError(f'{month:%B %Y} is closed; it can no longer be changed.')
    return ReconAcceptance.objects.filter(month=month, kind=kind, key=key, **book.scope()).delete()[0]


# --- what a close freezes ----------------------------------------------------------------------

def snapshot(rec):
    """The reconciliation as closed: plain JSON."""
    def money(v):
        return str(Decimal(v).quantize(Decimal('0.01')))
    res = rec['reservations']
    return {
        'payouts_matched': money(rec['payouts_matched']), 'deposits_total': money(rec['deposits_total']),
        'pairs': [{
            'date': p['line'].txn_date.isoformat(), 'amount': money(p['amount']), 'payee': p['line'].payee or p['line'].memo or p['line'].txn_type, 'difference': money(p.get('difference', 0)),
            'payouts': [{'label': g['label'], 'amount': money(g['total']), 'count': g['count']} for g in p['groups']],
        } for p in rec['pairs']],
        'accepted': [{
            'kind': i['kind'], 'amount': money(i['amount']), 'text': i['text'], 'note': i['accepted'].note,
            'by': (i['accepted'].accepted_by.get_full_name() or i['accepted'].accepted_by.username) if i['accepted'].accepted_by else '',
        } for i in rec['items'] if i['accepted']],
        'in_transit': [{'amount': money(i['amount']), 'text': i['text']} for i in rec['items'] if i['in_transit'] and not i['accepted']],
        'reservations': {
            'count': res['count'], 'payout_total': money(res['payout_total']), 'paid_in_month': money(res['paid_in_month']),
            'paid_later': money(res['paid_later']), 'carried_in': money(res['carried_in']),
            'paid_later_items': [{'guest': r['guest'], 'check_in': r['check_in'].isoformat(), 'amount': money(r['amount']), 'payout_date': r['payout_date'].isoformat() if r['payout_date'] else ''} for r in res['paid_later_items']],
        },
        'cleared_booking_ids': sorted(rec['cleared_out']),
    }


def from_close(close):
    """A closed month's frozen reconciliation, shaped like the live one where the page needs it."""
    data = close.recon or {}
    if not data:
        return None
    res = data.get('reservations', {})
    return {
        'closed': True, 'payouts_matched': Decimal(data.get('payouts_matched', '0')), 'deposits_total': Decimal(data.get('deposits_total', '0')),
        'pairs': data.get('pairs', []), 'accepted': data.get('accepted', []), 'in_transit': data.get('in_transit', []),
        'reservations': {k: (Decimal(v) if k in ('payout_total', 'paid_in_month', 'paid_later', 'carried_in') else v) for k, v in res.items()},
    }
