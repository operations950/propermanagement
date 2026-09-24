"""The management company's details that go on every document (letterhead, signature block, contact line).
Override any of them with an environment variable of the same name, upper-cased, prefixed LEGALFORMS_
(e.g. LEGALFORMS_COMPANY_PHONE)."""
import os

_DEFAULTS = {
    'company_name': 'Proper Realty Co.',
    'company_address1': '1045 E Atlantic Ave, Ste 309',
    'company_city': 'Delray Beach, FL 33483',
    'company_phone': '561-599-6300',
    'company_phone_display': '(561) 599-6300',
    'company_email': 'admin@proper-realty.com',
    'default_county': 'Palm Beach',
    'default_manager_title': 'Property Manager',
}


def company():
    return {k: os.environ.get('LEGALFORMS_' + k.upper(), v) for k, v in _DEFAULTS.items()}
