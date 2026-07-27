"""QuickBooks Online OAuth2 + the one report the Company Financials box
needs (YTD Profit & Loss). Same shape as core/google_calendar.py: an
is_configured() guard, and any failure is caught, logged, and turned into
None rather than raised — an unconfigured or broken integration should
degrade to a "Connect QuickBooks" prompt / stale cached snapshot, not
break the Owner Dashboard.

Unlike Google Calendar (one token per staff member), there is exactly one
QuickBooksToken row — the whole company connects once, not per-user. And
unlike Google, QuickBooks refresh tokens expire after ~100 days, so
periodic reconnection via /admin-tools/ is expected, not a bug.
"""
import logging
from datetime import date

import requests
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger(__name__)

AUTHORIZE_URL = 'https://appcenter.intuit.com/connect/oauth2'
TOKEN_URL = 'https://oauth2.platform.intuit.com/oauth2/v1/tokens/bearer'
API_BASE = 'https://quickbooks.api.intuit.com/v3/company'
SCOPE = 'com.intuit.quickbooks.accounting'


def is_configured():
    return bool(settings.QUICKBOOKS_CLIENT_ID and settings.QUICKBOOKS_CLIENT_SECRET)


def redirect_uri_for_display(request):
    """The exact value that must be registered as a Redirect URI on the
    Intuit Developer app — shown on the connect screen since a mismatch
    here is the #1 way this flow fails (mirrors google_calendar.py's
    identically-purposed helper)."""
    return request.build_absolute_uri(reverse('quickbooks_callback'))


def authorize_url(request, state):
    params = {
        'client_id': settings.QUICKBOOKS_CLIENT_ID,
        'response_type': 'code',
        'scope': SCOPE,
        'redirect_uri': redirect_uri_for_display(request),
        'state': state,
    }
    query = '&'.join(f'{k}={requests.utils.quote(str(v))}' for k, v in params.items())
    return f'{AUTHORIZE_URL}?{query}'


def exchange_code(request, code):
    """POST the authorization code for an access/refresh token pair.
    Returns the parsed token response dict, or None on any failure."""
    try:
        resp = requests.post(
            TOKEN_URL,
            data={'grant_type': 'authorization_code', 'code': code, 'redirect_uri': redirect_uri_for_display(request)},
            auth=(settings.QUICKBOOKS_CLIENT_ID, settings.QUICKBOOKS_CLIENT_SECRET),
            headers={'Accept': 'application/json'},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        logger.exception('QuickBooks token exchange failed')
        return None


def _refresh_if_needed(token):
    needs_refresh = not token.access_token or not token.access_token_expires_at or token.access_token_expires_at <= timezone.now()
    if not needs_refresh:
        return True
    try:
        resp = requests.post(
            TOKEN_URL,
            data={'grant_type': 'refresh_token', 'refresh_token': token.refresh_token},
            auth=(settings.QUICKBOOKS_CLIENT_ID, settings.QUICKBOOKS_CLIENT_SECRET),
            headers={'Accept': 'application/json'},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        token.access_token = data['access_token']
        token.refresh_token = data.get('refresh_token', token.refresh_token)
        token.access_token_expires_at = timezone.now() + timezone.timedelta(seconds=data.get('expires_in', 3600))
        if data.get('x_refresh_token_expires_in'):
            token.refresh_token_expires_at = timezone.now() + timezone.timedelta(seconds=data['x_refresh_token_expires_in'])
        token.save(update_fields=['access_token', 'refresh_token', 'access_token_expires_at', 'refresh_token_expires_at'])
        return True
    except Exception:
        logger.exception('QuickBooks token refresh failed')
        return False


def _find_report_total(rows, group_name):
    """QuickBooks's ProfitAndLoss report JSON nests Rows recursively, each
    optionally tagged with a 'group' (Income/COGS/Expenses/NetIncome/...).
    Walks the tree for the first Summary row matching group_name and
    returns its last (rightmost) ColData value as a float."""
    for row in rows or []:
        if row.get('group') == group_name:
            summary = row.get('Summary', {}).get('ColData', [])
            if summary and summary[-1].get('value'):
                try:
                    return float(summary[-1]['value'])
                except ValueError:
                    return None
        nested = row.get('Rows', {}).get('Row', [])
        found = _find_report_total(nested, group_name)
        if found is not None:
            return found
    return None


def fetch_profit_and_loss(token):
    """{'revenue', 'expenses', 'net_income'} (year-to-date), or None if
    unconfigured, not connected, or the request/refresh/parse fails."""
    if not is_configured() or not _refresh_if_needed(token):
        return None
    try:
        today = timezone.localdate()
        resp = requests.get(
            f'{API_BASE}/{token.realm_id}/reports/ProfitAndLoss',
            params={'start_date': date(today.year, 1, 1).isoformat(), 'end_date': today.isoformat()},
            headers={'Authorization': f'Bearer {token.access_token}', 'Accept': 'application/json'},
            timeout=15,
        )
        resp.raise_for_status()
        rows = resp.json().get('Rows', {}).get('Row', [])
        revenue = _find_report_total(rows, 'Income') or 0
        expenses = _find_report_total(rows, 'Expenses') or 0
        net_income = _find_report_total(rows, 'NetIncome')
        if net_income is None:
            net_income = revenue - expenses
        return {'revenue': revenue, 'expenses': expenses, 'net_income': net_income}
    except Exception:
        logger.exception('QuickBooks Profit & Loss fetch failed')
        return None
