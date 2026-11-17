"""The documents every property must have on file: a management agreement for ALL properties, and a lease for every commercial and long-term rental (each lease in use).

A document counts when it is kept in any of the places a person might put it: the accounting documents (kind management contract / lease), a commercial lease's own documents, or the property's
general Documents list under a name or category that says what it is. Nothing here changes anything; it only says what is missing.
"""
from .models import Property

MANAGEMENT = 'contract'
LEASE = 'lease'
LABELS = {MANAGEMENT: 'Management agreement', LEASE: 'Lease'}


def _general(prop, words):
    return any(any(w in f'{d.name} {d.category}'.lower() for w in words) for d in prop.documents.all())


def _assoc_kinds(prop):
    """The kinds of accounting document on file, counting the parts of a sorted binder."""
    kinds = set()
    for d in (x for x in prop.assoc_documents.all() if not x.is_staging):
        if d.auto_sort:
            if d.sort_status == 'confirmed':
                kinds |= {s.get('kind') for s in d.sections or [] if not s.get('repeat_of')}
        else:
            kinds.add(d.kind)
    return kinds


def required_for(prop):
    """[{'key', 'label', 'have': bool, 'detail'}] for one property."""
    if not prop.is_active or prop.is_general or prop.property_type == Property.Type.GENERAL:
        return []
    kinds = _assoc_kinds(prop)
    items = [{'key': MANAGEMENT, 'label': LABELS[MANAGEMENT], 'have': MANAGEMENT in kinds or _general(prop, ('management agreement', 'management contract', 'management')), 'detail': ''}]
    if prop.property_type == Property.Type.COMMERCIAL:
        leases = [l for l in prop.leases.exclude(status='ended').select_related('tenant', 'unit').prefetch_related('documents')]
        if not leases:
            items.append({'key': LEASE, 'label': 'Lease', 'have': LEASE in kinds or _general(prop, ('lease',)), 'detail': 'No lease is set up yet.' if LEASE not in kinds else ''})
        for l in leases:
            first = (l.tenant.name.lower().split() or [''])[0]
            have = any(d.kind == 'lease' for d in l.documents.all()) or any(d.kind == 'lease' and first in d.name.lower() for d in prop.assoc_documents.all())                 or any(first in f'{d.name} {d.category}'.lower() and 'lease' in f'{d.name} {d.category}'.lower() for d in prop.documents.all())
            items.append({'key': LEASE, 'label': f'Lease: {l.tenant.name} ({l.unit.label})', 'have': have, 'detail': '', 'lease': l})
    elif prop.property_type == Property.Type.LONG_TERM_RENTAL:
        leases = [l for l in prop.ltr_leases.exclude(status='ended').select_related('tenant')]
        have = LEASE in kinds or _general(prop, ('lease',))
        if not leases:
            items.append({'key': LEASE, 'label': 'Lease', 'have': have, 'detail': '' if have else 'No tenant lease is set up yet.'})
        else:
            for l in leases:
                items.append({'key': LEASE, 'label': f'Lease: {l.tenant.name}', 'have': have, 'detail': ''})
    return items


def missing_for(prop):
    return [i for i in required_for(prop) if not i['have']]


def all_missing():
    """[(property, [missing items])] for every active property that lacks a required document, by name."""
    out = []
    for prop in Property.objects.filter(is_active=True, is_general=False).prefetch_related('documents', 'assoc_documents').order_by('name'):
        miss = missing_for(prop)
        if miss:
            out.append((prop, miss))
    return out
