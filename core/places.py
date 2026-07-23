"""Google Places API (New) — live address-autocomplete suggestions for the
property form's address picker. Server-side only: the API key never
reaches the browser, and this is this app's only client of the Places
API (Autocomplete + Place Details, used together — Autocomplete alone
doesn't return structured street/city/state/zip components, only Place
Details does). See core/usps.py for the separate standardize-on-save
step; these two together satisfy "search as you type, verified on save."

Same shape as core/google_calendar.py: an is_configured() guard, and any
failure is caught, logged, and turned into an empty/None result rather
than raised — a broken or unconfigured integration should degrade to the
plain manual-entry form, not break the property page.
"""
import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

AUTOCOMPLETE_URL = 'https://places.googleapis.com/v1/places:autocomplete'
DETAILS_URL = 'https://places.googleapis.com/v1/places/{place_id}'

# Google's addressComponents "types" -> the Property field each maps to.
_STREET_NUMBER = 'street_number'
_ROUTE = 'route'
_CITY = 'locality'
_STATE = 'administrative_area_level_1'
_ZIP = 'postal_code'


def is_configured():
    return bool(settings.GOOGLE_PLACES_API_KEY)


def autocomplete(query):
    """[{'place_id', 'text'}, ...] (top ~8), or [] if unconfigured, blank
    query, or the request fails for any reason."""
    query = (query or '').strip()
    if not is_configured() or not query:
        return []
    try:
        resp = requests.post(
            AUTOCOMPLETE_URL,
            json={'input': query, 'includedRegionCodes': ['us']},
            headers={
                'X-Goog-Api-Key': settings.GOOGLE_PLACES_API_KEY,
                'Content-Type': 'application/json',
            },
            timeout=5,
        )
        resp.raise_for_status()
        suggestions = resp.json().get('suggestions', [])
        return [
            {'place_id': s['placePrediction']['placeId'], 'text': s['placePrediction']['text']['text']}
            for s in suggestions if 'placePrediction' in s
        ][:8]
    except Exception:
        logger.exception('Google Places autocomplete failed for %r', query)
        return []


def place_details(place_id):
    """{'street', 'city', 'state', 'zip_code'}, or None if unconfigured or
    the request fails / the place has no usable street address."""
    if not is_configured():
        return None
    try:
        resp = requests.get(
            DETAILS_URL.format(place_id=place_id),
            headers={
                'X-Goog-Api-Key': settings.GOOGLE_PLACES_API_KEY,
                'X-Goog-FieldMask': 'addressComponents',
            },
            timeout=5,
        )
        resp.raise_for_status()
        by_type = {}
        for component in resp.json().get('addressComponents', []):
            for t in component.get('types', []):
                by_type[t] = component
        street = ' '.join(filter(None, [
            (by_type.get(_STREET_NUMBER) or {}).get('longText'),
            (by_type.get(_ROUTE) or {}).get('longText'),
        ])).strip()
        return {
            'street': street,
            'city': (by_type.get(_CITY) or {}).get('longText', ''),
            'state': (by_type.get(_STATE) or {}).get('shortText', ''),
            'zip_code': (by_type.get(_ZIP) or {}).get('longText', ''),
        }
    except Exception:
        logger.exception('Google Places details lookup failed for %s', place_id)
        return None
