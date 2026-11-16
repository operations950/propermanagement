"""The words a screen uses for a property's books. An association has owners who pay assessments to a board; a commercial building has tenants who pay rent to a landlord
(the property's owner or owners). The books and the screens are the same code: only these words change.

    {% load acc_extras %}  {{ assoc|lab:"holders" }}   -> 'Owners' for an association, 'Tenants' for a commercial property
"""
from core.models import Property

# the property types whose books we can keep (an association, a commercial building)
BOOKS_TYPES = (Property.Type.ASSOCIATION, Property.Type.COMMERCIAL)

ASSOCIATION = {
    'holder': 'Owner', 'holders': 'Owners', 'holder_lower': 'owner', 'holders_lower': 'owners',
    'charge': 'Assessment', 'charges': 'Assessments', 'charge_lower': 'assessment',
    'units_title': 'Units & owners', 'charges_title': 'Charges to owners', 'payment': 'Owner payment', 'payments': 'Owner payments', 'aging': 'Owner aging',
    'statement': 'Owner statement', 'entity': 'association', 'entity_title': 'Association', 'owner_group': 'board', 'owner_group_title': 'Board',
    'package_to': 'the board', 'package_gets': 'the board gets', 'package_person': 'board member', 'package_people': 'board members', 'package_step': 'Send financials to the board', 'close_steps': 'Close associations',
    'charges_step': 'Owner charges posted',
}
COMMERCIAL = {
    'holder': 'Tenant', 'holders': 'Tenants', 'holder_lower': 'tenant', 'holders_lower': 'tenants',
    'charge': 'Rent', 'charges': 'Rent', 'charge_lower': 'rent',
    'units_title': 'Units & tenants', 'charges_title': 'Charges to tenants', 'payment': 'Tenant payment', 'payments': 'Tenant payments', 'aging': 'Tenant aging',
    'statement': 'Tenant statement', 'entity': 'property', 'entity_title': 'Property', 'owner_group': 'owners', 'owner_group_title': 'Owners',
    'package_to': 'the owners', 'package_gets': 'the owners get', 'package_person': 'owner', 'package_people': 'owners', 'package_step': 'Send financials to the owners', 'close_steps': 'Close properties',
    'charges_step': 'Rent and tenant charges posted',
}


def is_commercial(prop):
    return getattr(prop, 'property_type', None) == Property.Type.COMMERCIAL


def labels_for(prop):
    return COMMERCIAL if is_commercial(prop) else ASSOCIATION


def label(prop, key):
    return labels_for(prop).get(key, key)
