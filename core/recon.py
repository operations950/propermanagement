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
  * a platform pays a reservation out in several dated pieces: each installment of a long stay, a
    resolution or adjustment on its own day. The reconciliation works on those dated payouts
    (onsite.PayoutLine, from the transactions file), so a $-100 resolution deposited on Aug 3 is matched to
    that piece of its reservation, not compared with the whole reservation.
  * every other deposit is matched, to the cent, to a payout (or a couple of payouts that
    landed together) dated shortly before it. Deposits arrive a day or a few days after
    the payout date, so a payout dated at the very end of a month that arrives in the
    next is normal: it is shown as "in transit" — not a problem — and matches the
    deposit next month, because a payout stays in line until it has cleared.
  * the question each month is "what hit the bank, and is there a payout behind each of it?".
    Deposits (and take-backs: a negative income line) with no payout, and payouts dated within
    the books that have not arrived, are what is left. A payout still outstanding from an
    earlier month of the books is carried into the next month and can clear whenever it lands.
    A payout dated before the books start is only ever a candidate for an early deposit in the
    first month; if it matches nothing it reached the bank before the ledger begins, and is not
    reported as missing.
  * whatever is left over blocks the close until it is fixed (re-code the deposit, fix
    the amount in QuickBooks, upload the missing report) or accepted as a reconciling
    item with a note. An accepted item is remembered for that month at that amount.
  * a month that closes freezes its reconciliation, including which reservations it
    cleared, so a later month never re-counts them.

