"""Everything the property detail screen knows about a property, as plain data an
outside program (the message-answering assistant) can read.

Three tiers, decided by the caller's key (see BotAccessKey):
  * always: facts that are safe to tell a guest — size, address, check-in and
    check-out times, amenities, where the shutoffs are, the wifi NETWORK name,
    platform listing titles, and the FAQ;
  * access info (allow_access_info): gate/door/lockbox/alarm codes, the wifi
    password, per-unit codes and access notes. Without it these are absent and the
    profile only says which of them exist, so the assistant can say "I'll have
    someone send that" instead of guessing;
  * internal (allow_internal_info): staff notes, contacts and document names.

Nothing is invented. A fact nobody has recorded is null, and `facts_missing` lists
them, so the assistant knows what it does not know.
"""
from django.db.models import Q

from . import property_specs
from .models import Property, PropertyFAQ, PropertyListingName

SIZE_LABELS = (('bedroom_count', 'bedrooms'), ('bed_count', 'beds'), ('bathroom_count', 'bathrooms'), ('square_footage', 'square feet'))
ACCESS_FIELDS = (
    ('gate_code', 'Gate code'), ('door_code', 'Door code'), ('lockbox_code', 'Lockbox code'), ('alarm_code', 'Alarm code'),
    ('wifi_password', 'Wifi password'),
)


def _clock(t):
    if t is None:
        return None
    return {'time': t.strftime('%H:%M'), 'display': f'{t.hour % 12 or 12}:{t:%M} {"AM" if t.hour < 12 else "PM"}'}


def _num(value):
    if value is None:
        return None
    return float(value) if value != int(value) else int(value)


def _size(obj, fallback=None):
    out = {}
    for field, label in SIZE_LABELS:
        value = getattr(obj, field)
        if value is None and fallback is not None:
            value = getattr(fallback, field)
        out[label.replace(' ', '_')] = _num(value)
    return out


def faq_entry(entry):
    return {
        'id': entry.pk, 'question': entry.question, 'answer': entry.answer,
        'unit': {'id': entry.unit_id, 'label': entry.unit.label} if entry.unit_id else None,
        'applies_to': 'unit' if entry.unit_id else 'property',
        'origin': entry.origin, 'basis': entry.basis, 'reviewed': entry.reviewed, 'locked': entry.locked_against_bot(),
        'source_note': entry.source_note, 'times_used': entry.times_used,
        'updated_at': entry.updated_at.isoformat(),
    }


def faq_for(prop, unit=None):
    """The active FAQ. With a unit: the whole property's answers plus that unit's
    (another unit's answers are left out). Without: everything, each entry saying
    what it applies to."""
    qs = prop.faqs.filter(status=PropertyFAQ.Status.ACTIVE).select_related('unit')
    if unit is not None:
        qs = qs.filter(Q(unit__isnull=True) | Q(unit=unit))
    return qs.order_by('question')


