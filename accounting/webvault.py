"""Truist Web Vault: the monthly payment report and the unit lookup, read, lined up with our units, and tied to the deposits on the bank statement.

Web Vault is where owners pay by bill pay, ACH, e-check, card or coupon. Its monthly report lists each payment with the date it posted, the owner's Serial Acct No. and a Bill Pay Number. The
bank statement shows the same money as a few lump deposits:
  "CONSOL ELEC BILL PAY DEPOSIT n"  - n payments of type Elec Bill Pay
  "CONSOL ELEC DEPOSIT n"           - n payments of type ACH, eCheck or Credit Card
  "CONSOLIDATED COUPON PAYMENT n"   - n payments of type Coupon or MultiCoupon (each coupon is printed twice on the report: that is one payment)
all on the report's date, adding up to the deposit.

Every payment is recorded with the Web Vault date. A unit is found by its bill pay number (UnitProfile.webvault_bill_pay, filled from the unit lookup file): the first ten digits are the
unit's, the last digit is the kind of charge (1 is the regular assessment).
"""
import csv
import io
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.core import signing
from django.db import transaction

from core.models import Unit

from . import bankrec, charges, ledger, owners
from .models import ZERO, BankAccount, BankLine, Charge, Receipt, ReconLedgerItem, Reconciliation, UnitProfile

SALT = 'accounting.webvault'
TYPES = ('Elec Bill Pay', 'Check/MultiCoupon', 'Check/Coupon', 'MultiCoupon', 'Coupon', 'Credit Card', 'eCheck', 'ACH')
METHOD = {'ACH': Receipt.Method.ACH, 'eCheck': Receipt.Method.ACH, 'Elec Bill Pay': Receipt.Method.ONLINE, 'Coupon': Receipt.Method.CHECK, 'MultiCoupon': Receipt.Method.CHECK, 'Credit Card': Receipt.Method.CARD}
LABEL = {'ACH': 'ACH', 'eCheck': 'E-check', 'Elec Bill Pay': 'Bill pay', 'Coupon': 'Coupon check', 'MultiCoupon': 'Coupon check', 'Credit Card': 'Card'}
BUCKET = {'Elec Bill Pay': 'bill', 'ACH': 'elec', 'eCheck': 'elec', 'Credit Card': 'elec', 'Coupon': 'coupon', 'MultiCoupon': 'coupon'}
FUZZ_DAYS = 7                     # a payment already in the books counts as the same one when it is within this many days of the Web Vault date


class WebVaultError(Exception):
    pass


# ---------------------------------------------------------------- reading the report

_ROW = re.compile(r'(\d\d/\d\d/\d{4}) (\S+(?: ?-? ?\d+)*?) (\d{11}) ([\d,]+\.\d\d) (.*?)(?= \d\d/\d\d/\d{4} |Number of Transactions|$)')
_TYPE = re.compile(r'(?<![A-Za-z/])(' + '|'.join(re.escape(t) for t in TYPES) + r')(?![A-Za-z])')


def read_report(data):
    """(payments, meta) from the Web Vault payment report (PDF bytes). A payment: {date, serial, bill, amount, type, text}. Coupons are printed twice and counted once."""
    import pypdf
    try:
        pages = [p.extract_text() or '' for p in pypdf.PdfReader(io.BytesIO(data)).pages]
    except Exception as e:                                                            # an unreadable file: say so plainly
        raise WebVaultError('That file could not be read as a PDF.') from e
    text = re.sub(r'\s+', ' ', ' '.join(pages))
    meta = {}
    m = re.search(r'Posting Date:\s*(\d\d/\d\d/\d{4}) to (\d\d/\d\d/\d{4})', text)
    if m:
        meta['start'], meta['end'] = datetime.strptime(m.group(1), '%m/%d/%Y').date(), datetime.strptime(m.group(2), '%m/%d/%Y').date()
    m = re.search(r'Proper Realty Co (.*?)-\d{8,} Account Number', text)
    if m:
        meta['name'] = m.group(1).strip()
    m = re.search(r'Number of Transactions:\s*(\d+)\s*Amount of Transactions:\s*([\d,]+\.\d\d)', text)
    if m:
        meta['count'], meta['total'] = int(m.group(1)), Decimal(m.group(2).replace(',', ''))
    rows = []
    for m in _ROW.finditer(text):
        rest = m.group(5).strip()
        t = _TYPE.search(rest)
        if not t:
            continue
        rows.append({'date': datetime.strptime(m.group(1), '%m/%d/%Y').date(), 'serial': re.sub(r'\s+', '', m.group(2)), 'bill': m.group(3), 'amount': Decimal(m.group(4).replace(',', '')),
                     'type': t.group(1), 'text': re.sub(r'\s+', ' ', rest[:t.start()]).strip()})
    payments = _one_per_payment(rows)
    if not payments:
        raise WebVaultError('No payments were found in that file. Is it the Web Vault payment report?')
    return payments, meta


