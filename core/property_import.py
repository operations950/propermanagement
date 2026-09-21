"""Bulk update of property details from a pasted JSON file (Admin Tools → Import
property details).

Production has no shell, and details like wifi passwords must not be written into the
code or the git history, so this is the way to get a batch of them in: paste the JSON,
look at what each record would change, then apply. Nothing is applied on the first
step, and the preview never displays a password (only "will change"). The pasted text is
carried inside the preview page so Apply can use it; it is not stored anywhere.

Each record is one listing — a property and (for a multi-unit building) its unit:

    property_id, unit, listing, address, check_in_time, check_out_time,
    wifi_network, wifi_password, access_note, parking, laundry, trash, amenity_notes

and lands like this:
  * check-in / check-out time      -> the property (skipped if the records for one
                                      property disagree — set that by hand);
  * wifi network + password        -> the property if all its records agree, else each
                                      unit's own (Unit.wifi_*);
  * how to get in (access_note)    -> the unit's access notes, or the property's;
  * parking / laundry / trash /
    amenity_notes                  -> FAQ entries written as staff (so the assistant
                                      can read them and can't overwrite them): one for
                                      the whole property when the same words appear for
                                      two or more of its units (or it has no unit), else
                                      one per unit;
  * address                        -> never changed; a mismatch is only flagged.
A record whose text still says VERIFY is left unticked until a person has looked.
The unit a record means is found from its listing title, then its label; when neither
matches the person picks (or has it created)."""
import json
import re
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from . import faq as faq_service
from .models import Property, PropertyFAQ, Unit

MAX_CHARS = 400_000
MAX_RECORDS = 300
TEXT_FIELDS = ('listing', 'unit', 'address', 'access_note', 'wifi_network', 'wifi_password', 'parking', 'laundry', 'trash', 'amenity_notes')
FAQ_FIELDS = (
    ('parking', 'Where do I park?'),
    ('laundry', 'Is there laundry, and where is it?'),
    ('trash', 'When is trash pickup, and where does it go?'),
    ('amenity_notes', 'Any house tips or rules I should know?'),
)
FAQ_LABELS = dict(FAQ_FIELDS)
SECRET_FIELDS = ('wifi_password',)


class ImportError_(ValueError):
    """The pasted text can't be used at all."""


def _clean(value, limit=2000):
    if value is None:
        return ''
    text = str(value).replace('\r\n', '\n').replace('\r', '\n')
    text = re.sub(r'[ \t]+', ' ', text).strip()
    return text[:limit]


def _norm(text):
    return re.sub(r'[^a-z0-9]+', ' ', (text or '').lower()).strip()


def _core_label(raw):
    """"Modern (VERIFY — see discrepancies)" -> "Modern"; "Pearl (708 NE 7th Ct)" -> "Pearl"."""
    text = re.sub(r'\(.*?\)', ' ', raw or '')
    text = re.sub(r'\bverify\b.*', '', text, flags=re.IGNORECASE)
    return ' '.join(text.split())


def _parse_time(raw):
    raw = _clean(raw)
    if not raw:
        return None
    for fmt in ('%I:%M %p', '%I:%M%p', '%I %p', '%I%p', '%H:%M'):
        try:
            return datetime.strptime(raw.upper(), fmt).time()
        except ValueError:
            continue
    return None


def parse(text):
    """The records in the pasted JSON (a list, or an object with "records"). Raises
    ImportError_ with a plain message when it can't be read."""
    text = (text or '').strip()
    if not text:
        raise ImportError_('Paste the JSON first.')
    if len(text) > MAX_CHARS:
        raise ImportError_('That is too large to import in one go — split it up.')
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ImportError_(f'That is not valid JSON ({exc.msg}, line {exc.lineno}).') from None
    rows = data.get('records') if isinstance(data, dict) else data
    if not isinstance(rows, list) or not rows:
        raise ImportError_('Expected a list of records (or an object with a "records" list).')
    if len(rows) > MAX_RECORDS:
        raise ImportError_(f'At most {MAX_RECORDS} records at a time.')
    records = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ImportError_(f'Record {i + 1} is not an object.')
        pid = row.get('property_id')
        rec = {'index': i, 'property_id': pid if isinstance(pid, int) and not isinstance(pid, bool) else None}
        for field in TEXT_FIELDS:
            rec[field] = _clean(row.get(field))
        rec['check_in'] = _parse_time(row.get('check_in_time'))
        rec['check_out'] = _parse_time(row.get('check_out_time'))
        rec['times_unreadable'] = [n for n, raw, parsed in (('check-in', row.get('check_in_time'), rec['check_in']), ('check-out', row.get('check_out_time'), rec['check_out'])) if _clean(raw) and parsed is None]
        rec['verify'] = any('verify' in rec[f].lower() for f in ('listing', 'unit', 'address'))
        rec['unit_core'] = _core_label(rec['unit'])
        rec['address_shown'] = re.sub(r'\s*\(.*?verify.*?\)', '', rec['address'], flags=re.IGNORECASE).strip()
        records.append(rec)
    return records


