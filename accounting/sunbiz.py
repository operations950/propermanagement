"""Looks an association up on Florida's corporation search (SunBiz), keeps its official record current, and raises alerts.

The alerts, kept on the association's profile (AssociationProfile.sunbiz_alerts) and shown on its page:
  * the registered agent is not us (we should be the agent for the associations we manage);
  * the state does not list it as active;
  * this year's annual report is not filed and the May 1 deadline has passed;
  * the officers the state lists changed since we last looked.

SunBiz may refuse a request from a server (the person using a browser is let in; a program sometimes gets HTTP 403). When that happens nothing is changed, the
"checked on" date stays what it was, and the person is told that the lookup was refused: a failed lookup must never look like a clean one.
"""
import html as htmllib
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime

from django.utils import timezone

logger = logging.getLogger(__name__)

BASE = 'https://search.sunbiz.org'
BY_NAME = BASE + '/Inquiry/CorporationSearch/SearchResults/EntityName/{term}/Page1?searchNameOrder={order}'   # a document-number search needs a form post; a name search is a plain page
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
           'Accept': 'text/html,application/xhtml+xml', 'Accept-Language': 'en-US,en;q=0.9'}
OUR_NAME = 'proper realty'


class SunbizError(Exception):
    pass


class SunbizRefused(SunbizError):
    """SunBiz answered 403 or 429: it does not let a program look things up. Nothing is worked around; a person checks by hand."""


REFUSED_NOTE = 'SunBiz does not allow automatic lookups: check by hand.'
STALE_DAYS = 90


def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return resp.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            raise SunbizRefused(f'SunBiz refused the lookup (HTTP {e.code}).')
        raise SunbizError(f'SunBiz answered with an error (HTTP {e.code}).')
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise SunbizError(f'Could not reach SunBiz ({e}).')


def search_link(legal_name):
    """A link a person can click that lands on SunBiz's list of entities starting with this name (the one we want is at or near the top). '' when there is no name.
    SunBiz has no plain link for a document-number search (that needs a form post and a bare link lands on its error page), so the name search is the link that works."""
    term = ' '.join((legal_name or '').replace(',', ' ').replace('.', ' ').split())
    if not term:
        return ''
    return BY_NAME.format(term=urllib.parse.quote(term), order=re.sub(r'[^A-Z0-9]', '', term.upper()))


def _text(fragment):
    """An HTML fragment as plain lines joined with commas (an address block)."""
    parts = re.split(r'<br\s*/?>', fragment or '')
    lines = [htmllib.unescape(re.sub(r'<[^>]+>', '', p)).strip() for p in parts]
    return ', '.join(l for l in (' '.join(x.split()) for x in lines) if l)


def _date(s):
    try:
        return datetime.strptime(s.strip(), '%m/%d/%Y').date()
    except (ValueError, AttributeError):
        return None


def parse_detail(page):
    """The detail page of one entity as a dict. Raises SunbizError when the page is not one (a block page, a changed layout)."""
    m = re.search(r'<div class="detailSection corporationName">\s*<p>([^<]*)</p>\s*<p>([^<]*)</p>', page)
    if not m:
        raise SunbizError('That page did not look like a SunBiz record (the layout may have changed).')
    out = {'kind': htmllib.unescape(m.group(1)).strip(), 'name': htmllib.unescape(m.group(2)).strip()}
    labels = {k.strip(): htmllib.unescape(v).strip() for k, v in re.findall(r'<label[^>]*>([^<]+)</label>\s*<span>([^<]*)</span>', page)}
    out['doc'], out['ein'], out['status'] = labels.get('Document Number', ''), labels.get('FEI/EIN Number', ''), labels.get('Status', '')
    out['filed'] = _date(labels.get('Date Filed', ''))
    for key, title in (('principal', 'Principal Address'), ('mailing', 'Mailing Address')):
        a = re.search(rf'<span>{title}</span>\s*<span>\s*<div>(.*?)</div>', page, re.S)
        out[key] = _text(a.group(1)) if a else ''
    a = re.search(r'Registered Agent Name (?:&amp;|&) Address</span>\s*<span>([^<]*)</span>\s*<span>\s*<div>(.*?)</div>', page, re.S)
    out['agent_name'] = ' '.join(htmllib.unescape(a.group(1)).split()) if a else ''
    out['agent'] = (out['agent_name'] + ', ' + _text(a.group(2))) if a else ''
    out['officers'] = []
    section = re.search(r'Officer/Director Detail(.*?)(?:<div class="detailSection">\s*<span>Annual Reports|$)', page, re.S)
    if section:
        for title, name in re.findall(r'Title(?:&nbsp;|\s)+([^<]+)</span>\s*<br\s*/?>\s*<br\s*/?>\s*([^<]+?)\s*<span>', section.group(1)):
            name = htmllib.unescape(name).strip()
            first_last = ' '.join(reversed([x.strip() for x in name.split(',', 1)])) if ',' in name else name
            out['officers'].append((first_last.title() if first_last.isupper() else first_last, htmllib.unescape(title).strip()))
    docs = re.findall(r'(\d\d/\d\d/\d{4}) -- ([A-Z][A-Za-z. ]+?)</a>', page)
    annual = [_date(d) for d, what in docs if what.strip().upper() == 'ANNUAL REPORT']
    amended = [_date(d) for d, what in docs if what.strip().upper() == 'AMENDED ANNUAL REPORT']
    out['annual'] = max((d for d in annual if d), default=None)
    out['amended'] = max((d for d in amended if d), default=None)
    return out


