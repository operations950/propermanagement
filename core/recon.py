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
  * every other bank line is lined up with the platform payouts by amount and by date, to the cent, and NOT
    only one to one: a payout can land as several deposits and a deposit can be several payouts, so the
    program looks for any number on either side that add up to each other. Dates are the directional
    evidence — the platform pays out a couple of days after check-in and the money reaches the bank a few
    days after that — so only lines dated close enough to be the same money are considered, and the surest
    kinds of match are made first (one to one, then one deposit made of several payouts, then one payout
    paid as several deposits, then several against several) so a loose combination never takes what a
    tighter match was waiting for.
  * whatever the program cannot line up is left OPEN on each side, and a person matches lines from the two
    sides by hand (any number of each; ReconMatch). A match they disagree with can be broken; it then stays
    open until matched by hand. A payout still outstanding from an earlier month is carried into the next month
    and matched automatically when its deposit finally lands there ("prior month").
  * once everything that can be matched is, whatever is left is a reconciling item, and each one has to be
    explained before the month closes: a timing difference (a stay near the end of the month paid out in the
    next) or a genuine bookkeeping error, with a note (save_reconciliation). Accepted items are remembered
    for that month at that amount.
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
from .models import LedgerLine, MonthClose, ReconAcceptance, ReconMatch

ZERO = Decimal('0.00')
EARLY = timedelta(days=3)      # a deposit may be booked a little before the payout's own date
LATE = timedelta(days=10)      # ... and usually lands within a few days after it
TRANSIT = timedelta(days=5)    # a payout dated this close to month end is expected to land next month
LOOKBACK = timedelta(days=10)  # payouts this far before the books start can still land inside the first month
MAX_MONTHS = 36
MAX_PIECES = 8                 # the payouts of one reservation considered when adding them up to a deposit
MAX_SIDE = 4                   # most payouts (or bank lines) in one automatic match
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
    __slots__ = ('booking', 'date', 'amount', 'kind')

    def __init__(self, booking, date, amount, kind='reservation'):
        self.booking, self.date, self.amount, self.kind = booking, date, amount, kind

    @property
    def pk(self):
        return self.booking.pk

    @property
    def key(self):
        return (self.booking.pk, self.date, self.kind)

    @property
    def label(self):
        return KIND_LABELS.get(self.kind, self.kind)

    @property
    def id(self):
        """The event as text, for forms and stored matches: booking id | day | kind."""
        return f'{self.booking.pk}|{self.date.isoformat()}|{self.kind}'


KIND_LABELS = {'reservation': 'Payout', 'pass_through': 'Pass-through tax', 'other': 'Resolution / adjustment'}


def _events(scope):
    """Every line item of platform money, each its own event: the reservation's payout (one per installment of a long
    stay), the pass-through occupancy tax, a resolution or adjustment - each is a real financial transaction on the
    platform's report and has to be matched to the bank. From the dated payout lines when the transactions file gave
    them; otherwise one of each kind on the payout date."""
    out = []
    for b in scope:
        lines = list(b.payout_lines.all())
        if lines:
            for line in sorted(lines, key=lambda l: (l.date, l.kind)):
                if line.amount != 0:
                    out.append(_Event(b, line.date, line.amount, line.kind))
        elif b.payout_date and b.payout_amount is not None:
            for kind, amount in (('reservation', b.payout_amount), ('pass_through', b.pass_through_amount), ('other', b.other_payout_amount)):
                if amount:
                    out.append(_Event(b, b.payout_date, amount, kind))
    return out


def _is_cleared(event, cleared):
    """`cleared` holds event keys (booking id, day, kind) and, for months closed before each line item was its own
    event, (booking id, day) or whole booking ids."""
    return event.pk in cleared or event.key in cleared or (event.pk, event.date) in cleared


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
        mine = sorted((ev for ev in pool if ev.pk == b.pk and ev.key not in used), key=lambda ev: (abs((dep.txn_date - ev.date).days), ev.date, ev.kind))
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


def _local_day(dt):
    return timezone.localtime(dt).date()


def _event_row(ev, month):
    b = ev.booking
    return {
        'key': ev.id, 'guest': b.guest_name or 'Guest', 'code': b.external_uid, 'source': _source_label(b.source), 'kind': ev.kind, 'kind_label': ev.label,
        'check_in': _local_day(b.check_in), 'check_out': _local_day(b.check_out), 'date': ev.date, 'amount': ev.amount, 'carried': ev.date < month,
    }


