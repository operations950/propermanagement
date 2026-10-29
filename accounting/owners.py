"""Units, their owners, each unit's share, and bringing the owners in from a spreadsheet."""
import csv
import io
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal

from django.db import transaction

from core.models import Contact, Unit

from .models import BoardTerm, OwnerProfile, Ownership, ShareKind, UnitProfile, UnitShare

COMPANY_WORDS = re.compile(r'\b(corp|corporation|inc|incorporated|llc|l\.l\.c|ltd|company|co\.|trust|partners|holdings|association|properties|realty|enterprises)\b', re.I)
DEFAULT_SHARE = 'Common expenses'


class OwnerError(Exception):
    pass


def building_of(label):
    """7601-111 is in building 7601."""
    return label.split('-')[0] if '-' in label else ''


def ensure_unit(association, label, building=None):
    unit, _ = Unit.objects.get_or_create(property=association, label=label)
    profile, made = UnitProfile.objects.get_or_create(unit=unit, defaults={'building': building if building is not None else building_of(label)})
    return unit


def active_units(association):
    return Unit.objects.filter(property=association, is_active=True).select_related('assoc_profile').order_by('label')


# ---------------------------------------------------------------- shares

def default_share_kind(association):
    kind = ShareKind.objects.filter(association=association, is_default=True).first()
    if kind is None:
        kind, _ = ShareKind.objects.get_or_create(association=association, name=DEFAULT_SHARE, defaults={'is_default': True})
        if not kind.is_default:
            kind.is_default = True
            kind.save(update_fields=['is_default'])
    return kind


def set_equal_shares(association, kind=None):
    """Every unit gets the same share, handed out in millionths of a percent so the total is exactly 100."""
    kind = kind or default_share_kind(association)
    units = list(active_units(association))
    if not units:
        return 0
    n = len(units)
    unit_millionths = 100 * 1_000_000
    base, extra = divmod(unit_millionths, n)
    for i, u in enumerate(units):
        millionths = base + (1 if i < extra else 0)
        UnitShare.objects.update_or_create(unit=u, kind=kind, defaults={'percent': Decimal(millionths) / Decimal(1_000_000)})
    return n


def shares(association, kind=None):
    """[(unit, percent)] for every active unit; a unit with no share yet counts as 0."""
    kind = kind or default_share_kind(association)
    have = {s.unit_id: s.percent for s in UnitShare.objects.filter(kind=kind)}
    return [(u, have.get(u.pk, Decimal('0'))) for u in active_units(association)]


def shares_total(association, kind=None):
    return sum((p for u, p in shares(association, kind)), Decimal('0'))


def split_by_share(total, share_rows):
    """Divide `total` among units by percent so the cents add up exactly: everyone gets their share rounded down, and the spare cents go to
    the units with the largest remainders (ties to the first units)."""
    total = Decimal(str(total)).quantize(Decimal('0.01'))
    whole = sum((p for u, p in share_rows), Decimal('0'))
    if whole <= 0:
        raise OwnerError('No unit has a share yet, so there is nothing to split by.')
    exact = [(u, total * p / whole) for u, p in share_rows]
    floors = [(u, x.quantize(Decimal('0.01'), rounding=ROUND_DOWN)) for u, x in exact]
    spare_cents = int(((total - sum((f for u, f in floors), Decimal('0'))) * 100).to_integral_value())
    order = sorted(range(len(exact)), key=lambda i: (-(exact[i][1] - floors[i][1]), i))
    out = [f for u, f in floors]
    for i in order[:spare_cents]:
        out[i] += Decimal('0.01')
    return [(share_rows[i][0], out[i]) for i in range(len(share_rows))]


# ---------------------------------------------------------------- who owns what

def current_ownerships(unit):
    return list(unit.ownerships.filter(end_date__isnull=True).select_related('person'))


def billed_owner(unit):
    """The person a unit's charges go to: the owner marked as billed, else the first current owner."""
    own = current_ownerships(unit)
    billed = next((o for o in own if o.is_billed), None) or (own[0] if own else None)
    return billed.person if billed else None


def attach_owner(association, contact, is_company=False):
    """Make an existing contact an owner at this association (their name, email and phone stay as they are)."""
    if contact.contact_type != Contact.ContactType.OWNER and Contact.ContactType.OWNER not in (contact.secondary_types or []):
        contact.secondary_types = list(contact.secondary_types or []) + [Contact.ContactType.OWNER]
        contact.save(update_fields=['secondary_types'])
    contact.properties.add(association)
    OwnerProfile.objects.get_or_create(contact=contact, defaults={'is_company': is_company or bool(COMPANY_WORDS.search(contact.name))})
    return contact