def build_profile(prop, access=False, internal=False, unit=None):
    """The property's profile. `unit` narrows it to what is true for one unit of a
    multi-unit property (its own size, code and answers, plus everything about the
    building); without it the profile covers the whole property and says so when
    there are several units to choose from (`needs_unit`)."""
    units = [u for u in prop.units.all() if u.is_active]
    profile = {
        'scope': {'unit': {'id': unit.pk, 'label': unit.label} if unit is not None else None},
        'needs_unit': unit is None and len(units) > 1,
        'id': prop.pk, 'name': prop.name, 'type': prop.property_type, 'type_label': prop.get_property_type_display(),
        'active': prop.is_active, 'timezone': prop.timezone,
        'address': {'full': prop.address, 'street': prop.street, 'city': prop.city, 'state': prop.state, 'zip': prop.zip_code},
        'check_in': _clock(prop.default_check_in_time), 'check_out': _clock(prop.default_check_out_time),
        'size': _size(prop),
        'units': [
            {'id': u.pk, 'label': u.label, 'size': _size(u, fallback=prop), 'size_is_own': any(getattr(u, f) is not None for f, _ in SIZE_LABELS),
             'wifi_network': u.wifi_network or None}
            for u in units
        ],
        'unit': None if unit is None else {
            'id': unit.pk, 'label': unit.label, 'size': _size(unit, fallback=prop),
            'size_is_own': any(getattr(unit, f) is not None for f, _ in SIZE_LABELS),
            'listing_titles': [{'platform': ln.platform, 'title': ln.name} for ln in prop.listing_names.select_related('unit') if ln.unit_id == unit.pk],
        },
        'amenities': [
            {'label': a.attribute.label, 'category': a.attribute.category, 'note': a.note}
            for a in prop.attribute_assignments.select_related('attribute') if a.attribute.is_active
        ],
        'system_locations': [
            {'system': s.system_name, 'location': s.location, 'notes': s.notes} for s in prop.system_locations.all()
        ],
        'wifi_network': (unit.wifi_network if unit is not None and unit.wifi_network else prop.wifi_network) or None,
        'listing_titles': [
            {'platform': ln.platform, 'title': ln.name, 'unit': ln.unit.label if ln.unit_id else None}
            for ln in prop.listing_names.select_related('unit')
        ],
    }

    code_units = [unit] if unit is not None else units
    present = [label for field, label in ACCESS_FIELDS if getattr(prop, field)] + (['Unit codes'] if any(u.access_code for u in code_units) else [])
    if any(u.wifi_password for u in code_units):
        present.append('Unit wifi password')
    if any(u.access_notes for u in code_units):
        present.append('Unit access notes')
    if prop.access_notes:
        present.append('Access notes')
    if access:
        profile['access'] = {
            'restricted': False,
            'codes': {field: (unit.wifi_password if field == 'wifi_password' and unit is not None and unit.wifi_password else getattr(prop, field)) or None for field, _ in ACCESS_FIELDS},
            'unit_codes': {u.label: u.access_code for u in code_units if u.access_code},
            'unit_wifi_passwords': {u.label: u.wifi_password for u in code_units if u.wifi_password},
            'unit_access_notes': {u.label: u.access_notes for u in code_units if u.access_notes},
            'access_notes': prop.access_notes or None,
        }
    else:
        profile['access'] = {
            'restricted': True, 'on_file': present,
            'note': 'This key may not read access information. It exists (see on_file) but staff must supply it.',
        }

    if internal:
        profile['internal'] = {
            'restricted': False,
            'notes': prop.notes or None,
            'contacts': [
                {'name': c.name, 'role': c.get_contact_type_display(), 'trade': c.trade or None, 'phone': c.phone or None, 'email': c.email or None}
                for c in prop.contacts.all()
            ],
            'documents': [{'name': d.name, 'category': d.category or None} for d in prop.documents.all()],
        }
    else:
        profile['internal'] = {'restricted': True, 'note': 'This key may not read internal notes, contacts or documents.'}

    missing = property_specs.missing_specs(prop) if prop.property_type == Property.Type.SHORT_TERM_RENTAL else []
    gaps = [f'{m["label"] + ": " if m["label"] else ""}{", ".join(m["missing"])}' for m in missing]
    if not prop.default_check_in_time:
        gaps.append('check-in time')
    if not prop.default_check_out_time:
        gaps.append('check-out time')
    profile['facts_missing'] = gaps

    profile['faq'] = [faq_entry(e) for e in faq_for(prop, unit)]
    return profile