def lookup(doc_number, name=''):
    """The state's record for one document number, found by searching the association's name and picking the row with that number."""
    doc = (doc_number or '').strip()
    if not doc:
        raise SunbizError('There is no state document number to look up.')
    term = ' '.join((name or '').replace(',', ' ').replace('.', ' ').split())
    if not term:
        raise SunbizError('There is no legal name to search for.')
    results = fetch(BY_NAME.format(term=urllib.parse.quote(term), order=re.sub(r'[^A-Z0-9]', '', term.upper())))
    for row in re.findall(r'<tr[^>]*>(.*?)</tr>', results, re.S):
        link = re.search(r'href="(/Inquiry/CorporationSearch/SearchResultDetail[^"]+)"', row)
        cells = [htmllib.unescape(re.sub(r'<[^>]+>', '', c)).strip() for c in re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)]
        if link and any(c.upper() == doc.upper() for c in cells):
            return parse_detail(fetch(BASE + htmllib.unescape(link.group(1))))
    raise SunbizError(f'SunBiz has no record with document number {doc}.')


def _norm(n):
    return re.sub(r'[^a-z]', '', (n or '').lower())


def alerts_for(profile, rec, today=None, previous_officers=None):
    """What a person should look at, given the state's record."""
    today = today or timezone.localdate()
    out = []
    if OUR_NAME not in (rec['agent_name'] or '').lower():
        out.append(f"The registered agent is {rec['agent_name'] or 'not listed'}, not Proper Realty Co.")
    if (rec['status'] or '').strip().lower() != 'active':
        out.append(f"The state lists this association as {rec['status'] or 'not active'}.")
    if rec['annual'] is None or rec['annual'].year < today.year:
        if today > date(today.year, 5, 1):
            out.append(f'The {today.year} annual report was due May 1 and the state shows none filed.')
    if previous_officers:
        before, after = {_norm(o['name']) for o in previous_officers}, {_norm(n) for n, t in rec['officers']}
        if before and before != after:
            names = {_norm(n): n for n, t in rec['officers']}
            added = sorted(names[k] for k in after - before)
            gone = sorted(o['name'] for o in previous_officers if _norm(o['name']) in before - after)
            out.append('The state\'s officers changed' + (f': now {", ".join(added)}' if added else '') + (f'; no longer {", ".join(gone)}' if gone else '') + '.')
    return out


def apply_record(profile, rec, today=None):
    """Bring the profile's official record up to what the state says, and set its alerts. Returns the alerts."""
    today = today or timezone.localdate()
    previous = list(profile.sunbiz_officers or [])
    alerts = alerts_for(profile, rec, today, previous)
    profile.legal_name = rec['name'].title() if rec['name'].isupper() else rec['name']
    profile.state_doc_number, profile.ein, profile.state_status = rec['doc'] or profile.state_doc_number, rec['ein'] or profile.ein, rec['status'].title()
    profile.date_filed = rec['filed'] or profile.date_filed
    profile.principal_address, profile.mailing_address, profile.registered_agent = rec['principal'], rec['mailing'], rec['agent']
    profile.annual_report_filed = rec['annual'] or profile.annual_report_filed
    profile.sunbiz_officers = [{'name': n, 'title': t} for n, t in rec['officers']]
    profile.sunbiz_checked_on, profile.sunbiz_checked_by, profile.sunbiz_alerts = today, 'auto', alerts
    profile.save()
    return alerts


def check(profile, fetcher=None):
    """Look one association up and update it. Returns (ok, message). Never raises for a lookup problem."""
    try:
        rec = (fetcher or lookup)(profile.state_doc_number, profile.legal_name)
    except SunbizRefused as e:
        logger.info('SunBiz refused the lookup for %s (%s); nothing was changed.', profile.property.name, e)
        return False, REFUSED_NOTE                   # records nothing: no alert, no date, a hand check stays as it was
    except SunbizError as e:
        logger.warning('SunBiz lookup for %s failed: %s', profile.property.name, e)
        return False, str(e)
    alerts = apply_record(profile, rec)
    return True, ('Checked. ' + ' '.join(alerts)) if alerts else 'Checked: nothing needs a look.'


def hand_check(profile, today=None, status=None, agent_ok=None, annual_report_filed=None):
    """A person looked the association up on SunBiz and says so. Records today as the check date ('hand') and works the alerts out again from what they say. All of it is optional:
    the status the state shows, whether the registered agent is still us (True/False; None means they did not say), the last annual report date. Anything left out stays as it was,
    and an unknown agent, status or report date never raises an alert. Returns the alerts."""
    today = today or timezone.localdate()
    if (status or '').strip():
        profile.state_status = status.strip()[:30]
    if annual_report_filed:
        profile.annual_report_filed = annual_report_filed
    if agent_ok is True and not profile.registered_agent_is_us():
        profile.registered_agent = 'Proper Realty Co.'
    elif agent_ok is False and (profile.registered_agent_is_us() or not (profile.registered_agent or '').strip()):
        profile.registered_agent = 'Someone else: see SunBiz'
    agent_name = (profile.registered_agent or '').split(',')[0].strip()
    rec = {'agent_name': agent_name or OUR_NAME, 'status': profile.state_status or 'Active', 'annual': profile.annual_report_filed or today, 'officers': []}
    alerts = alerts_for(profile, rec, today, previous_officers=None)
    profile.sunbiz_alerts, profile.sunbiz_checked_on, profile.sunbiz_checked_by = alerts, today, 'hand'
    profile.save()
    return alerts


def needs_check(profile, today=None, days=STALE_DAYS):
    """True when nobody has looked at the official record for more than `days` days (or never)."""
    today = today or timezone.localdate()
    return profile.sunbiz_checked_on is None or (today - profile.sunbiz_checked_on).days > days