def create_owner_contact(association, name, source=Contact.Source.MANUAL):
    """A new owner Contact, whatever else is already in the system with the same name."""
    name = ' '.join((name or '').split())
    return attach_owner(association, Contact.objects.create(name=name, contact_type=Contact.ContactType.OWNER, source=source))


def person_for_name(association, name, is_company=False):
    """The owner Contact with this name for this association, made if there is none."""
    name = ' '.join((name or '').split())
    existing = Contact.objects.filter(name__iexact=name, properties=association).first() or Contact.objects.filter(name__iexact=name, contact_type=Contact.ContactType.OWNER).first()
    if existing is None:
        existing = Contact.objects.create(name=name, contact_type=Contact.ContactType.OWNER, source=Contact.Source.MANUAL)
    return attach_owner(association, existing, is_company)


@transaction.atomic
def add_owner(unit, person, start=None, occupancy=Ownership.Occupancy.OCCUPANT, billed=None):
    """Make `person` a current owner of `unit`. The first owner of a unit is billed unless told otherwise."""
    existing = Ownership.objects.filter(unit=unit, person=person, end_date__isnull=True).first()
    if existing:
        return existing
    first = not current_ownerships(unit)
    is_billed = first if billed is None else billed
    if is_billed:
        Ownership.objects.filter(unit=unit, end_date__isnull=True, is_billed=True).update(is_billed=False)
    own = Ownership.objects.create(unit=unit, person=person, start_date=start, occupancy=occupancy, is_billed=is_billed)
    person.units.add(unit)
    person.properties.add(unit.property)
    return own


@transaction.atomic
def set_billed(unit, person):
    own = Ownership.objects.filter(unit=unit, person=person, end_date__isnull=True).first()
    if own is None:
        raise OwnerError('That person is not a current owner of the unit.')
    Ownership.objects.filter(unit=unit, end_date__isnull=True).update(is_billed=False)
    own.is_billed = True
    own.save(update_fields=['is_billed'])


@transaction.atomic
def record_sale(unit, sale_date, new_people, billed_index=0):
    """Everyone who owns the unit now stops owning it the day before `sale_date`; the new owners start on it. Charges already posted stay with the
    former owners, so what they still owe stays theirs. `new_people` is [(Contact, occupancy)]."""
    if not new_people:
        raise OwnerError('Say who the new owner is.')
    for own in current_ownerships(unit):
        own.end_date = sale_date - timedelta(days=1)
        own.is_billed = False
        own.save(update_fields=['end_date', 'is_billed'])
    out = []
    for i, (person, occupancy) in enumerate(new_people):
        out.append(add_owner(unit, person, start=sale_date, occupancy=occupancy, billed=(i == billed_index)))
    return out


def end_ownership(own, end_date):
    own.end_date = end_date
    own.is_billed = False
    own.save(update_fields=['end_date', 'is_billed'])


# ---------------------------------------------------------------- board

def add_board_term(association, person, position, start=None, end=None):
    return BoardTerm.objects.create(association=association, person=person, position=position, start_date=start, end_date=end)


BOARD_ORDER = {'president': 0, 'vice president': 1, 'vp': 1, 'secretary': 2, 'treasurer': 3}


def board(association, current_only=True):
    """The board, officers first (president, vice president, secretary, treasurer), then the rest by name."""
    qs = BoardTerm.objects.filter(association=association).select_related('person')
    rows = list(qs.filter(end_date__isnull=True) if current_only else qs)
    return sorted(rows, key=lambda t: (BOARD_ORDER.get(t.position.lower(), 9), t.person.name))


# ---------------------------------------------------------------- importing owners from a spreadsheet

HEADER_WORDS = {'unit': ('unit', 'unit #', 'unit number', 'apt', 'lot'), 'owner': ('owner', 'owners', 'owner(s)', 'name', 'owner name'),
                'type': ('owner type', 'type', 'occupancy'), 'from': ('from', 'purchase', 'purchase date', 'since', 'start')}


