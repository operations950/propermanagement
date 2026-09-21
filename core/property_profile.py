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
        'origin': entry.origin, 'basis': entry.basis, 'reviewed': entry.reviewed, 'locked': entry.locked_against_bot(),
        'source_note': entry.source_note, 'times_used': entry.times_used,
        'updated_at': entry.updated_at.isoformat(),
    }


def build_profile(prop, access=False, internal=False):
    units = [u for u in prop.units.all() if u.is_active]
    profile = {
        'id': prop.pk, 'name': prop.name, 'type': prop.property_type, 'type_label': prop.get_property_type_display(),
        'active': prop.is_active, 'timezone': prop.timezone,
        'address': {'full': prop.address, 'street': prop.street, 'city': prop.city, 'state': prop.state, 'zip': prop.zip_code},
        'check_in': _clock(prop.default_check_in_time), 'check_out': _clock(prop.default_check_out_time),
        'size': _size(prop),
        'units': [
            {'id': u.pk, 'label': u.label, 'size': _size(u, fallback=prop), 'size_is_own': any(getattr(u, f) is not None for f, _ in SIZE_LABELS)}
            for u in units
        ],
        'amenities': [
            {'label': a.attribute.label, 'category': a.attribute.category, 'note': a.note}
            for a in prop.attribute_assignments.select_related('attribute') if a.attribute.is_active
        ],
        'system_locations': [
            {'system': s.system_name, 'location': s.location, 'notes': s.notes} for s in prop.system_locations.all()
        ],
        'wifi_network': prop.wifi_network or None,
        'listing_titles': [
            {'platform': ln.platform, 'title': ln.name, 'unit': ln.unit.label if ln.unit_id else None}
            for ln in prop.listing_names.select_related('unit')
        ],
    }

    present = [label for field, label in ACCESS_FIELDS if getattr(prop, field)] + (['Unit codes'] if any(u.access_code for u in units) else [])
    if prop.access_notes:
        present.append('Access notes')
    if access:
        profile['access'] = {
            'restricted': False,
            'codes': {field: getattr(prop, field) or None for field, _ in ACCESS_FIELDS},
            'unit_codes': {u.label: u.access_code for u in units if u.access_code},
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

    profile['faq'] = [
        faq_entry(e) for e in prop.faqs.filter(status=PropertyFAQ.Status.ACTIVE).select_related('unit').order_by('question')
    ]
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
    if p['units']:
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
        for e in p['faq']:
            scope = f' [{e["unit"]["label"]} only]' if e['unit'] else ''
            trust = '' if e['reviewed'] else ' (unreviewed)'
            lines.append(f'Q{scope}: {e["question"]}\nA{trust}: {e["answer"]}')
    if p['facts_missing']:
        lines.append('\n## Not recorded (do not guess)\n' + '; '.join(p['facts_missing']))
    return '\n'.join(lines) + '\n'
