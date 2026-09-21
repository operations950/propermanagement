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
import time
from datetime import date
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger(__name__)

# Intuit publishes its OAuth endpoints in an OpenID discovery document and
# asks apps to read them from there rather than hardcode them, since they
# can change. The FALLBACK_* values below are what that document says today
# and are used only when it can't be fetched.
DISCOVERY_URLS = {
    'production': 'https://developer.api.intuit.com/.well-known/openid_configuration',
    'sandbox': 'https://developer.api.intuit.com/.well-known/openid_sandbox_configuration',
}
FALLBACK_AUTHORIZE_URL = 'https://appcenter.intuit.com/connect/oauth2'
FALLBACK_TOKEN_URL = 'https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer'
API_BASES = {
    'production': 'https://quickbooks.api.intuit.com/v3/company',
    'sandbox': 'https://sandbox-quickbooks.api.intuit.com/v3/company',
}
DISCOVERY_CACHE_SECONDS = 24 * 60 * 60
DISCOVERY_RETRY_SECONDS = 5 * 60
SCOPE = 'com.intuit.quickbooks.accounting'

_discovery_cache = {'expires': 0.0, 'environment': None, 'doc': None}


def is_configured():
    return bool(settings.QUICKBOOKS_CLIENT_ID and settings.QUICKBOOKS_CLIENT_SECRET)


def _environment():
    return settings.QUICKBOOKS_ENVIRONMENT if settings.QUICKBOOKS_ENVIRONMENT in DISCOVERY_URLS else 'production'


def _is_intuit_https(url):
    parsed = urlparse(url or '')
    return parsed.scheme == 'https' and (parsed.hostname or '').endswith('.intuit.com')


def _endpoints():
    """(authorize_url, token_url) from Intuit's discovery document, cached
    for a day. Any problem fetching or parsing it falls back to the
    FALLBACK_* constants (retried in a few minutes), so a discovery outage
    can never take the connection down. A URL that isn't https on an
    *.intuit.com host is ignored — the tokens and client secret get sent
    to whatever this returns."""
    env = _environment()
    now = time.time()
    if _discovery_cache['environment'] != env or now >= _discovery_cache['expires']:
        doc, ttl = {}, DISCOVERY_RETRY_SECONDS
        try:
            resp = requests.get(DISCOVERY_URLS[env], headers={'Accept': 'application/json'}, timeout=5)
            resp.raise_for_status()
            doc, ttl = resp.json(), DISCOVERY_CACHE_SECONDS
        except Exception:
            logger.warning('QuickBooks discovery document unavailable — using built-in endpoints', exc_info=True)
        _discovery_cache.update(expires=now + ttl, environment=env, doc=doc)
    doc = _discovery_cache['doc'] or {}
    authorize = doc.get('authorization_endpoint')
    token = doc.get('token_endpoint')
    return (
        authorize if _is_intuit_https(authorize) else FALLBACK_AUTHORIZE_URL,
        token if _is_intuit_https(token) else FALLBACK_TOKEN_URL,
    )


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
    return f'{_endpoints()[0]}?{query}'


def exchange_code(request, code):
    """POST the authorization code for an access/refresh token pair.
    Returns the parsed token response dict, or None on any failure."""
    try:
        resp = requests.post(
            _endpoints()[1],
            data={'grant_type': 'authorization_code', 'code': code, 'redirect_uri': redirect_uri_for_display(request)},
            auth=(settings.QUICKBOOKS_CLIENT_ID, settings.QUICKBOOKS_CLIENT_SECRET),
            headers={'Accept': 'application/json'},
            timeout=10,
        )
        resp.raise_for_status()
        logger.info('QuickBooks token exchange ok (intuit_tid=%s)', _intuit_tid(resp))
        return resp.json()
    except Exception as exc:
        logger.error('QuickBooks token exchange failed: %s', _failure_summary(exc))
        return None


# Outcomes of _refresh_if_needed. REJECTED means Intuit itself answered 400/
# 401 (invalid_grant: the refresh token expired, was revoked, or was
# replaced) — only a human reconnecting fixes that. FAILED is everything
# else (timeout, Intuit outage): worth retrying, and not worth telling
# anyone to reconnect.
REFRESH_OK, REFRESH_REJECTED, REFRESH_FAILED = 'ok', 'rejected', 'failed'


def _status_code(exc):
    return getattr(getattr(exc, 'response', None), 'status_code', None)