def _one_per_payment(rows):
    """A coupon shows as a 'Coupon' row and a 'Check/Coupon' row: one payment. Everything else is one row, one payment."""
    out, groups = [], defaultdict(lambda: {'plain': [], 'check': []})
    order = []
    for r in rows:
        if r['type'].startswith('Check/') or r['type'] in ('Coupon', 'MultiCoupon'):
            key = (r['date'], r['bill'], r['amount'])
            g = groups[key]
            if not g['plain'] and not g['check']:
                order.append(('coupon', key))
            g['check' if r['type'].startswith('Check/') else 'plain'].append(r)
        else:
            order.append(('one', r))
    for kind, x in order:
        if kind == 'one':
            out.append(x)
            continue
        g = groups[x]
        keep = g['plain'] if len(g['plain']) >= len(g['check']) else [dict(c, type=c['type'].replace('Check/', '')) for c in g['check']]
        out.extend(keep)
    return out


# ---------------------------------------------------------------- reading the unit lookup

def read_unit_lookup(data):
    """[{bill, unit, serial, owner}] from Web Vault's Unit Lookup CSV, one per bill pay number (a unit has several; the one ending in 1 is its regular assessment)."""
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = data.decode('latin-1')
    rows = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 29 or not row[0].strip().isdigit():
            continue
        bill = row[28].strip()
        if not re.fullmatch(r'\d{11}', bill):
            continue
        rows.append({'bill': bill, 'unit': row[25].strip(), 'serial': row[26].strip(), 'owner': row[27].strip()})
    if not rows:
        raise WebVaultError('No units were found in that file. Is it the Web Vault Unit Lookup file?')
    return rows


def prefix(bill):
    return (bill or '')[:-1]


def _tokens(name):
    return {t for t in re.sub(r'[^a-z ]', ' ', (name or '').lower()).split() if len(t) > 1 and t not in ('llc', 'inc', 'and', 'the', 'trust', 'tr')}


def map_unit_rows(association, rows):
    """Tie each unit of the lookup file (its regular assessment row) to one of our units. Returns (mapped, undecided):
    mapped {prefix: unit}, undecided [{prefix, bill, unit, owner, candidates:[Unit]}] for a person to choose; nothing ambiguous is guessed."""
    units = list(Unit.objects.filter(property=association, is_active=True).order_by('label'))
    names = {u.pk: [_tokens(o.person.name) for o in u.ownerships.filter(end_date__isnull=True).select_related('person')] for u in units}
    by_label = {u.label.strip().lower(): u for u in units}
    regular = {}
    for r in rows:
        regular.setdefault(prefix(r['bill']), r)
        if r['bill'].endswith('1'):
            regular[prefix(r['bill'])] = r
    mapped, undecided = {}, []
    for pre, r in regular.items():
        want = r['unit'].strip().lower()
        hit = by_label.get(want)
        if hit is None and want:
            ends = [u for u in units if u.label.strip().lower().endswith('-' + want)]
            hit = ends[0] if len(ends) == 1 else None
        if hit is not None:
            mapped[pre] = hit
            continue
        toks = _tokens(r['owner'])
        scored = []
        for u in units:
            best = max((len(toks & n) / max(1, min(len(toks), len(n))) for n in names[u.pk] if n and toks), default=0)
            if best >= 0.5:
                scored.append((best, u))
        scored.sort(key=lambda x: -x[0])
        if scored and (len(scored) == 1 or scored[0][0] > scored[1][0]):
            mapped[pre] = scored[0][1]
        else:
            undecided.append({'prefix': pre, 'bill': r['bill'], 'unit': r['unit'], 'owner': r['owner'], 'candidates': [u for _, u in scored[:6]]})
    # two lookup units that landed on one of our units are not both right: leave them for a person
    seen = defaultdict(list)
    for pre, u in mapped.items():
        seen[u.pk].append(pre)
    for pk, pres in seen.items():
        if len(pres) > 1:
            for pre in pres:
                r = regular[pre]
                undecided.append({'prefix': pre, 'bill': r['bill'], 'unit': r['unit'], 'owner': r['owner'], 'candidates': [u for u in units if u.pk == pk]})
                mapped.pop(pre, None)
    return mapped, undecided