def _column(headers, key):
    for i, h in enumerate(headers):
        if (h or '').strip().lower() in HEADER_WORDS[key]:
            return i
    return None


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    for fmt in ('%m/%d/%Y', '%Y-%m-%d', '%m/%d/%y'):
        try:
            return datetime.strptime(str(value).strip(), fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def read_owner_rows(name, data):
    """Rows of {unit, owner, absentee, since} from an Excel or CSV file's bytes."""
    ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    if ext in ('xlsx', 'xlsm'):
        import openpyxl
        rows = list(openpyxl.load_workbook(io.BytesIO(data), data_only=True).worksheets[0].iter_rows(values_only=True))
    elif ext in ('csv', 'txt'):
        rows = list(csv.reader(io.StringIO(data.decode('utf-8-sig', errors='replace'))))
    else:
        raise OwnerError('Upload the owners as an Excel or CSV file.')
    if not rows:
        raise OwnerError('The file is empty.')
    headers = [str(c) if c is not None else '' for c in rows[0]]
    ui, oi, ti, fi = _column(headers, 'unit'), _column(headers, 'owner'), _column(headers, 'type'), _column(headers, 'from')
    if ui is None or oi is None:
        raise OwnerError('The first row must name the columns, at least "Unit" and "Owner".')
    out = []
    for r in rows[1:]:
        r = list(r) + [None] * 8
        unit, owner = (str(r[ui]).strip() if r[ui] is not None else ''), (str(r[oi]).strip() if r[oi] is not None else '')
        if not unit or not owner:
            continue
        kind = (str(r[ti]).lower() if ti is not None and r[ti] is not None else '')
        out.append({'unit': unit, 'owner': ' '.join(owner.split()), 'absentee': 'absent' in kind or 'not' in kind or 'tenant' in kind,
                    'since': _as_date(r[fi]) if fi is not None else None})
    if not out:
        raise OwnerError('No owner rows were found.')
    return out


def plan_import(association, rows):
    """What the import found, for the person to check before anything is saved."""
    by_unit = defaultdict(list)
    names, by_person = {}, defaultdict(list)
    for r in rows:
        by_unit[r['unit']].append(r['owner'])
        names.setdefault(r['owner'].lower(), r['owner'])
        by_person[r['owner'].lower()].append(r['unit'])
    existing = set(Unit.objects.filter(property=association).values_list('label', flat=True))
    dates = defaultdict(int)
    for r in rows:
        if r['since']:
            dates[r['since']] += 1
    common = max(dates.items(), key=lambda kv: kv[1]) if dates else None
    return {
        'rows': len(rows), 'units': len(by_unit), 'new_units': len([u for u in by_unit if u not in existing]),
        'multi_owner_units': {u: owners for u, owners in by_unit.items() if len(owners) > 1},
        'multi_unit_people': {names[k]: units for k, units in by_person.items() if len(units) > 1},
        'companies': sorted({r['owner'] for r in rows if COMPANY_WORDS.search(r['owner'])}),
        'common_date': common[0] if common and common[1] > max(2, len(rows) // 2) else None,
        'common_date_count': common[1] if common else 0,
        'absentee_units': sorted({r['unit'] for r in rows if r['absentee']}),
    }


@transaction.atomic
def apply_import(association, rows, billed=None, equal_shares=True):
    """Make the units, owners and ownership rows. `billed` is {unit label: owner name} for units with more than one owner; others bill their only owner.
    Running it again changes nothing that is already there."""
    billed = {k: v.lower() for k, v in (billed or {}).items()}
    made = {'units': 0, 'owners': 0, 'ownerships': 0}
    by_unit = defaultdict(list)
    for r in rows:
        by_unit[r['unit']].append(r)
    for label, owner_rows in by_unit.items():
        had_unit = Unit.objects.filter(property=association, label=label).exists()
        unit = ensure_unit(association, label)
        made['units'] += 0 if had_unit else 1
        choice = billed.get(label)
        for r in owner_rows:
            had_person = r.get('person') is not None or Contact.objects.filter(name__iexact=r['owner'], properties=association).exists()
            person = r.get('person') or person_for_name(association, r['owner'])   # a row may bring its person: one already worked out (the Yardi load matches by email, phone, then name)
            made['owners'] += 0 if had_person else 1
            if Ownership.objects.filter(unit=unit, person=person, end_date__isnull=True).exists():
                continue
            is_billed = (r['owner'].lower() == choice) if choice else (len(owner_rows) == 1 or None)
            add_owner(unit, person, start=r['since'], occupancy=Ownership.Occupancy.ABSENTEE if r['absentee'] else Ownership.Occupancy.OCCUPANT, billed=is_billed)
            made['ownerships'] += 1
        if choice is None and len(owner_rows) > 1 and not any(o.is_billed for o in current_ownerships(unit)):
            first = current_ownerships(unit)[0]
            set_billed(unit, first.person)
    if equal_shares and not UnitShare.objects.filter(kind__association=association).exists():
        set_equal_shares(association)
    return made