def _intuit_tid(response):
    """The intuit_tid response header — Intuit's per-request trace ID, the
    first thing their support asks for when troubleshooting a call."""
    tid = (getattr(response, 'headers', None) or {}).get('intuit_tid', '')
    return tid if isinstance(tid, str) else ''


def _error_code(response):
    """Intuit's own short error code from a failed response body (the
    OAuth 'error' string, e.g. invalid_grant, or the first QuickBooks Fault
    code) — nothing else from the body, which can carry company data."""
    try:
        body = response.json()
        code = body.get('error') or body['Fault']['Error'][0].get('code')
        return code[:60] if isinstance(code, str) else ''
    except Exception:
        return ''


def _failure_summary(exc):
    """A log-safe one-liner for a failed QuickBooks request: exception
    type, HTTP status, intuit_tid and Intuit's error code. Deliberately
    NOT str(exc)/a traceback: requests puts the full request URL in that
    message, and the report URL contains the company ID (realmId), which
    is stored encrypted and must not end up readable in the logs. Intuit's
    requirements also forbid logging credentials or QuickBooks data."""
    response = getattr(exc, 'response', None)
    parts = [type(exc).__name__]
    if _status_code(exc):
        parts.append(f'HTTP {_status_code(exc)}')
    if _intuit_tid(response):
        parts.append(f'intuit_tid={_intuit_tid(response)}')
    if _error_code(response):
        parts.append(f'error={_error_code(response)}')
    return ' '.join(parts)


def _refresh_if_needed(token, force=False):
    expired = not token.access_token or not token.access_token_expires_at or token.access_token_expires_at <= timezone.now()
    if not (expired or force):
        return REFRESH_OK
    try:
        resp = requests.post(
            _endpoints()[1],
            data={'grant_type': 'refresh_token', 'refresh_token': token.refresh_token},
            auth=(settings.QUICKBOOKS_CLIENT_ID, settings.QUICKBOOKS_CLIENT_SECRET),
            headers={'Accept': 'application/json'},
            timeout=10,
        )
        resp.raise_for_status()
        logger.info('QuickBooks token refresh ok (intuit_tid=%s)', _intuit_tid(resp))
        data = resp.json()
        token.access_token = data['access_token']
        token.refresh_token = data.get('refresh_token', token.refresh_token)
        token.access_token_expires_at = timezone.now() + timezone.timedelta(seconds=data.get('expires_in', 3600))
        if data.get('x_refresh_token_expires_in'):
            token.refresh_token_expires_at = timezone.now() + timezone.timedelta(seconds=data['x_refresh_token_expires_in'])
        token.save(update_fields=['access_token', 'refresh_token', 'access_token_expires_at', 'refresh_token_expires_at'])
        return REFRESH_OK
    except Exception as exc:
        logger.error('QuickBooks token refresh failed: %s', _failure_summary(exc))
        return REFRESH_REJECTED if _status_code(exc) in (400, 401) else REFRESH_FAILED


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


def _read_profit_and_loss(token):
    """One attempt at the YTD report — raises on any failure so the caller
    can tell an expired-token 401 from a transient error."""
    today = timezone.localdate()
    resp = requests.get(
        f'{API_BASES[_environment()]}/{token.realm_id}/reports/ProfitAndLoss',
        params={'start_date': date(today.year, 1, 1).isoformat(), 'end_date': today.isoformat()},
        headers={'Authorization': f'Bearer {token.access_token}', 'Accept': 'application/json'},
        timeout=15,
    )
    resp.raise_for_status()
    logger.info('QuickBooks Profit & Loss read ok (intuit_tid=%s)', _intuit_tid(resp))
    rows = resp.json().get('Rows', {}).get('Row', [])
    revenue = _find_report_total(rows, 'Income') or 0
    expenses = _find_report_total(rows, 'Expenses') or 0
    net_income = _find_report_total(rows, 'NetIncome')
    if net_income is None:
        net_income = revenue - expenses
    return {'revenue': revenue, 'expenses': expenses, 'net_income': net_income}


def fetch_profit_and_loss(token):
    """{'revenue', 'expenses', 'net_income'} (year-to-date), or None if
    unconfigured, not connected, or the request/refresh/parse fails."""
    if not is_configured() or _refresh_if_needed(token) != REFRESH_OK:
        return None
    try:
        return _read_profit_and_loss(token)
    except Exception as exc:
        logger.error('QuickBooks Profit & Loss fetch failed: %s', _failure_summary(exc))
        return None