# --- matching ------------------------------------------------------------------------------------

def _resolve_unit(units, listing_names, rec):
    """(unit or None, how). None with how='' means the record names no unit."""
    if not rec['unit_core']:
        return None, ''
    title = _norm(rec['listing'])
    if title:
        for ln in listing_names:
            if ln.unit_id and _norm(ln.name) == title:
                unit = next((u for u in units if u.pk == ln.unit_id), None)
                if unit:
                    return unit, 'matched by its listing title'
    core = _norm(rec['unit_core'])
    hits = [u for u in units if _norm(u.label) == core]
    if len(hits) == 1:
        return hits[0], 'matched by its label'
    hits = [u for u in units if core and core in _norm(u.label).split()]
    if len(hits) == 1:
        return hits[0], 'matched by its label'
    return None, 'no unit matches'


def _street_number(text):
    m = re.match(r'\s*(\d+)', text or '')
    return m.group(1) if m else ''


def _same_text(a, b):
    return _norm(a) == _norm(b)


# --- planning ------------------------------------------------------------------------------------

def _fmt_time(t):
    return f'{t.hour % 12 or 12}:{t:%M} {"AM" if t.hour < 12 else "PM"}' if t else ''


def build_plan(records, choices=None):
    """What importing would do, grouped by property. `choices` (from the preview form)
    is {record index: {'unit': 'auto'|'none'|'new'|<unit pk>, 'apply': bool}} plus
    {'prop': {property id: bool}}; missing entries take their defaults."""
    choices = choices or {}
    prop_choices = choices.get('prop', {})
    by_property = {}
    for rec in records:
        by_property.setdefault(rec['property_id'], []).append(rec)
    plan = []
    for pid, recs in by_property.items():
        prop = Property.objects.filter(pk=pid).prefetch_related('units', 'listing_names').first() if pid else None
        block = {'property_id': pid, 'property': prop, 'records': [], 'property_changes': [], 'errors': [], 'apply': bool(prop_choices.get(pid, True))}
        if prop is None:
            block['errors'].append('No property with that id here — its records are skipped.' if pid else 'These records have no property_id — skipped.')
            block['records'] = [{'record': r, 'unit': None, 'unit_how': '', 'unit_options': [], 'changes': [], 'apply': False, 'warnings': [], 'index': r['index']} for r in recs]
            plan.append(block)
            continue
        units = [u for u in prop.units.all() if u.is_active]
        listing_names = list(prop.listing_names.all())

        # which unit each record means
        resolved = []
        for r in recs:
            choice = choices.get(r['index'], {})
            pick = str(choice.get('unit', 'auto'))
            unit, how, create = None, '', None
            if pick == 'none':
                how = 'the whole property (your choice)'
            elif pick == 'new':
                create, how = r['unit_core'], f'a new unit "{r["unit_core"]}" will be created'
            elif pick.isdigit() and any(u.pk == int(pick) for u in units):
                unit, how = next(u for u in units if u.pk == int(pick)), 'your choice'
            else:
                unit, how = _resolve_unit(units, listing_names, r)
            resolved.append({'record': r, 'unit': unit, 'create': create, 'how': how})

        def key_of(x):
            return ('u', x['unit'].pk) if x['unit'] else (('c', _norm(x['create'])) if x['create'] else ('p', None))

        # --- property-wide decisions -------------------------------------------------------
        for label, field, attr in (('check-in time', 'check_in', 'default_check_in_time'), ('check-out time', 'check_out', 'default_check_out_time')):
            values = {r[field] for r in recs if r[field]}
            if len(values) == 1:
                new = values.pop()
                old = getattr(prop, attr)
                if old != new:
                    block['property_changes'].append({'kind': 'property', 'field': attr, 'label': label.capitalize(), 'old': _fmt_time(old) or '—', 'new': _fmt_time(new), 'value': new})
            elif len(values) > 1:
                block['errors'].append(f'The records disagree about the {label} ({", ".join(sorted(_fmt_time(v) for v in values))}) — set it by hand.')
        wifis = {(r['wifi_network'], r['wifi_password']) for r in recs if r['wifi_network'] or r['wifi_password']}
        wifi_at_property = len(wifis) == 1
        if wifi_at_property:
            net, pw = next(iter(wifis))
            for field, label, new, secret in (('wifi_network', 'Wifi network', net, False), ('wifi_password', 'Wifi password', pw, True)):
                if new and getattr(prop, field) != new:
                    block['property_changes'].append({'kind': 'property', 'field': field, 'label': label, 'old': ('set' if getattr(prop, field) else 'not set') if secret else (getattr(prop, field) or '—'),
                                                      'new': 'will change' if secret else new, 'value': new, 'secret': secret})
        # a record with no unit carries its access note to the property
        # FAQ: the same words for two or more records -> the whole property
        faq_scope = {}
        for field, _q in FAQ_FIELDS:
            texts = [r[field] for r in recs if r[field]]
            faq_scope[field] = {t for t in texts if sum(1 for u in texts if _same_text(u, t)) >= 2}

        seen_keys = {}
        for x in resolved:
            r, unit, create = x['record'], x['unit'], x['create']
            changes, warnings = [], []
            index = r['index']
            if r['unit_core'] and unit is None and create is None and not x['how'].startswith('the whole'):
                warnings.append('Choose which unit this is (or create it) — the listing title and label match none of the units.')
            if not r['unit_core'] and len(units) > 1:
                warnings.append('This record names no unit, but the property has several — its details go to the whole property.')
            if r['verify']:
                warnings.append('Marked VERIFY in the data — check it before applying.')
            for name in r['times_unreadable']:
                warnings.append(f'The {name} time could not be read — skipped.')
            street = _street_number(r['address_shown'])
            if street and street not in ' '.join(_norm(f) for f in (prop.address, prop.name) + tuple(u.label for u in units)).split() and street not in prop.address:
                warnings.append(f'The address on the sheet ({r["address_shown"]}) does not obviously match "{prop.name}" — make sure the property id is right.')
            k = key_of(x)
            if k != ('p', None) and k in seen_keys:
                warnings.append('Another record in this file is for the same unit.')
            seen_keys[k] = True
            at_unit = unit is not None or create is not None

            # wifi at the unit when the property's records don't agree
            if not wifi_at_property and (r['wifi_network'] or r['wifi_password']):
                for field, label, new, secret in (('wifi_network', 'Wifi network', r['wifi_network'], False), ('wifi_password', 'Wifi password', r['wifi_password'], True)):
                    if not new:
                        continue
                    if at_unit:
                        old = getattr(unit, field) if unit else ''
                        if old != new:
                            changes.append({'kind': 'unit', 'field': field, 'label': label + ' (this unit)', 'old': (('set' if old else 'not set') if secret else (old or '—')), 'new': 'will change' if secret else new, 'value': new, 'secret': secret})
                    else:
                        old = getattr(prop, field)
                        if old != new:
                            changes.append({'kind': 'property', 'field': field, 'label': label, 'old': (('set' if old else 'not set') if secret else (old or '—')), 'new': 'will change' if secret else new, 'value': new, 'secret': secret})
            # how to get in
            if r['access_note']:
                if at_unit:
                    old = unit.access_notes if unit else ''
                    if old != r['access_note']:
                        changes.append({'kind': 'unit', 'field': 'access_notes', 'label': 'How to get in (this unit)', 'old': old or '—', 'new': r['access_note'], 'value': r['access_note']})
                else:
                    old = prop.access_notes or ''
                    if _norm(r['access_note']) not in _norm(old):
                        new = (old.rstrip() + '\n' + r['access_note']).strip() if old else r['access_note']
                        changes.append({'kind': 'property', 'field': 'access_notes', 'label': 'How to get in' + (' (added to what is there)' if old else ''), 'old': old or '—', 'new': r['access_note'], 'value': new})
            # FAQ entries
            for field, question in FAQ_FIELDS:
                text = r[field]
                if not text:
                    continue
                whole = (not at_unit) or any(_same_text(text, t) for t in faq_scope[field])
                if whole:
                    dup = next((c for c in block['property_changes'] if c['kind'] == 'faq' and c['field'] == field), None)
                    if dup is not None:
                        continue         # the same answer was already listed for the whole property
                    change = _faq_change(prop, None, question, field, text)
                    if change:
                        block['property_changes'].append(change)
                else:
                    change = _faq_change(prop, unit, question, field, text, new_unit=create is not None)
                    if change:
                        change['label'] = f'{FAQ_LABELS[field]} (this unit only)'
                        change['create_unit'] = create
                        changes.append(change)
            default_apply = not r['verify'] and (unit is not None or create is not None or not r['unit_core'] or x['how'].startswith('the whole'))
            choice = choices.get(index, {})
            block['records'].append({
                'record': r, 'index': index, 'unit': unit, 'create': create, 'unit_how': x['how'], 'unit_options': units,
                'changes': changes, 'warnings': warnings, 'apply': bool(choice.get('apply', default_apply)),
                'unit_pick': str(choice.get('unit', 'auto')),
            })
        plan.append(block)
    return plan


