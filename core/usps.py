"""USPS Addresses API v3.2.3 — standardizes/verifies a property's address
on save (endpoint shape confirmed against github.com/USPS/api-examples;
the newer 3.3.1 API is postponed until August 2026, so this deliberately
targets the currently-available 3.2.3 surface).

OAuth2 client-credentials is service-level, not per-user, so a single
shared token is cached in memory here rather than in a DB-backed token
model like GoogleCalendarToken — appropriate at this app's single-dyno
scale, and simpler.

Never raises and never blocks a property save: any failure (unconfigured,
network error, USPS couldn't match the address) returns verified=False so
the caller saves the address as submitted rather than refusing to save —
an address genuinely unknown to USPS (new construction, etc.) shouldn't
make the tool unusable.
"""
import logging
from datetime import timedelta

import requests
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

TOKEN_URL = 'https://apis.usps.com/oauth2/v3/token'
ADDRESS_URL = 'https://apis.usps.com/addresses/v3/address'

_token_cache = {'token': None, 'expires_at': None}


def is_configured():
    return bool(settings.USPS_CLIENT_ID and settings.USPS_CLIENT_SECRET)


def _get_token():
    now = timezone.now()
    if _token_cache['token'] and _token_cache['expires_at'] and _token_cache['expires_at'] > now:
        return _token_cache['token']
    resp = requests.post(
        TOKEN_URL,
        json={
            'client_id': settings.USPS_CLIENT_ID,
            'client_secret': settings.USPS_CLIENT_SECRET,
            'grant_type': 'client_credentials',
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    _token_cache['token'] = data['access_token']
    _token_cache['expires_at'] = now + timedelta(seconds=data.get('expires_in', 3600) - 60)
    return _token_cache['token']


def standardize(street, city, state, zip_code):
    """{'verified': True, 'street', 'city', 'state', 'zip_code'} on a
    confirmed match (USPS's own standardized casing/abbreviations, plus a
    real ZIP+4 when available), or {'verified': False, 'error': <str>}
    otherwise — never raises."""
    if not is_configured():
        return {'verified': False, 'error': 'USPS is not configured yet.'}
    try:
        token = _get_token()
        resp = requests.get(
            ADDRESS_URL,
            params={'streetAddress': street, 'city': city, 'state': state, 'ZIPCode': zip_code},
            headers={'Authorization': f'Bearer {token}'},
            timeout=10,
        )
        if resp.status_code != 200:
            return {'verified': False, 'error': f"USPS couldn't verify this address (status {resp.status_code})."}
        data = resp.json().get('address', {})
        zip_full = data.get('ZIPCode', zip_code)
        if data.get('ZIPPlus4'):
            zip_full = f"{data.get('ZIPCode', zip_code)}-{data['ZIPPlus4']}"
        return {
            'verified': True,
            'street': data.get('streetAddress', street),
            'city': data.get('city', city),
            'state': data.get('state', state),
            'zip_code': zip_full,
        }
    except Exception as exc:
        logger.exception('USPS address standardization failed for %r, %r, %r, %r', street, city, state, zip_code)
        return {'verified': False, 'error': str(exc)[:200]}