RECONNECT_ERROR = 'QuickBooks rejected the saved connection — reconnect QuickBooks in Admin Tools.'
FETCH_ERROR = "Couldn't read the financials from QuickBooks — will retry on the next sync."
RETRY_DELAY_SECONDS = 2


def _refresh_with_retry(token):
    """_refresh_if_needed, retried once (after a short pause) when the
    failure looks transient. A REJECTED refresh is never retried — Intuit
    said no, asking again just burns a call."""
    outcome = _refresh_if_needed(token)
    if outcome == REFRESH_FAILED:
        time.sleep(RETRY_DELAY_SECONDS)
        outcome = _refresh_if_needed(token)
    return outcome


def _fetch_with_retry(token):
    """Returns (result, error). A failed read is retried once: a 401 means
    Intuit no longer honors the access token we think is still valid, so
    force a refresh first (and if THAT is rejected, it's a reconnect
    situation); anything else is treated as transient and retried after a
    short pause."""
    try:
        return _read_profit_and_loss(token), ''
    except Exception as exc:
        logger.warning('QuickBooks Profit & Loss fetch failed, retrying once: %s', _failure_summary(exc))
        if _status_code(exc) == 401:
            outcome = _refresh_if_needed(token, force=True)
            if outcome == REFRESH_REJECTED:
                return None, RECONNECT_ERROR
            if outcome != REFRESH_OK:
                return None, FETCH_ERROR
        else:
            time.sleep(RETRY_DELAY_SECONDS)
    try:
        return _read_profit_and_loss(token), ''
    except Exception as exc:
        logger.error('QuickBooks Profit & Loss fetch failed after retry: %s', _failure_summary(exc))
        return None, FETCH_ERROR


def sync_snapshot(token):
    """Refreshes the token if needed, pulls the YTD Profit & Loss, and
    stores it (or the reason it failed) on the QuickBooksToken row. Used by
    the scheduled job, the startup run, and the post-connect sync so all
    three behave identically. Failed steps are retried once (see the two
    helpers above). Returns True on success. A failure keeps the last good
    numbers and only records last_sync_error."""
    token.last_sync_attempt_at = timezone.now()
    result = None
    if not is_configured():
        error = 'QuickBooks client ID/secret are not set — add them in Admin Tools.'
    else:
        outcome = _refresh_with_retry(token)
        if outcome == REFRESH_REJECTED:
            error = RECONNECT_ERROR
        elif outcome == REFRESH_FAILED:
            error = FETCH_ERROR
        else:
            result, error = _fetch_with_retry(token)

    if result:
        token.ytd_revenue = result['revenue']
        token.ytd_expenses = result['expenses']
        token.ytd_net_income = result['net_income']
        token.last_synced_at = timezone.now()
    token.last_sync_error = error
    token.save(update_fields=[
        'ytd_revenue', 'ytd_expenses', 'ytd_net_income', 'last_synced_at',
        'last_sync_attempt_at', 'last_sync_error',
    ])
    return bool(result)


# --- chart of accounts -------------------------------------------------------------------
# Each property is tied to two QuickBooks accounts (see core/qb_accounts.py), so the
# chart of accounts is copied into QuickBooksAccount and refreshed here.

ACCOUNT_PAGE_SIZE = 1000
ACCOUNTS_ERROR = "Couldn't read the chart of accounts from QuickBooks — will retry on the next sync."

# QuickBooks normally supplies Classification; this covers an account that doesn't.
_TYPE_CLASSIFICATION = {
    'Bank': 'Asset', 'Accounts Receivable': 'Asset', 'Other Current Asset': 'Asset', 'Fixed Asset': 'Asset', 'Other Asset': 'Asset',
    'Accounts Payable': 'Liability', 'Credit Card': 'Liability', 'Other Current Liability': 'Liability', 'Long Term Liability': 'Liability',
    'Equity': 'Equity', 'Income': 'Revenue', 'Other Income': 'Revenue',
    'Expense': 'Expense', 'Other Expense': 'Expense', 'Cost of Goods Sold': 'Expense',
}


def _read_accounts(token):
    """One attempt at the whole chart of accounts (paged); raises on any failure."""
    accounts, start = [], 1
    while True:
        resp = requests.get(
            f'{API_BASES[_environment()]}/{token.realm_id}/query',
            params={'query': f'select * from Account startposition {start} maxresults {ACCOUNT_PAGE_SIZE}'},
            headers={'Authorization': f'Bearer {token.access_token}', 'Accept': 'application/json'},
            timeout=20,
        )
        resp.raise_for_status()
        logger.info('QuickBooks account list read ok (intuit_tid=%s)', _intuit_tid(resp))
        page = resp.json().get('QueryResponse', {}).get('Account', [])
        accounts.extend(page)
        if len(page) < ACCOUNT_PAGE_SIZE:
            return accounts
        start += ACCOUNT_PAGE_SIZE