def _line_row(line):
    return {'pk': line.pk, 'date': line.txn_date, 'desc': line.shown_memo or line.txn_type or line.payee, 'amount': line.flow}


def _make_match(deps, evs, month, kind, note='', manual=False, by_code=False):
    """One matched set: any number of bank lines against any number of dated platform payouts (one payout landing as
    two deposits, two payouts as one, ...). kind is 'system' (found by the program), 'code' (the deposit's memo names
    the reservation) or 'user' (matched by hand). Carries the old one-deposit-per-pair keys (`line`, `groups`, ...)
    the close checks and the frozen snapshot still read."""
    deps = sorted(deps, key=lambda l: (l.txn_date, l.pk))
    evs = sorted(evs, key=lambda e: (e.date, e.booking.pk, e.kind))
    amount = sum((l.flow for l in deps), ZERO)
    expected = sum((e.amount for e in evs), ZERO).quantize(Decimal('0.01'))
    groups = _groups(evs)
    for g in groups:
        g['carried'] = g['date'] < month
    return {
        'kind': kind, 'lines': deps, 'events': evs, 'line': deps[0], 'groups': groups, 'amount': amount, 'expected': expected,
        'difference': (amount - expected).quantize(Decimal('0.01')), 'manual': manual, 'by_code': by_code, 'note': note,
        'prior': any(e.date < month for e in evs), 'carried': sum((g['total'] for g in groups if g['carried']), ZERO),
        'line_rows': [_line_row(l) for l in deps], 'event_rows': [_event_row(e, month) for e in evs],
        'dep_pks': [l.pk for l in deps], 'event_keys': [e.id for e in evs],
    }


def _compatible(dep, ev, month):
    """Dates as directional evidence: a deposit lands a little before to a week or so after the payout's own date
    (a payout carried over from an earlier month can land whenever it lands)."""
    return ev.date - EARLY <= dep.txn_date and (dep.txn_date <= ev.date + LATE or ev.date < month)


def _distance(dep, ev):
    return abs((dep.txn_date - ev.date).days)


def _auto_match(deps, evs, month):
    """Lines up whatever bank lines and platform payouts add up to each other, in passes from the surest to the
    loosest so a loose combination can never take something a tighter match was waiting for: one to one, then one
    deposit made of several payouts, then one payout paid in several deposits, then several against several. Each
    pass looks only at what is dated close enough to be the same money. Returns [(deposits, payouts), ...]."""
    deps, evs = list(deps), list(evs)
    found = []

    def take(ds, es):
        found.append((list(ds), list(es)))
        for d in ds:
            deps.remove(d)
        for e in es:
            evs.remove(e)

    # one to one — nearest dates first
    candidates = sorted(
        ((_distance(d, e), d.txn_date, d.pk, e.date, e.booking.pk, e.kind, d, e) for d in deps for e in evs if d.flow == e.amount and _compatible(d, e, month)),
        key=lambda t: t[:6],
    )
    for *_key, d, e in candidates:
        if d in deps and e in evs:
            take([d], [e])

    # one deposit made of several payouts
    for size in range(2, MAX_SIDE + 1):
        for d in sorted(deps, key=lambda l: (l.txn_date, l.pk)):
            if d not in deps:
                continue
            near = sorted((e for e in evs if _compatible(d, e, month)), key=lambda e: (_distance(d, e), e.date, e.booking.pk, e.kind))[:12]
            for combo in combinations(near, size):
                if sum((e.amount for e in combo), ZERO) == d.flow:
                    take([d], combo)
                    break

    # one payout paid as several deposits
    for size in range(2, MAX_SIDE + 1):
        for e in sorted(evs, key=lambda x: (x.date, x.booking.pk, x.kind)):
            if e not in evs:
                continue
            near = sorted((d for d in deps if _compatible(d, e, month)), key=lambda d: (_distance(d, e), d.txn_date, d.pk))[:12]
            for combo in combinations(near, size):
                if sum((d.flow for d in combo), ZERO) == e.amount:
                    take(combo, [e])
                    break

    # several deposits against several payouts - looked for around each remaining deposit in turn, among the lines
    # dated close enough to it
    options = []
    for anchor in sorted(deps, key=lambda l: (l.txn_date, l.pk)):
        others = sorted((d for d in deps if d is not anchor and abs((d.txn_date - anchor.txn_date).days) <= LATE.days),
                        key=lambda d: (abs((d.txn_date - anchor.txn_date).days), d.pk))[:6]
        cand_deps = [anchor] + others
        cand_evs = sorted((e for e in evs if any(_compatible(d, e, month) for d in cand_deps)), key=lambda e: (_distance(anchor, e), e.date, e.booking.pk, e.kind))[:12]
        if len(cand_evs) < 2:
            continue
        ev_sums = {}
        for size in range(2, 4):
            for combo in combinations(cand_evs, size):
                ev_sums.setdefault(sum((e.amount for e in combo), ZERO), []).append(combo)
        for size in range(2, 4):
            for rest in combinations(others, size - 1):
                dcombo = (anchor,) + rest
                for ecombo in ev_sums.get(sum((d.flow for d in dcombo), ZERO), []):
                    if all(any(_compatible(d, e, month) for d in dcombo) for e in ecombo) and all(any(_compatible(d, e, month) for e in ecombo) for d in dcombo):
                        spread = max(d.txn_date for d in dcombo) - min(d.txn_date for d in dcombo) + (max(e.date for e in ecombo) - min(e.date for e in ecombo))
                        options.append((len(dcombo) + len(ecombo), spread, dcombo, ecombo))
    for _n, _spread, dcombo, ecombo in sorted(options, key=lambda o: (o[0], o[1])):
        if all(d in deps for d in dcombo) and all(e in evs for e in ecombo):
            take(dcombo, ecombo)
    return found