def render_text(profile):
    """The profile as a compact markdown document to paste into a prompt."""
    p, lines = profile, []
    lines.append(f'# {p["name"]}')
    lines.append(f'{p["type_label"]}. {p["address"]["full"] or "Address not recorded"}.')
    times = []
    if p['check_in']:
        times.append(f'check-in {p["check_in"]["display"]}')
    if p['check_out']:
        times.append(f'check-out {p["check_out"]["display"]}')
    if times:
        lines.append('Usual ' + ', '.join(times) + '.')

    def size_line(s):
        bits = [f'{s[k]} {label}' for k, label in (('bedrooms', 'bedroom(s)'), ('beds', 'bed(s)'), ('bathrooms', 'bathroom(s)'), ('square_feet', 'sq ft')) if s.get(k) is not None]
        return ', '.join(bits) if bits else 'size not recorded'

    lines.append('Size: ' + size_line(p['size']) + '.')
    if p['scope']['unit']:
        u = p['unit']
        lines.insert(1, f'**This is about the unit "{u["label"]}"** — its answers are below along with the building\'s. Say nothing about the other units.')
        lines.append(f'Unit "{u["label"]}": {size_line(u["size"])}.')
    elif p['needs_unit']:
        lines.append('\n## Units — which one?')
        lines.append('This building has several units and the guest is in ONE of them. Work out which (from the listing title, the booking, or ask) '
                     'before answering anything that differs between units — appliances, layout, beds, the unit\'s own door. Then read the profile again with unit_id. '
                     'Never give one unit\'s code to a guest in another.')
        lines += [f'- {u["label"]} (unit_id {u["id"]}): {size_line(u["size"])}' for u in p['units']]
    elif p['units']:
        lines.append('\n## Units')
        lines += [f'- {u["label"]}: {size_line(u["size"])}' for u in p['units']]
    if p['amenities']:
        lines.append('\n## Features and amenities')
        lines += [f'- {a["label"]}' + (f' ({a["note"]})' if a['note'] else '') for a in p['amenities']]
    if p['system_locations']:
        lines.append('\n## Where things are')
        lines += [f'- {s["system"]}: {s["location"]}' + (f' — {s["notes"]}' if s['notes'] else '') for s in p['system_locations']]
    if p['wifi_network']:
        lines.append(f'\nWifi network: {p["wifi_network"]}')
    if not p['scope']['unit']:
        lines += [f'Wifi network in {u["label"]}: {u["wifi_network"]}' for u in p['units'] if u.get('wifi_network')]
    access = p['access']
    if access['restricted']:
        if access['on_file']:
            lines.append('\n## Access\nOn file but not available to you: ' + ', '.join(access['on_file']) + '. Do not guess; say staff will send it.')
    else:
        lines.append('\n## Access')
        for field, label in ACCESS_FIELDS:
            if access['codes'].get(field):
                lines.append(f'- {label}: {access["codes"][field]}')
        lines += [f'- {label} (unit) code: {code}' for label, code in access['unit_codes'].items()]
        if access.get('unit_wifi_passwords') and not p['scope']['unit']:
            lines += [f'- Wifi password for {label}: {pw}' for label, pw in access['unit_wifi_passwords'].items()]
        lines += [f'- How to get into {label}: {note}' for label, note in access.get('unit_access_notes', {}).items()]
        if access['access_notes']:
            lines.append(f'- Notes: {access["access_notes"]}')
    internal = p['internal']
    if not internal['restricted']:
        if internal['notes']:
            lines.append('\n## Internal notes\n' + internal['notes'])
        if internal['contacts']:
            lines.append('\n## Contacts')
            lines += [f'- {c["name"]} ({c["role"]}{", " + c["trade"] if c["trade"] else ""})' + (f' {c["phone"]}' if c['phone'] else '') for c in internal['contacts']]
    if p['faq']:
        lines.append('\n## FAQ (answers already given for this property)')
        lines.append('An answer marked with a unit is true of that unit only; if a unit\'s answer and the building\'s disagree, the unit\'s wins.')
        for e in p['faq']:
            scope = f' [{e["unit"]["label"]} only]' if e['unit'] else ''
            trust = '' if e['reviewed'] else ' (unreviewed)'
            lines.append(f'Q{scope}: {e["question"]}\nA{trust}: {e["answer"]}')
    if p['facts_missing']:
        lines.append('\n## Not recorded (do not guess)\n' + '; '.join(p['facts_missing']))
    return '\n'.join(lines) + '\n'