def _read_accounts_with_retry(token):
    """Returns (accounts, error), retrying once like the financial sync does: a 401
    forces a token refresh first (rejected means reconnect), anything else is
    treated as transient."""
    try:
        return _read_accounts(token), ''
    except Exception as exc:
        logger.warning('QuickBooks account list failed, retrying once: %s', _failure_summary(exc))
        if _status_code(exc) == 401:
            outcome = _refresh_if_needed(token, force=True)
            if outcome == REFRESH_REJECTED:
                return None, RECONNECT_ERROR
            if outcome != REFRESH_OK:
                return None, ACCOUNTS_ERROR
        else:
            time.sleep(RETRY_DELAY_SECONDS)
    try:
        return _read_accounts(token), ''
    except Exception as exc:
        logger.error('QuickBooks account list failed after retry: %s', _failure_summary(exc))
        return None, ACCOUNTS_ERROR


def _account_fields(raw):
    account_type = raw.get('AccountType', '')
    return {
        'name': (raw.get('Name') or '')[:300],
        'fully_qualified_name': (raw.get('FullyQualifiedName') or raw.get('Name') or '')[:500],
        'account_type': account_type[:60],
        'account_sub_type': (raw.get('AccountSubType') or '')[:80],
        'classification': raw.get('Classification') or _TYPE_CLASSIFICATION.get(account_type, ''),
        'active': bool(raw.get('Active', True)),
    }


def sync_accounts(token):
    """Copies the QuickBooks chart of accounts into QuickBooksAccount. Accounts
    QuickBooks no longer returns are kept but marked inactive (so a mapping to one
    stays visible); an empty answer never deactivates anything. Returns
    (count, error); a failure keeps what is already stored."""
    from .models import QuickBooksAccount

    accounts, error = None, ''
    if not is_configured():
        error = 'QuickBooks client ID/secret are not set — add them in Admin Tools.'
    else:
        outcome = _refresh_with_retry(token)
        if outcome == REFRESH_REJECTED:
            error = RECONNECT_ERROR
        elif outcome == REFRESH_FAILED:
            error = ACCOUNTS_ERROR
        else:
            accounts, error = _read_accounts_with_retry(token)

    count = 0
    if accounts:
        now = timezone.now()
        seen = set()
        for raw in accounts:
            qb_id = str(raw.get('Id', '')).strip()
            if not qb_id:
                continue
            seen.add(qb_id)
            QuickBooksAccount.objects.update_or_create(qb_id=qb_id, defaults={**_account_fields(raw), 'synced_at': now})
            count += 1
        QuickBooksAccount.objects.exclude(qb_id__in=seen).filter(active=True).update(active=False)
        token.accounts_synced_at = now
    token.accounts_sync_error = error
    token.save(update_fields=['accounts_synced_at', 'accounts_sync_error'])
    return count, error


# --- transactions of one account (the month-end close) -------------------------------------
# A General Ledger report filtered to one account lists every transaction that hit it
# (one row per line), each row carrying QuickBooks's own transaction id. core/ledger.py
# groups those into one entry per transaction and keeps them in step with QuickBooks.

LEDGER_COLUMNS = 'tx_date,txn_type,doc_num,name,memo,split_acc,debt_amt,credit_amt,subt_nat_amount'
LEDGER_ERROR = "Couldn't read the transactions from QuickBooks — will retry on the next sync."

# Column keys QuickBooks reports, and the titles to fall back on if a key is missing.
_TITLE_KEYS = {
    'date': 'tx_date', 'transaction type': 'txn_type', 'num': 'doc_num', 'name': 'name', 'memo/description': 'memo', 'memo': 'memo',
    'split': 'split_acc', 'debit': 'debt_amt', 'credit': 'credit_amt', 'amount': 'subt_nat_amount', 'balance': 'rbal_nat_amount',
}


def _money(text):
    """A report cell as a Decimal (commas, a leading minus or accounting
    parentheses tolerated); blank is zero."""
    from decimal import Decimal, InvalidOperation
    raw = (text or '').replace(',', '').replace('$', '').strip()
    if not raw:
        return Decimal('0')
    negative = raw.startswith('(') and raw.endswith(')')
    try:
        value = Decimal(raw.strip('()'))
    except InvalidOperation:
        return Decimal('0')
    return -value if negative else value