def saved_units(association):
    """{prefix: unit} from what was saved on our units before."""
    out = {}
    for p in UnitProfile.objects.filter(unit__property=association).exclude(webvault_bill_pay='').select_related('unit'):
        out[prefix(p.webvault_bill_pay)] = p.unit
    return out


def save_units(mapped, rows):
    """Remember each unit's regular-assessment bill pay number. (A plain write: the safe, asked-first version is save_lookup.)"""
    regular = {prefix(r['bill']): r['bill'] for r in rows if r['bill'].endswith('1')}
    n = 0
    for pre, unit in mapped.items():
        bill = regular.get(pre)
        if not bill:
            continue
        profile, _ = UnitProfile.objects.get_or_create(unit=unit, defaults={'building': owners.building_of(unit.label)})
        if profile.webvault_bill_pay != bill:
            UnitProfile.objects.filter(pk=profile.pk).update(webvault_bill_pay=bill)
            n += 1
    return n


# ---------------------------------------------------------------- the saved unit lookup

def lookup_status(association):
    """(units with a saved bill pay number, active units): the quiet line above the import."""
    active = Unit.objects.filter(property=association, is_active=True)
    return UnitProfile.objects.filter(unit__in=active).exclude(webvault_bill_pay='').count(), active.count()


def _representatives(rows):
    """{prefix: row}: one row per unit of the lookup file, its regular assessment (the bill pay number ending in 1) when there is one."""
    out = {}
    for r in rows:
        pre = prefix(r['bill'])
        if pre not in out or r['bill'].endswith('1'):
            out[pre] = r
    return out


def plan_lookup(association, rows, choices=None):
    """What saving this unit lookup would do, writing nothing.
    {'rows': [{prefix, bill, lookup_unit, owner, unit, status, stored, holder}], 'undecided': [...]}.
    status: 'same' (already saved exactly so), 'new' (nothing saved for it yet), 'changed' (a different number is saved for that unit, or another unit holds this number: only saved if a person says so).
    `choices` {prefix: unit pk} are a person's answers for the rows that could not be placed on their own."""
    choices = choices or {}
    mapped, undecided = map_unit_rows(association, rows)
    reps = _representatives(rows)
    held = saved_units(association)
    stored = {p.unit_id: p.webvault_bill_pay for p in UnitProfile.objects.filter(unit__property=association)}
    still, found = [], dict(mapped)
    for d in undecided:
        unit = Unit.objects.filter(pk=choices.get(d['prefix']), property=association).first() if choices.get(d['prefix']) else None
        if unit is not None:
            found[d['prefix']] = unit
        else:
            still.append(d)
    out = []
    for pre, unit in found.items():
        r = reps[pre]
        current, holder = stored.get(unit.pk, ''), held.get(pre)
        if current == r['bill'] and (holder is None or holder.pk == unit.pk):
            status = 'same'
        elif current in ('', r['bill']) and (holder is None or holder.pk == unit.pk):
            status = 'new'
        else:
            status = 'changed'
        out.append({'prefix': pre, 'bill': r['bill'], 'lookup_unit': r['unit'], 'owner': r['owner'], 'unit': unit, 'status': status, 'stored': current, 'holder': holder})
    return {'rows': out, 'undecided': still}


def save_lookup(association, rows, choices=None, accept=()):
    """Save the unit lookup: every unit that is new, and the people's answers. A number that would replace a different saved one is saved only for a prefix in `accept`.
    Returns {'saved': n, 'same': n, 'changed': [rows left unsaved], 'undecided': [rows still to place]}."""
    plan = plan_lookup(association, rows, choices)
    accept = set(accept or ())
    saved = same = 0
    left = []
    with transaction.atomic():
        for x in plan['rows']:
            if x['status'] == 'same':
                same += 1
                continue
            if x['status'] == 'changed' and x['prefix'] not in accept:
                left.append(x)
                continue
            if x['holder'] is not None and x['holder'].pk != x['unit'].pk:                        # the number moves to the unit a person chose
                UnitProfile.objects.filter(unit=x['holder']).update(webvault_bill_pay='')
            profile, _ = UnitProfile.objects.get_or_create(unit=x['unit'], defaults={'building': owners.building_of(x['unit'].label)})
            UnitProfile.objects.filter(pk=profile.pk).update(webvault_bill_pay=x['bill'])
            saved += 1
    return {'saved': saved, 'same': same, 'changed': left, 'undecided': plan['undecided']}