def _faq_change(prop, unit, question, field, text, new_unit=False):
    """One FAQ entry the import would write, or None if it is already there."""
    try:
        q, a, key = faq_service.validate(prop, question, text)
    except faq_service.FAQError as exc:
        return {'kind': 'faq', 'field': field, 'label': FAQ_LABELS[field], 'old': '—', 'new': text, 'blocked': str(exc)}
    existing = None if new_unit else PropertyFAQ.objects.filter(property=prop, question_key=key, unit=unit, status=PropertyFAQ.Status.ACTIVE).first()
    if existing is not None and _same_text(existing.answer, a):
        return None
    return {'kind': 'faq', 'field': field, 'label': FAQ_LABELS[field], 'question': q, 'key': key, 'old': existing.answer if existing else '—', 'new': a, 'value': a, 'unit': unit, 'replaces': existing is not None}


# --- applying ------------------------------------------------------------------------------------

@transaction.atomic
def apply_plan(plan, user):
    """Carries out the ticked parts of the plan. Returns {'properties': n, 'units_created': n,
    'fields': n, 'faq_added': n, 'faq_replaced': n, 'skipped': [text]} — never a secret."""
    out = {'properties': 0, 'units_created': 0, 'fields': 0, 'faq_added': 0, 'faq_replaced': 0, 'skipped': []}
    now = timezone.now()

    def write_faq(prop, unit, change):
        if change.get('blocked'):
            out['skipped'].append(f'{prop.name}: {change["label"]} — {change["blocked"]}')
            return
        entry = PropertyFAQ.objects.filter(property=prop, question_key=change['key'], unit=unit, status=PropertyFAQ.Status.ACTIVE).first()
        if entry is None:
            PropertyFAQ.objects.create(
                property=prop, unit=unit, question=change['question'], question_key=change['key'], answer=change['value'],
                origin=PropertyFAQ.Origin.STAFF, basis=PropertyFAQ.Basis.STAFF, reviewed=True, reviewed_by=user, reviewed_at=now,
                source_note='Imported from the property details file',
            )
            out['faq_added'] += 1
        else:
            entry.answer, entry.origin, entry.basis, entry.reviewed, entry.reviewed_by, entry.reviewed_at = change['value'], PropertyFAQ.Origin.STAFF, PropertyFAQ.Basis.STAFF, True, user, now
            entry.save()
            out['faq_replaced'] += 1

    for block in plan:
        prop = block['property']
        if prop is None or not block['apply']:
            continue
        touched = False
        prop_fields = []
        for change in block['property_changes']:
            if change['kind'] == 'property':
                setattr(prop, change['field'], change['value'])
                prop_fields.append(change['field'])
                out['fields'] += 1
            elif change['kind'] == 'faq':
                write_faq(prop, None, change)
            touched = True
        created = {}
        for item in block['records']:
            if not item['apply']:
                continue
            unit = item['unit']
            if unit is None and item['create']:
                label = item['create']
                unit = created.get(_norm(label)) or Unit.objects.filter(property=prop, label__iexact=label).first()
                if unit is None:
                    unit = Unit.objects.create(property=prop, label=label)
                    out['units_created'] += 1
                created[_norm(label)] = unit
            unit_fields = []
            for change in item['changes']:
                if change['kind'] == 'unit' and unit is not None:
                    setattr(unit, change['field'], change['value'])
                    unit_fields.append(change['field'])
                    out['fields'] += 1
                elif change['kind'] == 'property':
                    setattr(prop, change['field'], change['value'])
                    prop_fields.append(change['field'])
                    out['fields'] += 1
                elif change['kind'] == 'faq':
                    write_faq(prop, unit, change)
                touched = True
            if unit is not None and unit_fields:
                unit.save(update_fields=sorted(set(unit_fields)))
        if prop_fields:
            prop.save(update_fields=sorted(set(prop_fields)))
        out['properties'] += bool(touched)
    return out