def _column_keys(payload):
    keys = []
    for column in (payload.get('Columns') or {}).get('Column', []) or []:
        key = ''
        for meta in column.get('MetaData', []) or []:
            if meta.get('Name') == 'ColKey':
                key = meta.get('Value', '')
        keys.append(key or column.get('ColType') or _TITLE_KEYS.get((column.get('ColTitle') or '').strip().lower(), ''))
    return keys


def parse_general_ledger(payload):
    """The transactions listed by a General Ledger report, one dict per report row:
    txn_id (QuickBooks's own), txn_type, date (ISO string), doc_num, name, memo, split
    and debit / credit (or, when the report has no such columns, the signed natural
    `amount`). Section headers, totals and the beginning-balance row carry no
    transaction id and are skipped."""
    keys = _column_keys(payload)
    found = []

    def walk(rows):
        for row in rows or []:
            cells = row.get('ColData')
            if cells and not row.get('Rows') and row.get('type', 'Data') == 'Data':
                values = {keys[i]: c.get('value', '') for i, c in enumerate(cells) if i < len(keys) and keys[i]}
                ids = {keys[i]: c.get('id', '') for i, c in enumerate(cells) if i < len(keys) and keys[i]}
                txn_id = ids.get('tx_date') or ids.get('txn_type') or next((v for v in ids.values() if v), '')
                if txn_id and values.get('txn_type') and values.get('tx_date'):
                    item = {
                        'txn_id': str(txn_id), 'txn_type': values['txn_type'].strip(), 'date': values['tx_date'].strip(),
                        'doc_num': values.get('doc_num', '').strip(), 'name': values.get('name', '').strip(),
                        'memo': values.get('memo', '').strip(), 'split': values.get('split_acc', '').strip(),
                    }
                    if 'debt_amt' in values or 'credit_amt' in values:
                        item['debit'], item['credit'] = _money(values.get('debt_amt')), _money(values.get('credit_amt'))
                    else:
                        item['amount'] = _money(values.get('subt_nat_amount'))
                    found.append(item)
            walk((row.get('Rows') or {}).get('Row', []))

    walk((payload.get('Rows') or {}).get('Row', []))
    return found


def _read_ledger(token, account_qb_id, start, end):
    resp = requests.get(
        f'{API_BASES[_environment()]}/{token.realm_id}/reports/GeneralLedger',
        params={
            'start_date': start.isoformat(), 'end_date': end.isoformat(), 'account': account_qb_id,
            'columns': LEDGER_COLUMNS, 'sort_by': 'tx_date', 'sort_order': 'ascend',
        },
        headers={'Authorization': f'Bearer {token.access_token}', 'Accept': 'application/json'},
        timeout=30,
    )
    resp.raise_for_status()
    logger.info('QuickBooks General Ledger read ok (intuit_tid=%s)', _intuit_tid(resp))
    return parse_general_ledger(resp.json())


def fetch_ledger(token, account_qb_id, start, end):
    """(entries, error): the transactions that hit one QuickBooks account between
    two dates. Refreshes the token if needed, and retries once like the other reads
    (a 401 forces a refresh; a rejected refresh means reconnect)."""
    if not is_configured():
        return None, 'QuickBooks client ID/secret are not set — add them in Admin Tools.'
    outcome = _refresh_with_retry(token)
    if outcome == REFRESH_REJECTED:
        return None, RECONNECT_ERROR
    if outcome == REFRESH_FAILED:
        return None, LEDGER_ERROR
    try:
        return _read_ledger(token, account_qb_id, start, end), ''
    except Exception as exc:
        logger.warning('QuickBooks General Ledger failed, retrying once: %s', _failure_summary(exc))
        if _status_code(exc) == 401:
            outcome = _refresh_if_needed(token, force=True)
            if outcome == REFRESH_REJECTED:
                return None, RECONNECT_ERROR
            if outcome != REFRESH_OK:
                return None, LEDGER_ERROR
        else:
            time.sleep(RETRY_DELAY_SECONDS)
    try:
        return _read_ledger(token, account_qb_id, start, end), ''
    except Exception as exc:
        logger.error('QuickBooks General Ledger failed after retry: %s', _failure_summary(exc))
        return None, LEDGER_ERROR