# ---------------------------------------------------------------- the preview

def _owner_for(unit):
    """The person this unit's charges are under: whoever has the most open charges, else the billed owner."""
    count = Counter(c.owner_id for c in Charge.objects.filter(unit=unit, is_void=False).prefetch_related('applications') if c.owner_id and c.balance > 0)
    if count:
        from core.models import Contact
        return Contact.objects.get(pk=count.most_common(1)[0][0])
    return owners.billed_owner(unit)


def _bank_line_of(receipt, bank):
    return receipt.entry.lines.filter(account=bank.account).first() if receipt.entry_id else None


def bucket_of_bank(description):
    d = (description or '').upper()
    if d.startswith('CONSOL ELEC BILL PAY'):
        return 'bill'
    if d.startswith('CONSOL ELEC DEPOSIT'):
        return 'elec'
    if d.startswith('CONSOLIDATED COUPON'):
        return 'coupon'
    return None


def build(association, bank, statement, payments, lookup_rows=None, choices=None):
    """Work out what importing this report would do, writing nothing. `choices` {prefix: unit pk} are a person's answers for lookup units that could not be tied by themselves.
    Returns {'items': [...], 'deposits': [...], 'undecided': [...], 'mapped': {prefix: unit}, 'problems': n, 'to_record': n}."""
    choices = choices or {}
    saved = saved_units(association)
    mapped = dict(saved)
    undecided = []
    if lookup_rows:
        auto, undecided = map_unit_rows(association, lookup_rows)
        for pre, u in auto.items():
            mapped.setdefault(pre, u)                              # what was saved before is kept
        still = []
        for d in undecided:
            if d['prefix'] in saved:
                continue
            pick = choices.get(d['prefix'])
            unit = Unit.objects.filter(pk=pick, property=association).first() if pick else None
            if unit is not None:
                mapped[d['prefix']] = unit
            else:
                still.append(d)
        undecided = still
    used_receipts = set()
    items = []
    for i, p in enumerate(payments):
        unit = mapped.get(prefix(p['bill']))
        it = {'i': i, 'p': p, 'unit': unit, 'owner': None, 'status': 'problem', 'note': '', 'receipt': None, 'tied': False, 'jl': None}
        items.append(it)
        if unit is None:
            it['note'] = 'No unit has this bill pay number yet. ' + ('Pick it below.' if lookup_rows else 'Add the unit lookup file.')
            continue
        it['owner'] = _owner_for(unit)
        rec = None
        for r in Receipt.objects.filter(association=association, unit=unit, is_void=False, amount=p['amount'], date__gte=p['date'] - timedelta(days=FUZZ_DAYS), date__lte=p['date'] + timedelta(days=FUZZ_DAYS)).order_by('date', 'pk'):
            if r.pk not in used_receipts:
                rec = r
                break
        if rec is not None:
            used_receipts.add(rec.pk)
            it['status'], it['receipt'] = 'recorded', rec
            jl = _bank_line_of(rec, bank)
            it['jl'] = jl
            it['tied'] = bool(jl is not None and ReconLedgerItem.objects.filter(line=jl).exists())
            if rec.date != p['date']:
                it['note'] = f'Recorded {rec.date:%b} {rec.date.day}; Web Vault says {p["date"]:%b} {p["date"].day}. The date is left as it is.'
            if it['tied']:
                it['note'] = (it['note'] + ' ').lstrip() + 'Already tied to a bank deposit.'
            elif jl is None:
                it['note'] = (it['note'] + ' ').lstrip() + 'It was recorded on a different bank account, so it is not tied here.'
            continue
        if it['owner'] is None:
            it['note'] = f'Unit {unit.label} has no owner on record.'
            continue
        it['status'] = 'record'
    deposits = _group_deposits(statement, items) if statement is not None else []
    return {'items': items, 'deposits': deposits, 'undecided': undecided, 'mapped': mapped, 'problems': sum(1 for x in items if x['status'] == 'problem'), 'to_record': sum(1 for x in items if x['status'] == 'record'),
            'recorded': sum(1 for x in items if x['status'] == 'recorded')}


