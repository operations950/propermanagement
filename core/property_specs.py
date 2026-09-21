"""Which short-term rentals are missing the size details that the on-site visit
estimates depend on.

A cleaning's estimated time (and so its calculated price) is built from checklist
items that scale by bedrooms, beds, bathrooms or square footage. A count that was
never filled in counts as zero, so a rental without them gets a suspiciously low
estimate. Nothing forces anyone to fill them in — this only says which rentals
still need it, so the Properties screen can remind people.

A rentable listing is the property itself, or each active unit of a building. A
unit's own figure wins; where it has none, the property's is used (the same
fallback the estimates use), so a building whose units inherit the building's
numbers is complete."""
from .models import Property

SPEC_FIELDS = (
    ('bedroom_count', 'bedrooms'),
    ('bed_count', 'beds'),
    ('bathroom_count', 'bathrooms'),
    ('square_footage', 'square footage'),
)


def applies_to(prop):
    return prop.property_type == Property.Type.SHORT_TERM_RENTAL and prop.is_active and not prop.is_general


def _missing(unit, prop):
    names = []
    for field, label in SPEC_FIELDS:
        value = getattr(unit, field) if unit is not None and getattr(unit, field) is not None else getattr(prop, field)
        if value is None:
            names.append(label)
    return names


def missing_specs(prop):
    """[{'label': unit label ('' for the property itself), 'missing': [names]}]
    for each listing of this rental that lacks something; [] when it is complete
    or the property isn't a rental the estimates apply to."""
    if not applies_to(prop):
        return []
    units = [u for u in prop.units.all() if u.is_active]
    listings = [(u.label, _missing(u, prop)) for u in units] if units else [('', _missing(None, prop))]
    return [{'label': label, 'missing': names} for label, names in listings if names]


def summary(prop_missing):
    """One line for a tooltip or notice: 'Missing bedrooms, beds' or, for a
    building, 'Unit 2: bedrooms, beds; Unit 3: square footage'."""
    if not prop_missing:
        return ''
    if len(prop_missing) == 1 and not prop_missing[0]['label']:
        return 'Missing ' + ', '.join(prop_missing[0]['missing'])
    return '; '.join(f'{m["label"] or "Property"}: ' + ', '.join(m['missing']) for m in prop_missing)