def _match_month(book, month, cleared, scope, events):
    """One month's matching. `scope` is the book's platform-payout reservations, `events` their dated payouts."""
    month_end = ledger.next_month(month) - timedelta(days=1)
    first = ledger.month_of(ledger.books_start())
    lower = first - LOOKBACK
    deposits = sorted(_income_deposits(book, month), key=lambda l: (l.txn_date, l.pk))
    by_line = {l.pk: l for l in deposits}
    by_key = {ev.key: ev for ev in events}
    pool = [ev for ev in events if not _is_cleared(ev, cleared) and lower <= ev.date <= month_end + EARLY]
    used, used_lines = set(), set()
    blocked_lines, blocked_events = set(), set()
    manual_pairs, code_pairs, system_pairs = [], [], []

    by_day = {}
    for ev in events:
        by_day.setdefault((ev.pk, ev.date), []).append(ev.key)

    def keys_of(raw):
        """Stored payouts as event keys. One stored without its kind (made when a day's money was one payout) is every
        line item of that reservation on that day."""
        out = []
        for item in raw:
            try:
                pk, day = int(item[0]), date.fromisoformat(item[1])
            except (TypeError, ValueError, IndexError):
                out.append(None)
                continue
            if len(item) >= 3:
                out.append((pk, day, item[2]))
            else:
                out.extend(by_day.get((pk, day), [None]))
        return out

    stored = list(ReconMatch.objects.filter(month=month, **book.scope()).order_by('pk'))
    # Matches a person broke keep those lines open: they are not offered to the automatic matching again.
    for m in stored:
        if m.kind == ReconMatch.Kind.HOLD:
            blocked_lines |= {int(pk) for pk in m.lines}
            blocked_events |= {k for k in keys_of(m.events) if k}
    # Matches a person made by hand come first: they claim the lines and payouts they picked.
    for m in stored:
        if m.kind != ReconMatch.Kind.USER:
            continue
        dl = [by_line.get(int(pk)) for pk in m.lines]
        el = [by_key.get(k) if k else None for k in keys_of(m.events)]
        if not dl or not el or not all(dl) or not all(el):
            continue
        if any(l.pk in used_lines for l in dl) or any(_is_cleared(ev, cleared) or ev.key in used for ev in el):
            continue
        used_lines |= {l.pk for l in dl}
        used |= {ev.key for ev in el}
        manual_pairs.append(_make_match(dl, el, month, 'user', note=m.note, manual=True) | {'stored_pk': m.pk})

    # Deposits that name their reservation(s) by confirmation code. A code names the reservation, not the day: a
    # resolution is paid on its own day, a long stay in installments, so of each named reservation's payouts the one
    # nearest the deposit is the one this deposit is (the exact amount first, for one code).
    named = [b for b in scope if b.external_uid]
    for dep in deposits:
        if dep.pk in used_lines or dep.pk in blocked_lines:
            continue
        text = f'{dep.payee} {dep.memo} {dep.description}'
        hits = [b for b in named if re.search(r'(?<![A-Za-z0-9])' + re.escape(b.external_uid) + r'(?![A-Za-z0-9])', text, re.IGNORECASE)]
        chosen, _exact = _pieces(hits, dep, pool, used | blocked_events)
        if not chosen:
            continue
        used_lines.add(dep.pk)
        used |= {ev.key for ev in chosen}
        code_pairs.append(_make_match([dep], chosen, month, 'code', by_code=True))

    # Everything else lines up by amount and date, any number against any number.
    rem_deps = [d for d in deposits if d.pk not in used_lines and d.pk not in blocked_lines]
    rem_evs = [ev for ev in pool if ev.key not in used and ev.key not in blocked_events]
    for ds, es in _auto_match(rem_deps, rem_evs, month):
        used_lines |= {d.pk for d in ds}
        used |= {e.key for e in es}
        system_pairs.append(_make_match(ds, es, month, 'system'))

    matches = manual_pairs + code_pairs + system_pairs
    matches.sort(key=lambda p: (p['lines'][0].txn_date, p['lines'][0].pk))
    matched = {key for p in matches for key in (ev.key for ev in p['events'])}
    open_deposits = [d for d in deposits if d.pk not in used_lines]
    # Only payouts from inside the books are owed to the bank. One dated just before the books start is
    # let into the pool above so it can match a deposit that lands early in the first month, but if it
    # matched nothing it reached the bank before the ledger begins: not something this month can chase.
    open_events = [ev for ev in pool if ev.key not in used and first <= ev.date <= month_end]
    return {
        'matches': matches, 'pairs': matches, 'open_deposits': open_deposits, 'open_events': open_events,
        'cleared_out': cleared | matched, 'month_end': month_end,
        'mismatched': [p for p in matches if p['difference'] != 0],
    }


