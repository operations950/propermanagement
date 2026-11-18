"""Filling each owner's LEGAL name from the Yardi owner list (accounting/yardi_sep30_data.py), so nothing that goes out carries a phone-book tag ("Ray Griffith GLEN", "Ally Amorim - SAG").

`plan()` only reads: for every current owner of a unit it finds the Yardi owner(s) of that unit whose name matches the contact (all of the Yardi name's words are in the contact's name, or the other
way round for a contact saved by first name only) and proposes the Yardi name. An owner with a legal name already typed is never touched. What cannot be matched is listed, not guessed:
the person has to type the name from the deed (Owners > the owner > "Name exactly as on the deed").
`apply(plan)` writes the proposed names. Run through `manage.py fill_owner_legal_names` (a dry run unless --apply)."""
import re

from django.db import transaction

from core.models import Property

from . import owners
from .models import Ownership, OwnerProfile
from .yardi_load import FIND
from .yardi_sep30_data import DATA

SKIP_WORDS = {'and', 'jr', 'sr', 'ii', 'iii', 'iv', 'the', 'trust', 'of'}


def _tokens(name):
    return {t for t in re.findall(r'[a-z]+', (name or '').lower()) if t not in SKIP_WORDS and len(t) > 1}


def _covered(small, big):
    """Every word of `small` is a word of `big`, or the start of one (a contact saved as 'Jeff' or 'Ken' for Jeffrey or Kenneth)."""
    return all(any(t == u or (len(t) >= 3 and u.startswith(t)) or (len(u) >= 3 and t.startswith(u)) for u in big) for t in small)


def _matches(yardi_name, person_name):
    y, p = _tokens(yardi_name), _tokens(owners.clean_name(person_name))
    return bool(y) and bool(p) and (_covered(y, p) or _covered(p, y))


def _yardi_units():
    """{(property id, unit label): [Yardi owner names]} for every association in the Yardi file that is in the system."""
    out = {}
    for key, cfg in DATA.items():
        prop = next((p for p in Property.objects.filter(property_type=Property.Type.ASSOCIATION).order_by('id') if re.search(FIND[key], p.name.lower().replace('#', ' '), re.I)), None)
        if prop is None:
            continue
        for unit in cfg['units']:
            names = [o['name'].strip() for o in unit['owners'] if o.get('name') and not o.get('placeholder')]
            out[(prop.pk, unit['label'])] = names
    return out


def plan():
    """{'set': [(person, property, unit label, legal name)], 'unmatched': [(property, unit label, contact name, [Yardi names])], 'no_yardi': [(property, unit label, contact name)],
    'already': n}. People are listed once even if they own several units (the first unit that matches decides)."""
    yardi = _yardi_units()
    out = {'set': [], 'unmatched': [], 'no_yardi': [], 'already': 0}
    seen = set()
    legal = owners._legal_name_map()
    for own in Ownership.objects.filter(end_date__isnull=True).select_related('person', 'unit__property').order_by('unit__property__name', 'unit__label', '-is_billed'):
        person, unit = own.person, own.unit
        if person.pk in seen:
            continue
        if legal.get(person.pk):
            out['already'] += 1
            seen.add(person.pk)
            continue
        names = yardi.get((unit.property_id, unit.label))
        if names is None:
            out['no_yardi'].append((unit.property.name, unit.label, person.name))
            continue
        hits = list(dict.fromkeys(n for n in names if _matches(n, person.name)))
        hits = [h for h in hits if not any(o != h and _tokens(h) < _tokens(o) for o in hits)]            # "Rydlun" beside "Darcy Rydlun" is the same person, once
        if hits and _covered(_tokens(owners.clean_name(person.name)), set().union(*(_tokens(h) for h in hits))):
            out['set'].append((person, unit.property.name, unit.label, ' and '.join(hits)))
            seen.add(person.pk)
        else:                          # nobody matches, or the contact names more people than Yardi does (a couple saved as one contact): never shortened, listed for a person to type
            out['unmatched'].append((unit.property.name, unit.label, person.name, names))
    return out


@transaction.atomic
def apply(proposed):
    n = 0
    for person, prop_name, unit_label, name in proposed['set']:
        profile, _ = OwnerProfile.objects.get_or_create(contact=person)
        if not profile.legal_name:
            profile.legal_name = name
            profile.save(update_fields=['legal_name'])
            n += 1
    return n