Alongside that, the reservations view: which reservations CHECK IN during the month and
how much of their payouts arrived within it — a five-night stay that starts on the
31st belongs to that month even though its money lands in the next."""
import re
from datetime import date, timedelta
from decimal import Decimal
from itertools import combinations

from django.utils import timezone

from . import ledger
from .models import LedgerLine, MonthClose, ReconAcceptance, ReconTie

ZERO = Decimal('0.00')
EARLY = timedelta(days=3)      # a deposit may be booked a little before the payout's own date
LATE = timedelta(days=10)      # ... and usually lands within a few days after it
TRANSIT = timedelta(days=5)    # a payout dated this close to month end is expected to land next month
LOOKBACK = timedelta(days=10)  # payouts this far before the books start can still land inside the first month
MAX_MONTHS = 36
MAX_PIECES = 8                 # the payouts of one reservation considered when adding them up to a deposit
TIE_DAYS = 7                   # how far either side of the month the manual tie lists reservations


def _bookings(book):
    """The platform payouts this set of books answers for: reservations with a payout amount, or with dated payout lines."""
    from django.db.models import Q

    from onsite.models import Booking
    qs = (Booking.objects.filter(property=book.property, source__in=(Booking.Source.AIRBNB, Booking.Source.VRBO))
          .filter((Q(payout_amount__isnull=False) & ~Q(payout_amount=0)) | Q(payout_lines__isnull=False)).distinct())
    if book.unit is not None:
        qs = qs.filter(unit=book.unit)
    return qs.prefetch_related('payout_lines')


def _source_label(source):
    return {'airbnb': 'Airbnb', 'vrbo': 'VRBO'}.get(source, source)


class _Event:
    """One dated movement of a reservation's platform money: what was paid out on one day. A reservation has
    one (paid in one go), or several (a long stay's installments, a resolution paid later)."""
    __slots__ = ('booking', 'date', 'amount')

    def __init__(self, booking, date, amount):
        self.booking, self.date, self.amount = booking, date, amount

    @property
    def pk(self):
        return self.booking.pk

    @property
    def key(self):
        return (self.booking.pk, self.date)


def _events(scope):
    return [_Event(b, d, a) for b in scope for d, a in sorted(b.cash_events().items())]


def _is_cleared(event, cleared):
    """`cleared` holds event keys (booking id, day) and, for months closed before payouts were tracked by
    day, whole booking ids."""
    return event.pk in cleared or event.key in cleared


def _groups(events):
    """Money paid out together (same platform, same day) is one payout; that is what shows up as one deposit."""
    groups = {}
    for ev in events:
        b = ev.booking
        key = f'{b.source}:{ev.date.isoformat()}'
        g = groups.setdefault(key, {'key': key, 'source': b.source, 'date': ev.date, 'total': ZERO, 'ids': [], 'events': [], 'guests': []})
        g['total'] += ev.amount
        if b.pk not in g['ids']:
            g['ids'].append(b.pk)
        g['events'].append(ev.key)
        if b.guest_name and len(g['guests']) < 3 and b.guest_name not in g['guests']:
            g['guests'].append(b.guest_name)
    for g in groups.values():
        g['count'] = len(g['ids'])
        g['label'] = f'{_source_label(g["source"])} payout dated {g["date"]:%b} {g["date"].day}'
        g['total'] = g['total'].quantize(Decimal('0.01'))
    return sorted(groups.values(), key=lambda g: (g['date'], g['source']))


def _income_deposits(book, month):
    # Negative ones count too: a platform can take money back (a resolution, an adjustment), and QuickBooks
    # shows that as a negative line in the trust account, matched by the negative payout.
    return [l for l in ledger.month_lines(book, month) if l.role == LedgerLine.Role.TRUST and l.category == LedgerLine.Category.DEPOSIT and l.flow != 0]


def _pieces(hits, dep, pool, used):
    """The payouts a deposit naming reservation codes is made of. A reservation is paid out in pieces (its
    payout, a credit given to the guest as a negative line, a resolution, the pass-through tax), and the pieces of
    ONE reservation add up to what is deposited: so for each named reservation some of its payouts are chosen
    such that everything adds up to the deposit, fewest and nearest first. If nothing adds up exactly, the
    nearest payout of each named reservation (the deposit is then shown as a difference). Returns (events, exact)."""
    per = []
    for b in hits:
        mine = sorted((ev for ev in pool if ev.pk == b.pk and ev.key not in used), key=lambda ev: (abs((dep.txn_date - ev.date).days), ev.date))
        if mine:
            per.append(mine[:MAX_PIECES])
    if not per:
        return [], False
    reachable = {ZERO: []}
    for mine in per:
        options = {}
        for size in range(1, len(mine) + 1):
            for combo in combinations(mine, size):
                options.setdefault(sum((ev.amount for ev in combo), ZERO), list(combo))
        step = {}
        for total, chosen in reachable.items():
            for part, combo in options.items():
                step.setdefault(total + part, chosen + combo)
        reachable = step
    exact = reachable.get(dep.flow)
    if exact:
        return exact, True
    return [mine[0] for mine in per], False


def _pair(events, dep, month, manual=False, note=''):
    """A deposit matched to particular dated payouts of particular reservations (by code, or by hand)."""
    books = [ev.booking for ev in events]
    codes = ', '.join(sorted({b.external_uid for b in books}))
    day = min(ev.date for ev in events)
    expected = sum((ev.amount for ev in events), ZERO).quantize(Decimal('0.01'))
    group = {
        'key': ('tie:' if manual else 'codes:') + ','.join(sorted(ev.booking.external_uid + '@' + ev.date.isoformat() for ev in events)), 'source': books[0].source, 'date': day,
        'total': expected, 'ids': sorted({b.pk for b in books}), 'events': [ev.key for ev in events], 'guests': [b.guest_name for b in books if b.guest_name][:3], 'count': len({b.pk for b in books}),
        'codes': codes, 'label': f'{_source_label(books[0].source)} payout for {codes} dated {day:%b} {day.day}', 'carried': day < month,
        'on_file': [(ev.booking.external_uid, ev.booking.payout_amount, ev.booking.pass_through_amount, ev.booking.other_payout_amount) for ev in events],
    }
    return {'line': dep, 'groups': [group], 'amount': dep.flow, 'expected': expected, 'difference': (dep.flow - expected).quantize(Decimal('0.01')), 'by_code': not manual, 'manual': manual, 'note': note}


def _match_month(book, month, cleared, scope, events):
    """One month's matching. `scope` is the book's platform-payout reservations, `events` their dated payouts."""
    month_end = ledger.next_month(month) - timedelta(days=1)
    first = ledger.month_of(ledger.books_start())
    lower = first - LOOKBACK
    deposits = sorted(_income_deposits(book, month), key=lambda l: (l.txn_date, l.pk))
    pool = [ev for ev in events if not _is_cleared(ev, cleared) and lower <= ev.date <= month_end + EARLY]
    used = set()
    # First, deposits that name their reservation(s) by confirmation code. A code names the reservation, not the
    # day: a resolution is paid on its own day, a long stay in installments, so of each named reservation's
    # payouts the one nearest the deposit is the one this deposit is (the exact amount first, for one code).
    # Deposits a person tied by hand come first: they claim the payouts they picked.
    ties = {t.line_id: t for t in ReconTie.objects.filter(month=month, **book.scope())}
    by_key = {ev.key: ev for ev in events}
    code_pairs, plain = [], []
    untied = []
    for dep in deposits:
        tie = ties.get(dep.pk)
        picked = None
        if tie is not None and tie.amount == dep.flow:
            found = [by_key.get((pk, date.fromisoformat(day))) for pk, day in tie.events]
            if found and all(found) and not any(_is_cleared(ev, cleared) or ev.key in used for ev in found):
                picked = found
        if picked is None:
            untied.append(dep)
            continue
        used |= {ev.key for ev in picked}
        code_pairs.append(_pair(picked, dep, month, manual=True, note=tie.note))
    deposits = untied
    named = [b for b in scope if b.external_uid]
    for dep in deposits:
        text = f'{dep.payee} {dep.memo} {dep.description}'
        hits = [b for b in named if re.search(r'(?<![A-Za-z0-9])' + re.escape(b.external_uid) + r'(?![A-Za-z0-9])', text, re.IGNORECASE)]
        chosen, _exact = _pieces(hits, dep, pool, used)
        if not chosen:
            plain.append(dep)
            continue
        used |= {ev.key for ev in chosen}
        code_pairs.append(_pair(chosen, dep, month))
    deposits = plain
    free = _groups([ev for ev in pool if ev.key not in used])
    pairs, open_deposits = list(code_pairs), []
    for dep in deposits:
        # A payout carried over from an earlier month can land whenever it lands; one dated this month is expected within LATE.
        cands = [g for g in free if g['date'] - EARLY <= dep.txn_date and (dep.txn_date <= g['date'] + LATE or g['date'] < month)]
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
            for g in chosen:
                g['carried'] = g['date'] < month
            pairs.append({'line': dep, 'groups': list(chosen), 'amount': dep.flow})
            free = [g for g in free if g not in chosen]
        else:
            open_deposits.append(dep)
    for p in pairs:
        p['carried'] = sum((g['total'] for g in p['groups'] if g.get('carried')), ZERO)
    matched = {key for p in pairs for g in p['groups'] for key in g['events']}
    # Only payouts from inside the books are owed to the bank. One dated just before the books start is
    # let into the pool above so it can match a deposit that lands early in the first month, but if it
    # matched nothing it reached the bank before the ledger begins: not something this month can chase.
    open_groups = [g for g in free if first <= g['date'] <= month_end]
    return {'pairs': pairs, 'open_deposits': open_deposits, 'open_groups': open_groups, 'cleared_out': cleared | matched, 'month_end': month_end,
            'mismatched': [p for p in code_pairs if p['difference'] != 0]}


def _cleared_by_closes(prop, month):
    closes = list(MonthClose.objects.filter(property=prop, month=month))
    if not closes:
        return None
    cleared = set()
    for close in closes:
        data = close.recon or {}
        cleared |= set(data.get('cleared_booking_ids', []))
        cleared |= {(pk, date.fromisoformat(day)) for pk, day in data.get('cleared_events', [])}
    return cleared


def _cleared_before(book, month, scope, events):
    """What the months before this one have cleared: frozen for a closed month, worked out for an open one."""
    cleared = set()
    for m in _month_sequence(month):
        if m == month:
            break
        frozen = _cleared_by_closes(book.property, m)
        if frozen is not None:
            cleared |= frozen
        else:
            cleared = _match_month(book, m, cleared, scope, events)['cleared_out']
    return cleared


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
    # In the first month of the books nothing could have been carried forward from the month before, so a deposit
    # that pays out an earlier stay may be marked as one.
    first_month = month == ledger.month_of(ledger.books_start())
    items = []
    for pair in result.get('mismatched', []):
        dep, diff, group = pair['line'], pair['difference'], pair['groups'][0]
        who = dep.shown_memo or dep.txn_type
        hint = ''
        if diff > 0 and not any((pt or 0) for _c, _p, pt, _o in group['on_file']):
            hint = " Airbnb's transactions file lists a separate 'Pass Through Tot' line (tax it pays the host) for each stay: upload the latest transactions file and it is included."
        a = accepted.get(('deposit', f'line:{dep.pk}'))
        items.append({
            'kind': 'deposit', 'key': f'line:{dep.pk}', 'amount': dep.flow, 'date': dep.txn_date, 'in_transit': False, 'mismatch': True,
            'text': f'{dep.txn_date:%b} {dep.txn_date.day}: ${abs(dep.flow):,.2f} {"deposited" if dep.flow > 0 else "taken out"} ({who}) {"is tied by hand to" if pair.get("manual") else "names"} {group["codes"]}, but is ${abs(diff):,.2f} {"more" if diff > 0 else "less"} '
                    f'than the ${pair["expected"]:,.2f} {"on the payouts it is tied to" if pair.get("manual") else "that platform payout is on file for"}.{"" if pair.get("manual") else hint}',
            'tied': bool(pair.get('manual')),
            'accepted': a if a and a.amount == dep.flow else None,
        })
    for dep in result['open_deposits']:
        who = dep.shown_memo or dep.txn_type
        a = accepted.get(('deposit', f'line:{dep.pk}'))
        items.append({
            'kind': 'deposit', 'key': f'line:{dep.pk}', 'amount': dep.flow, 'date': dep.txn_date, 'in_transit': False, 'prior_ok': first_month,
            'text': f'{dep.txn_date:%b} {dep.txn_date.day}: ${abs(dep.flow):,.2f} {"deposited" if dep.flow > 0 else "taken out"} ({who}) with no matching platform payout',
            'accepted': a if a and a.amount == dep.flow else None,
        })
    for g in result['open_groups']:
        a = accepted.get(('payout', g['key']))
        transit = g['date'] > result['month_end'] - TRANSIT
        carried = g['date'] < month
        items.append({
            'kind': 'payout', 'key': g['key'], 'amount': g['total'], 'date': g['date'], 'in_transit': transit, 'carried': carried,
            'text': f'{"Carried over from " + format(g["date"], "%B") + ": " if carried else ""}{g["label"]}: ${g["total"]:,.2f} for {g["count"]} reservation{"" if g["count"] == 1 else "s"}'
                    f'{" (" + ", ".join(g["guests"]) + ")" if g["guests"] else ""} has not reached the trust account'
                    f'{" — it was still on its way at the end of that month and has not arrived yet" if carried else ""}',
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
    events = _events(scope)
    cleared = _cleared_before(book, month, scope, events)
    result = _match_month(book, month, cleared, scope, events)
    items = _items(book, month, result)
    deposits = _income_deposits(book, month)
    month_end = result['month_end']
    matched_payouts = sum((p.get('expected', p['amount']) for p in result['pairs']), ZERO)
    booking_ids = {c for c in result['cleared_out'] if isinstance(c, int)}
    undated = [b for b in _undated(book) if b.pk not in booking_ids and month_end >= timezone.localtime(b.check_in).date() >= ledger.month_of(ledger.books_start()) - LOOKBACK]
    unassigned = 0
    if book.unit is not None:
        from onsite.models import Booking
        unassigned = Booking.objects.filter(
            property=book.property, unit__isnull=True, source__in=(Booking.Source.AIRBNB, Booking.Source.VRBO),
            payout_date__gte=ledger.month_of(ledger.books_start()), payout_date__lte=month_end, payout_amount__isnull=False,
        ).exclude(payout_amount=0).count()
    return {
        'closed': False, 'month': month, 'pairs': result['pairs'], 'items': items, 'cleared_out': result['cleared_out'],
        'payouts_matched': matched_payouts, 'deposits_total': sum((d.flow for d in deposits), ZERO),
        'carried_payouts': sum((p['carried'] for p in result['pairs']), ZERO),
        'prior_total': sum((i['amount'] for i in items if i['accepted'] and i['accepted'].prior_period), ZERO),
        'undated': len(undated), 'undated_total': sum((b.payout_amount for b in undated), ZERO), 'unassigned': unassigned,
        'reservations': _reservations_view(book, month, scope),
    }


def _undated(book):
    from onsite.models import Booking
    qs = Booking.objects.filter(property=book.property, source__in=(Booking.Source.AIRBNB, Booking.Source.VRBO), payout_date__isnull=True, payout_amount__isnull=False, payout_lines__isnull=True).exclude(payout_amount=0)
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

def accept_item(book, month, user, kind, key, note, prior_period=False):
    """A person accepts one open item as a reconciling item, with the reason. `prior_period` marks a deposit in the
    first month of the books as paying out something from before the books start (a reason is then optional)."""
    month = ledger.month_of(month)
    if ledger.is_closed(book, month):
        raise ledger.CloseError(f'{month:%B %Y} is closed; it can no longer be changed.')
    note = (note or '').strip()
    rec = reconcile(book, month)
    item = next((i for i in rec['items'] if i['kind'] == kind and i['key'] == key), None)
    if item is None:
        raise ledger.CloseError('That item is no longer open — the page has been refreshed.')
    if prior_period:
        if not item.get('prior_ok'):
            raise ledger.CloseError(f'Only an unmatched deposit in the first month of the books ({ledger.month_of(ledger.books_start()):%B %Y}) can be marked as from before the books.')
        note = note or f'Payout from before {month:%B %Y}, where the books start'
    elif not note:
        raise ledger.CloseError('Say why this is a reconciling item — a short note is required.')
    ReconAcceptance.objects.update_or_create(
        month=month, kind=kind, key=key, **book.scope(),
        defaults={'amount': item['amount'], 'description': item['text'][:300], 'note': note[:300], 'prior_period': bool(prior_period), 'accepted_by': user},
    )
    return item


def unaccept_item(book, month, kind, key):
    month = ledger.month_of(month)
    if ledger.is_closed(book, month):
        raise ledger.CloseError(f'{month:%B %Y} is closed; it can no longer be changed.')
    return ReconAcceptance.objects.filter(month=month, kind=kind, key=key, **book.scope()).delete()[0]


# --- tying a deposit to reservations by hand --------------------------------------------------------

def _event_key(raw):
    pk, _sep, day = str(raw).partition('|')
    try:
        return int(pk), date.fromisoformat(day)
    except ValueError:
        raise ledger.CloseError('One of the chosen payouts could not be read — reload the page and choose again.')


def tie_candidates(book, month, rec):
    """The reservations around the month (a stay checking in, or a payout, from TIE_DAYS before it to TIE_DAYS
    after it) with their dated payouts, for a person to pick what a deposit is made of. Each payout says whether it
    is free, already cleared by an earlier month, or matched to another deposit this month."""
    from django.utils import timezone as tz

    from onsite.models import Booking
    month = ledger.month_of(month)
    lo = month - timedelta(days=TIE_DAYS)
    hi = ledger.next_month(month) - timedelta(days=1) + timedelta(days=TIE_DAYS)
    scope = list(_bookings(book))
    events = _events(scope)
    cleared = _cleared_before(book, month, scope, events)
    taken, soft = {}, {}
    for p in rec['pairs']:
        for g in p['groups']:
            for key in g['events']:
                # a payout the program paired with a deposit that does not add up is still open to be tied elsewhere; one a person tied is not
                (soft if p.get('difference') and not p.get('manual') else taken)[key] = f'{p["line"].txn_date:%b} {p["line"].txn_date.day} deposit'
    rows = {}
    for ev in events:
        if lo <= ev.date <= hi:
            rows.setdefault(ev.pk, ev.booking)
    stays = Booking.objects.filter(property=book.property, source__in=(Booking.Source.AIRBNB, Booking.Source.VRBO))
    if book.unit is not None:
        stays = stays.filter(unit=book.unit)
    for b in stays:
        if lo <= tz.localtime(b.check_in).date() <= hi and (b.status == Booking.Status.ACTIVE or b.payout_amount):
            rows.setdefault(b.pk, b)
    kinds = {'reservation': 'payout', 'pass_through': 'pass-through tax', 'other': 'resolution/adjustment'}
    out = []
    for b in rows.values():
        lines = list(b.payout_lines.all()) if hasattr(b, 'payout_lines') else []
        pieces = []
        for ev in (e for e in events if e.pk == b.pk):
            parts = [f'{kinds.get(l.kind, l.kind)} {l.amount:+,.2f}' for l in lines if l.date == ev.date] or [f'payout {ev.amount:+,.2f}']
            state, label = ('cleared', 'already matched in an earlier month') if _is_cleared(ev, cleared) else (('taken', f'matched to the {taken[ev.key]}') if ev.key in taken else ('free', f'paired with the {soft[ev.key]}, which does not add up' if ev.key in soft else ''))
            pieces.append({'key': f'{ev.pk}|{ev.date.isoformat()}', 'date': ev.date, 'amount': ev.amount, 'parts': '; '.join(parts), 'state': state, 'label': label})
        out.append({
            'id': b.pk, 'code': b.external_uid, 'guest': b.guest_name or 'Guest', 'source': _source_label(b.source), 'check_in': tz.localtime(b.check_in).date(), 'check_out': tz.localtime(b.check_out).date(),
            'status': b.status, 'pieces': pieces, 'first': min([p['date'] for p in pieces] + [tz.localtime(b.check_in).date()]),
        })
    return sorted(out, key=lambda r: (r['first'], r['code']))


def tie_deposit(book, month, user, key, event_keys, note='', new_amounts=None):
    """Tie an open deposit to the payouts a person picked. Each has to exist and not be spoken for; the deposit
    is then matched to them, and any difference is still an item to accept with a reason.

    `new_amounts` is {booking id: (amount, date)} for a reservation the tie panel offered with NO payout on
    file at all (nothing was ever imported for it, or the import missed it): the person is asserting, from
    what they can see in the platform's own file, that it paid out this amount on this day. That is recorded
    on the reservation itself (Booking.payout_amount/payout_date, a PayoutLine) — not just this one tie — so
    the reservation is right from here on, in this month and any other deposit or month that names it."""
    from onsite.models import Booking, PayoutLine
    month = ledger.month_of(month)
    if ledger.is_closed(book, month):
        raise ledger.CloseError(f'{month:%B %Y} is closed; it can no longer be changed.')
    rec = reconcile(book, month)
    item = next((i for i in rec['items'] if i['kind'] == 'deposit' and i['key'] == key), None)
    if item is None:
        raise ledger.CloseError('That deposit is no longer open — the page has been refreshed.')
    picked = [_event_key(k) for k in event_keys]
    for b_pk, (amount, day) in (new_amounts or {}).items():
        if not amount or amount <= 0 or day is None:
            continue
        booking = Booking.objects.filter(pk=b_pk, property=book.property, unit=book.unit, source__in=(Booking.Source.AIRBNB, Booking.Source.VRBO)).first()
        if booking is None:
            raise ledger.CloseError('One of the reservations is no longer on file — reload the page and try again.')
        if booking.payout_amount is not None or booking.payout_lines.exists():
            raise ledger.CloseError(f'{booking.external_uid} already has a payout on file now — reload the page and tie to that instead of entering a new one.')
        PayoutLine.objects.create(booking=booking, kind=PayoutLine.Kind.RESERVATION, date=day, amount=amount)
        booking.payout_amount, booking.payout_date, booking.amount_source = amount, day, 'reconciliation tie'
        booking.save(update_fields=['payout_amount', 'payout_date', 'amount_source'])
        picked.append((b_pk, day))
    if not picked:
        raise ledger.CloseError('Choose at least one payout to tie the deposit to.')
    dep = LedgerLine.objects.get(pk=int(key.split(':')[1]))
    scope = list(_bookings(book))
    events = _events(scope)
    by_key = {ev.key: ev for ev in events}
    cleared = _cleared_before(book, month, scope, events)
    elsewhere = {k for p in rec['pairs'] if p['line'].pk != dep.pk and (p.get('manual') or not p.get('difference')) for g in p['groups'] for k in g['events']}
    for k in picked:
        if k not in by_key:
            raise ledger.CloseError('One of the chosen payouts is no longer on file — reload the page and choose again.')
        if _is_cleared(by_key[k], cleared) or k in elsewhere:
            raise ledger.CloseError(f'The payout for {by_key[k].booking.external_uid} on {k[1]:%b} {k[1].day} is already matched to another deposit.')
    ReconTie.objects.update_or_create(
        line=dep, defaults={'property': book.property, 'unit': book.unit, 'month': month, 'events': [[pk, day.isoformat()] for pk, day in sorted(set(picked))],
                            'amount': dep.flow, 'note': (note or '').strip()[:300], 'tied_by': user},
    )


def untie_deposit(book, month, key):
    month = ledger.month_of(month)
    if ledger.is_closed(book, month):
        raise ledger.CloseError(f'{month:%B %Y} is closed; it can no longer be changed.')
    return ReconTie.objects.filter(line_id=int(key.split(':')[1]), month=month, **book.scope()).delete()[0]


# --- what a close freezes ----------------------------------------------------------------------

def snapshot(rec):
    """The reconciliation as closed: plain JSON."""
    def money(v):
        return str(Decimal(v).quantize(Decimal('0.01')))
    res = rec['reservations']
    return {
        'payouts_matched': money(rec['payouts_matched']), 'deposits_total': money(rec['deposits_total']), 'prior_total': money(rec['prior_total']), 'carried_payouts': money(rec['carried_payouts']),
        'pairs': [{
            'date': p['line'].txn_date.isoformat(), 'amount': money(p['amount']), 'payee': p['line'].shown_memo or p['line'].txn_type, 'difference': money(p.get('difference', 0)),
            'manual': bool(p.get('manual')), 'payouts': [{'label': g['label'], 'amount': money(g['total']), 'count': g['count'], 'carried': bool(g.get('carried'))} for g in p['groups']],
        } for p in rec['pairs']],
        'accepted': [{
            'kind': i['kind'], 'amount': money(i['amount']), 'text': i['text'], 'note': i['accepted'].note, 'prior_period': i['accepted'].prior_period,
            'by': (i['accepted'].accepted_by.get_full_name() or i['accepted'].accepted_by.username) if i['accepted'].accepted_by else '',
        } for i in rec['items'] if i['accepted']],
        'in_transit': [{'amount': money(i['amount']), 'text': i['text']} for i in rec['items'] if i['in_transit'] and not i['accepted']],
        'reservations': {
            'count': res['count'], 'payout_total': money(res['payout_total']), 'paid_in_month': money(res['paid_in_month']),
            'paid_later': money(res['paid_later']), 'carried_in': money(res['carried_in']),
            'paid_later_items': [{'guest': r['guest'], 'check_in': r['check_in'].isoformat(), 'amount': money(r['amount']), 'payout_date': r['payout_date'].isoformat() if r['payout_date'] else ''} for r in res['paid_later_items']],
        },
        # what has cleared, so a later month never counts it again: whole reservations (months closed before payouts
        # were tracked by day) and dated payouts
        'cleared_booking_ids': sorted(c for c in rec['cleared_out'] if isinstance(c, int)),
        'cleared_events': sorted([pk, day.isoformat()] for pk, day in (c for c in rec['cleared_out'] if isinstance(c, tuple))),
    }


def from_close(close):
    """A closed month's frozen reconciliation, shaped like the live one where the page needs it."""
    data = close.recon or {}
    if not data:
        return None
    res = data.get('reservations', {})
    return {
        'closed': True, 'payouts_matched': Decimal(data.get('payouts_matched', '0')), 'deposits_total': Decimal(data.get('deposits_total', '0')), 'prior_total': Decimal(data.get('prior_total', '0')),
        'carried_payouts': Decimal(data.get('carried_payouts', '0')),
        'pairs': data.get('pairs', []), 'accepted': data.get('accepted', []), 'in_transit': data.get('in_transit', []),
        'reservations': {k: (Decimal(v) if k in ('payout_total', 'paid_in_month', 'paid_later', 'carried_in') else v) for k, v in res.items()},
    }