def _cleared_by_closes(prop, month):
    closes = list(MonthClose.objects.filter(property=prop, month=month))
    if not closes:
        return None
    cleared = set()
    for close in closes:
        data = close.recon or {}
        cleared |= set(data.get('cleared_booking_ids', []))
        cleared |= {(item[0], date.fromisoformat(item[1]), *item[2:]) for item in data.get('cleared_events', [])}
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
    """What is left over, as items a person has to explain: a match whose two sides differ, a bank line with no
    platform payout behind it, a platform payout that has not reached the bank."""
    accepted = {(a.kind, a.key): a for a in ReconAcceptance.objects.filter(month=month, **book.scope())}
    # In the first month of the books nothing could have been carried forward from the month before, so a deposit
    # that pays out an earlier stay may be marked as one.
    first_month = month == ledger.month_of(ledger.books_start())
    month_end = result['month_end']
    items = []
    for pair in result.get('mismatched', []):
        diff = pair['difference']
        first_line = pair['lines'][0]
        who = first_line.shown_memo or first_line.txn_type
        codes = ', '.join(sorted({ev.booking.external_uid for ev in pair['events']}))
        hint = ''
        if diff > 0 and pair['by_code'] and not any((ev.booking.pass_through_amount or 0) for ev in pair['events']):
            hint = " Airbnb's transactions file lists a separate 'Pass Through Tot' line (tax it pays the host) for each stay: upload the latest transactions file and it is included."
        a = accepted.get(('deposit', f'line:{first_line.pk}'))
        n = len(pair['lines'])
        items.append({
            'kind': 'deposit', 'key': f'line:{first_line.pk}', 'amount': pair['amount'], 'date': first_line.txn_date, 'in_transit': False, 'mismatch': True,
            'text': f'{first_line.txn_date:%b} {first_line.txn_date.day}: ${abs(pair["amount"]):,.2f} {"deposited" if pair["amount"] > 0 else "taken out"}{"" if n == 1 else f" across {n} bank lines"} ({who}) '
                    f'{"is matched by hand to" if pair["manual"] else "names"} {codes}, but is ${abs(diff):,.2f} {"more" if diff > 0 else "less"} '
                    f'than the ${pair["expected"]:,.2f} {"on the payouts it is matched to" if pair["manual"] else "that platform payout is on file for"}.{"" if pair["manual"] else hint}',
            'tied': bool(pair['manual']), 'matched': True, 'note_hint': pair.get('note', ''), 'reason_hint': 'error',
            'dep_pks': pair['dep_pks'], 'event_keys': pair['event_keys'], 'stored_pk': pair.get('stored_pk'),
            'accepted': a if a and a.amount == pair['amount'] else None,
        })
    for dep in result['open_deposits']:
        who = dep.shown_memo or dep.txn_type
        a = accepted.get(('deposit', f'line:{dep.pk}'))
        items.append({
            'kind': 'deposit', 'key': f'line:{dep.pk}', 'amount': dep.flow, 'date': dep.txn_date, 'in_transit': False, 'prior_ok': first_month,
            'text': f'{dep.txn_date:%b} {dep.txn_date.day}: ${abs(dep.flow):,.2f} {"deposited" if dep.flow > 0 else "taken out"} ({who}) with no matching platform payout',
            'reason_hint': 'error', 'accepted': a if a and a.amount == dep.flow else None,
        })
    for ev in result['open_events']:
        key = f'event:{ev.id}'
        a = accepted.get(('payout', key))
        transit = ev.date > month_end - TRANSIT
        carried = ev.date < month
        b = ev.booking
        label = f'{_source_label(b.source)} {"payout" if ev.kind == "reservation" else ev.label.lower()} dated {ev.date:%b} {ev.date.day}'
        items.append({
            'kind': 'payout', 'key': key, 'amount': ev.amount, 'date': ev.date, 'in_transit': transit, 'carried': carried,
            'text': f'{"Carried over from " + format(ev.date, "%B") + ": " if carried else ""}{label}: ${ev.amount:,.2f} for 1 reservation'
                    f'{" (" + b.guest_name + ")" if b.guest_name else ""} has not reached the trust account'
                    f'{" — it was still on its way at the end of that month and has not arrived yet" if carried else ""}',
            'reason_hint': 'timing' if (transit or carried) else 'error', 'event_key': ev.id,
            'accepted': a if a and a.amount == ev.amount else None,
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


def assign_units_from_deposits(prop):
    """A reservation filed under the property with no unit (a report with no listing on it) is given its unit when the bank
    shows which one it was paid to: its confirmation code sits in the memo of a deposit booked to exactly one unit. Returns
    how many were assigned. Never overrides a unit already set, and leaves a code that turns up under two units alone."""
    from onsite.models import Booking
    loose = list(Booking.objects.filter(property=prop, unit__isnull=True, source__in=(Booking.Source.AIRBNB, Booking.Source.VRBO)).exclude(external_uid=''))
    if not loose:
        return 0
    memos = list(LedgerLine.objects.filter(property=prop, unit__isnull=False, flow__gt=0).values_list('memo', 'unit_id'))
    assigned = 0
    for b in loose:
        code = b.external_uid.upper()
        units = {unit_id for memo, unit_id in memos if code in (memo or '').upper()}
        if len(units) == 1:
            b.unit_id = units.pop()
            b.save(update_fields=['unit'])
            assigned += 1
    return assigned


def reconcile(book, month):
    """The live reconciliation of an open month: every match (by the program, by memo code, or by hand), what's left
    over on each side, what has been accepted, and the reservations view. None if the month is already closed."""
    month = ledger.month_of(month)
    if ledger.is_closed(book, month):
        return None
    if book.unit is not None:
        assign_units_from_deposits(book.property)
    scope = list(_bookings(book))
    events = _events(scope)
    cleared = _cleared_before(book, month, scope, events)
    result = _match_month(book, month, cleared, scope, events)
    items = _items(book, month, result)
    deposits = _income_deposits(book, month)
    month_end = result['month_end']
    matched_payouts = sum((p['expected'] for p in result['matches']), ZERO)
    booking_ids = {c for c in result['cleared_out'] if isinstance(c, int)}
    undated = [b for b in _undated(book) if b.pk not in booking_ids and month_end >= timezone.localtime(b.check_in).date() >= ledger.month_of(ledger.books_start()) - LOOKBACK]
    unassigned_rows = []
    if book.unit is not None:
        from onsite.models import Booking
        unassigned_rows = list(Booking.objects.filter(
            property=book.property, unit__isnull=True, source__in=(Booking.Source.AIRBNB, Booking.Source.VRBO),
            payout_date__gte=ledger.month_of(ledger.books_start()), payout_date__lte=month_end, payout_amount__isnull=False,
        ).exclude(payout_amount=0).order_by('payout_date', 'pk'))
    unassigned = len(unassigned_rows)
    acc = {(i['kind'], i['key']): i['accepted'] for i in items}
    mismatch_keys = {i['key'] for i in items if i.get('matched')}
    return {
        'closed': False, 'month': month, 'pairs': result['matches'], 'matches': result['matches'], 'items': items, 'cleared_out': result['cleared_out'],
        'open_lines': [{**_line_row(l), 'key': f'line:{l.pk}', 'accepted': acc.get(('deposit', f'line:{l.pk}'))} for l in result['open_deposits']],
        'open_payouts': [{**_event_row(ev, month), 'accepted': acc.get(('payout', f'event:{ev.id}'))} for ev in result['open_events']],
        'mismatch_keys': mismatch_keys,
        'payouts_matched': matched_payouts, 'deposits_total': sum((d.flow for d in deposits), ZERO),
        'carried_payouts': sum((p['carried'] for p in result['matches']), ZERO),
        'prior_total': sum((i['amount'] for i in items if i['accepted'] and i['accepted'].prior_period), ZERO),
        'undated': len(undated), 'undated_total': sum((b.payout_amount for b in undated), ZERO), 'unassigned': unassigned,
        'unassigned_list': [{'pk': b.pk, 'guest': b.guest_name or 'Guest', 'code': b.external_uid, 'amount': b.payout_amount, 'date': b.payout_date} for b in unassigned_rows],
        'reservations': _reservations_view(book, month, scope),
    }


def _undated(book):
    from onsite.models import Booking
    qs = Booking.objects.filter(property=book.property, source__in=(Booking.Source.AIRBNB, Booking.Source.VRBO), payout_date__isnull=True, payout_amount__isnull=False, payout_lines__isnull=True).exclude(payout_amount=0)
    if book.unit is not None:
        qs = qs.filter(unit=book.unit)
    return list(qs.exclude(status='cancelled'))


def problems(rec):
    """(open deposits, open payouts) still needing to be matched or explained. A payout dated just before month end
    that will clear next month is still a reconciling item: it needs its note like any other."""
    deposits = [i for i in rec['items'] if i['kind'] == 'deposit' and not i['accepted']]
    payouts = [i for i in rec['items'] if i['kind'] == 'payout' and not i['accepted']]
    return deposits, payouts


def is_clean(rec):
    d, p = problems(rec)
    return not d and not p and not rec['unassigned']


# --- explaining what is left over ---------------------------------------------------------------

def accept_item(book, month, user, kind, key, note, prior_period=False, reason=''):
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
        reason = ReconAcceptance.Reason.PRIOR_BOOKS
    elif not note:
        raise ledger.CloseError('Say why this is a reconciling item — a short note is required.')
    if reason and reason not in ReconAcceptance.Reason.values:
        raise ledger.CloseError('Choose why it is a reconciling item.')
    ReconAcceptance.objects.update_or_create(
        month=month, kind=kind, key=key, **book.scope(),
        defaults={'amount': item['amount'], 'description': item['text'][:300], 'note': note[:300], 'prior_period': bool(prior_period), 'reason': reason or '', 'accepted_by': user},
    )
    return item


def save_reconciliation(book, month, user, entries):
    """Finalize the reconciliation: everything still left over is a reconciling item, and each one has to be explained.
    `entries` is [{'kind', 'key', 'reason', 'note'}, ...] — one for every open item. A reconciling item is either a
    timing difference (a stay at the end of the month paid out in the next) or a genuine bookkeeping error."""
    month = ledger.month_of(month)
    if ledger.is_closed(book, month):
        raise ledger.CloseError(f'{month:%B %Y} is closed; it can no longer be changed.')
    rec = reconcile(book, month)
    pending = [i for i in rec['items'] if not i['accepted']]
    given = {(e.get('kind'), e.get('key')): e for e in entries}
    blank = [i for i in pending if not (given.get((i['kind'], i['key'])) or {}).get('note', '').strip()]
    if blank:
        raise ledger.CloseError(f'{len(blank)} reconciling item{"" if len(blank) == 1 else "s"} still {"needs" if len(blank) == 1 else "need"} a note — say why each one is left over.')
    unsure = [i for i in pending if (given[(i['kind'], i['key'])].get('reason') or '') not in (ReconAcceptance.Reason.TIMING, ReconAcceptance.Reason.ERROR, ReconAcceptance.Reason.PRIOR_BOOKS)]
    if unsure:
        raise ledger.CloseError('Say whether each reconciling item is a timing difference or a bookkeeping error.')
    for i in pending:
        e = given[(i['kind'], i['key'])]
        prior = e['reason'] == ReconAcceptance.Reason.PRIOR_BOOKS
        if prior and not i.get('prior_ok'):
            raise ledger.CloseError('Only an unmatched deposit in the first month of the books can be marked as from before the books.')
        ReconAcceptance.objects.update_or_create(
            month=month, kind=i['kind'], key=i['key'], **book.scope(),
            defaults={'amount': i['amount'], 'description': i['text'][:300], 'note': e['note'].strip()[:300], 'prior_period': prior, 'reason': e['reason'], 'accepted_by': user},
        )
    return len(pending)


def unaccept_item(book, month, kind, key):
    month = ledger.month_of(month)
    if ledger.is_closed(book, month):
        raise ledger.CloseError(f'{month:%B %Y} is closed; it can no longer be changed.')
    return ReconAcceptance.objects.filter(month=month, kind=kind, key=key, **book.scope()).delete()[0]


# --- matching by hand -----------------------------------------------------------------------------

def _event_key(raw):
    """'booking id|day|kind' -> (id, day, kind)."""
    parts = str(raw).split('|')
    try:
        return int(parts[0]), date.fromisoformat(parts[1]), (parts[2] if len(parts) > 2 else '')
    except (ValueError, IndexError):
        raise ledger.CloseError('One of the chosen payouts could not be read — reload the page and choose again.')


def manual_match(book, month, user, line_ids, event_keys, note=''):
    """Match open bank lines to open platform payouts by hand — any number of each. If the two sides add up to the
    same amount that is the whole match; if they don't the difference is still an item to explain, and the match
    needs a note saying why."""
    month = ledger.month_of(month)
    if ledger.is_closed(book, month):
        raise ledger.CloseError(f'{month:%B %Y} is closed; it can no longer be changed.')
    rec = reconcile(book, month)
    open_lines = {r['pk']: r for r in rec['open_lines']}
    open_payouts = {r['key']: r for r in rec['open_payouts']}
    try:
        line_ids = sorted({int(x) for x in line_ids})
    except (TypeError, ValueError):
        raise ledger.CloseError('One of the chosen bank lines could not be read — reload the page and choose again.')
    keys = sorted({_event_key(k) for k in event_keys})
    if not line_ids or not keys:
        raise ledger.CloseError('Choose at least one bank line and at least one platform payout to match.')
    if any(pk not in open_lines for pk in line_ids) or any(f'{pk}|{day.isoformat()}|{kind}' not in open_payouts for pk, day, kind in keys):
        raise ledger.CloseError('One of those is no longer open — the page has been refreshed; choose again.')
    bank = sum((open_lines[pk]['amount'] for pk in line_ids), ZERO)
    platform = sum((open_payouts[f'{pk}|{day.isoformat()}|{kind}']['amount'] for pk, day, kind in keys), ZERO)
    note = (note or '').strip()
    if bank != platform and not note:
        raise ledger.CloseError(f'The bank lines (${bank:,.2f}) and the payouts (${platform:,.2f}) differ by ${abs(bank - platform):,.2f} — say why in the note to match them anyway.')
    return ReconMatch.objects.create(
        kind=ReconMatch.Kind.USER, month=month, lines=line_ids, events=[[pk, day.isoformat(), kind] for pk, day, kind in keys],
        note=note[:300], created_by=user, **book.scope(),
    )


def unmatch(book, month, user, line_ids, event_keys, stored_pk=None):
    """Break a match. One a person made is simply removed; one the program made is kept open from then on (a hold), so
    it is not put straight back together."""
    month = ledger.month_of(month)
    if ledger.is_closed(book, month):
        raise ledger.CloseError(f'{month:%B %Y} is closed; it can no longer be changed.')
    if stored_pk:
        return ReconMatch.objects.filter(pk=stored_pk, kind=ReconMatch.Kind.USER, month=month, **book.scope()).delete()[0]
    try:
        line_ids = sorted({int(x) for x in line_ids})
    except (TypeError, ValueError):
        raise ledger.CloseError('One of the bank lines could not be read — reload the page.')
    keys = sorted({_event_key(k) for k in event_keys})
    if not line_ids:
        raise ledger.CloseError('Nothing to unmatch.')
    return ReconMatch.objects.create(
        kind=ReconMatch.Kind.HOLD, month=month, lines=line_ids, events=[[pk, day.isoformat(), kind] for pk, day, kind in keys], created_by=user, **book.scope(),
    )


def payoutless_reservations(book, month):
    """Reservations around the month (a stay checking in from TIE_DAYS before it to TIE_DAYS after) with NO payout on
    file at all — nothing imported for them, or the import missed them — so a person can give the amount and day the
    platform's own file shows they were paid (add_missing_payout)."""
    from onsite.models import Booking
    month = ledger.month_of(month)
    lo = month - timedelta(days=TIE_DAYS)
    hi = ledger.next_month(month) - timedelta(days=1) + timedelta(days=TIE_DAYS)
    stays = Booking.objects.filter(property=book.property, source__in=(Booking.Source.AIRBNB, Booking.Source.VRBO), status=Booking.Status.ACTIVE, payout_amount__isnull=True, payout_lines__isnull=True)
    if book.unit is not None:
        stays = stays.filter(unit=book.unit)
    rows = [b for b in stays if lo <= _local_day(b.check_in) <= hi]
    return sorted(
        ({'id': b.pk, 'code': b.external_uid, 'guest': b.guest_name or 'Guest', 'source': _source_label(b.source), 'check_in': _local_day(b.check_in), 'check_out': _local_day(b.check_out)} for b in rows),
        key=lambda r: (r['check_in'], r['code']),
    )


def add_missing_payout(book, month, user, booking_id, amount, day):
    """Record, from what the platform's own file shows, that a reservation with NO payout on file paid out this amount
    on this day. Saved on the reservation itself (Booking.payout_amount/payout_date, a PayoutLine) — not just this
    month — so it is right from here on, and it appears with the other open payouts to be matched."""
    from onsite.models import Booking, PayoutLine
    month = ledger.month_of(month)
    if ledger.is_closed(book, month):
        raise ledger.CloseError(f'{month:%B %Y} is closed; it can no longer be changed.')
    if not amount or amount <= 0 or day is None:
        raise ledger.CloseError('Give the amount and the day it was paid.')
    booking = Booking.objects.filter(pk=booking_id, property=book.property, unit=book.unit, source__in=(Booking.Source.AIRBNB, Booking.Source.VRBO)).first()
    if booking is None:
        raise ledger.CloseError('That reservation is no longer on file — reload the page and try again.')
    if booking.payout_amount is not None or booking.payout_lines.exists():
        raise ledger.CloseError(f'{booking.external_uid} already has a payout on file now — reload the page.')
    PayoutLine.objects.create(booking=booking, kind=PayoutLine.Kind.RESERVATION, date=day, amount=amount)
    booking.payout_amount, booking.payout_date, booking.amount_source = amount, day, 'reconciliation entry'
    booking.save(update_fields=['payout_amount', 'payout_date', 'amount_source'])
    return booking


# --- what a close freezes ----------------------------------------------------------------------

def snapshot(rec):
    """The reconciliation as closed: plain JSON."""
    def money(v):
        return str(Decimal(v).quantize(Decimal('0.01')))
    res = rec['reservations']
    return {
        'payouts_matched': money(rec['payouts_matched']), 'deposits_total': money(rec['deposits_total']), 'prior_total': money(rec['prior_total']), 'carried_payouts': money(rec['carried_payouts']),
        'matches': [{
            'kind': p['kind'], 'amount': money(p['amount']), 'expected': money(p['expected']), 'difference': money(p['difference']), 'note': p.get('note', ''), 'prior': bool(p['prior']),
            'lines': [{'date': r['date'].isoformat(), 'desc': r['desc'], 'amount': money(r['amount'])} for r in p['line_rows']],
            'events': [{'guest': r['guest'], 'code': r['code'], 'source': r['source'], 'kind': r['kind'], 'kind_label': r['kind_label'], 'check_in': r['check_in'].isoformat(), 'check_out': r['check_out'].isoformat(),
                        'date': r['date'].isoformat(), 'amount': money(r['amount']), 'carried': bool(r['carried'])} for r in p['event_rows']],
        } for p in rec['matches']],
        'pairs': [{
            'date': p['line'].txn_date.isoformat(), 'amount': money(p['amount']), 'payee': p['line'].shown_memo or p['line'].txn_type, 'difference': money(p.get('difference', 0)),
            'manual': bool(p.get('manual')), 'payouts': [{'label': g['label'], 'amount': money(g['total']), 'count': g['count'], 'carried': bool(g.get('carried'))} for g in p['groups']],
        } for p in rec['matches']],
        'accepted': [{
            'kind': i['kind'], 'amount': money(i['amount']), 'text': i['text'], 'note': i['accepted'].note, 'prior_period': i['accepted'].prior_period, 'reason': i['accepted'].reason,
            'reason_label': i['accepted'].get_reason_display() if i['accepted'].reason else '',
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
        'cleared_events': sorted([c[0], c[1].isoformat(), *c[2:]] for c in rec['cleared_out'] if isinstance(c, tuple)),
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
        'pairs': data.get('pairs', []), 'matches': data.get('matches', []), 'accepted': data.get('accepted', []), 'in_transit': data.get('in_transit', []),
        'reservations': {k: (Decimal(v) if k in ('payout_total', 'paid_in_month', 'paid_later', 'carried_in') else v) for k, v in res.items()},
    }