def _group_deposits(statement, items):
    """For each unmatched Web Vault deposit on the statement, the payments that make it up: the same date (or within three days), the same kind, as many as the deposit says, adding up to it."""
    pool = [x for x in items if x['status'] in ('record', 'recorded') and not x['tied'] and (x['status'] == 'record' or x['jl'] is not None)]
    out = []
    for bl in statement.lines.filter(amount__gt=0, match__isnull=True).order_by('date', 'id'):
        b = bucket_of_bank(bl.description)
        if not b:
            continue
        m = re.search(r'(\d+)\s*$', bl.description)
        n = int(m.group(1)) if m else 0
        pick = None
        for window in (0, 3):
            cands = [x for x in pool if BUCKET[x['p']['type']] == b and abs((x['p']['date'] - bl.date).days) <= window]
            for day in sorted({x['p']['date'] for x in cands}, key=lambda d: abs((d - bl.date).days)):
                grp = [x for x in cands if x['p']['date'] == day]
                if len(grp) == n and sum((x['p']['amount'] for x in grp), ZERO) == bl.amount:
                    pick = grp
                    break
            if pick:
                break
        if pick:
            for x in pick:
                pool.remove(x)
            out.append({'line': bl, 'items': pick, 'ok': True})
        else:
            out.append({'line': bl, 'items': [], 'ok': False})
    return out


# ---------------------------------------------------------------- recording

def pack(payments, lookup_rows):
    """A signed copy of what was read, to carry from the preview to the button, so the file does not have to be sent twice and cannot be changed in between."""
    return signing.dumps({'payments': [dict(p, date=p['date'].isoformat(), amount=str(p['amount'])) for p in payments], 'lookup': lookup_rows or []}, salt=SALT, compress=True)


def unpack(token):
    try:
        raw = signing.loads(token, salt=SALT, max_age=6 * 3600)
    except signing.BadSignature as e:
        raise WebVaultError('That preview has expired. Choose the file again.') from e
    payments = [dict(p, date=date.fromisoformat(p['date']), amount=Decimal(p['amount'])) for p in raw['payments']]
    return payments, raw.get('lookup') or []


def confirm(association, bank, statement, payments, lookup_rows=None, choices=None, user=None):
    """Record what the report has that the books do not (dated as Web Vault dates it), remember each unit's bill pay number, and tie each Web Vault deposit to its payments. All or nothing.
    Run twice, the second time records nothing new and ties nothing twice."""
    with transaction.atomic():
        bank = BankAccount.objects.select_for_update().get(pk=bank.pk)
        if statement is None:
            raise WebVaultError('Upload the statement first.')
        month_first = ledger.month_start(statement.period_end)
        if Reconciliation.objects.filter(bank_account=bank, month=month_first, status=Reconciliation.Status.CLOSED).exists():
            raise WebVaultError(f'The {month_first:%B %Y} reconciliation is closed. Open it again before importing.')
        plan = build(association, bank, statement, payments, lookup_rows, choices)
        if lookup_rows:
            save_lookup(association, lookup_rows, choices)                                   # new numbers only: a saved one is never replaced here
        made = 0
        for x in plan['items']:
            if x['status'] != 'record':
                continue
            p = x['p']
            receipt = charges.receive_payment(association, x['unit'], x['owner'], p['date'], p['amount'], METHOD[p['type']], reference=f'Webvault {p["serial"]} {p["type"]}'[:80], bank_account=bank, user=user)
            x['receipt'] = receipt
            x['jl'] = receipt.entry.lines.get(account=bank.account)
            made += 1
        tied = 0
        for g in _group_deposits(statement, plan['items']):
            if not g['ok']:
                continue
            bl = BankLine.objects.select_for_update().get(pk=g['line'].pk)
            if bl.match_id:
                continue
            items = [bankrec.Item('line', x['jl'], x['jl'].entry.date, x['jl'].debit - x['jl'].credit) for x in g['items']]
            day = g['items'][0]['p']['date']
            bankrec._commit(bank, month_first, [bl], items, 'Web Vault deposit', auto=False, note=f'Web Vault {day:%m/%d}')
            tied += 1
        return {'recorded': made, 'tied': tied, 'plan': plan}
