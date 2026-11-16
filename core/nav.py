"""The smart Up link.

"Up" is not "Back". It moves the person out to a WIDER screen (a list, a hub, a checklist, a dashboard), and it goes to the wide screen they actually came from: a screen that can be
reached from more than one place (late fees, from the month-end close or from the accounting hub) shows "Month-end close" or "Accounting" depending on the way in.
A detail screen (one ticket, one property, one contact, one unit) is the bottom level: Up never points to one, only to a screen its view lists as a parent.

How it works
  * NavTrailMiddleware keeps a short trail in the session: the last ~15 real page visits (full-page GETs that answered 200). Each entry is [url name, full address with its query,
    the address's keyword arguments]. Another visit to the same screen (a month arrow, a filter, a page) replaces the top entry instead of stacking, and a POST that redirects back
    to the same screen leaves one entry.
  * up(request, parents, default) walks the trail from the most recent entry back and returns the first one whose url name is in `parents` (a {url name: label} dict of WIDE
    screens) and that belongs to the same record (the same association, property, month ...: a keyword argument the two screens share must have the same value).
    Nothing found (a bookmark, an emailed link, a new login) means `default`, the screen's usual parent.
  * Screens list their parents either in core/nav_map.py (read by the {% up_link %} tag, nothing else to write in the view) or by calling up() themselves when the label or the
    default is worked out in the view (accounting/web.py acc_up()).

Adding a screen: add one line to core/nav_map.py (its url name, the wide screens it can be reached from, its usual parent), and put {% up_link %} where the old hand-written
link was. A detail screen's own entry goes in DETAIL_VIEWS so the guard test keeps a detail out of every other detail's parents.
"""
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.urls import NoReverseMatch, reverse

TRAIL_KEY = 'nav_trail'
TRAIL_MAX = 15
SAME_RECORD_IGNORE = frozenset({'what', 'token'})          # keyword arguments that name a file or a variant, not a record


@dataclass
class Up:
    url: str
    label: str
    from_trail: bool = False

    def __bool__(self):
        return bool(self.url)


# ---------------------------------------------------------------- the trail

def _wants_a_page(request, response):
    """A real page visit: a full-page GET that answered 200 with HTML, from a person (not a background fetch, a download, a frame or a redirect)."""
    if request.method != 'GET' or getattr(response, 'status_code', 0) != 200 or getattr(response, 'streaming', False):
        return False
    if not response.get('Content-Type', '').startswith('text/html') or response.has_header('Content-Disposition'):
        return False
    if 'text/html' not in request.META.get('HTTP_ACCEPT', ''):
        return False
    if request.headers.get('X-Requested-With') or request.headers.get('HX-Request'):
        return False
    dest = request.headers.get('Sec-Fetch-Dest')
    if dest and dest != 'document':
        return False
    user = getattr(request, 'user', None)
    return bool(user and user.is_authenticated and getattr(request, 'resolver_match', None) and request.resolver_match.view_name)


def _kwargs(match):
    return {k: str(v) for k, v in (match.kwargs or {}).items()}


def record(request, response):
    """Add this page visit to the person's trail."""
    if not _wants_a_page(request, response):
        return
    session = getattr(request, 'session', None)
    if session is None:
        return
    entry = [request.resolver_match.view_name, request.get_full_path(), _kwargs(request.resolver_match)]
    trail = list(session.get(TRAIL_KEY) or [])
    if trail and trail[-1][0] == entry[0]:
        if trail[-1] == entry:
            return
        trail[-1] = entry
    else:
        trail.append(entry)
    session[TRAIL_KEY] = trail[-TRAIL_MAX:]


class NavTrailMiddleware:
    """Keeps the trail the Up link reads (see the module note). It only ever adds to the session after the page is built, so a failure here never takes a page down."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        try:
            record(request, response)
        except Exception:
            pass
        return response


def trail(request):
    session = getattr(request, 'session', None)
    return list(session.get(TRAIL_KEY) or []) if session is not None else []


# ---------------------------------------------------------------- resolving Up

def D(name, label, args=(), query=(), fixed=None):
    """A screen's usual parent, used when the trail has none of its wide parents: the url `name`, built from the current address's keyword arguments `args` (a tuple of names that
    are the same in both addresses, or a {parent argument: current argument} dict), with the query parameters `query` copied across (the month, say) and the `fixed` ones added."""
    return {'name': name, 'label': label, 'args': args, 'query': tuple(query), 'fixed': dict(fixed or {})}


def _default_up(request, spec):
    if callable(spec):                                       # a screen whose usual parent depends on the person (which layout they use)
        spec = spec(request)
    if spec is None:
        return Up('', '')
    if isinstance(spec, Up):
        return spec
    if isinstance(spec, (tuple, list)) and len(spec) == 2 and isinstance(spec[0], str):
        return Up(spec[0], spec[1])
    match = getattr(request, 'resolver_match', None)
    have = dict(match.kwargs) if match else {}
    args = spec.get('args') or ()
    wanted = args if isinstance(args, dict) else {a: a for a in args}
    try:
        url = reverse(spec['name'], kwargs={target: have[source] for target, source in wanted.items()})
    except (NoReverseMatch, KeyError):
        return Up('', '')
    query = list((spec.get('fixed') or {}).items()) + [(k, request.GET[k]) for k in spec.get('query') or () if request.GET.get(k)]
    if query:
        url += '?' + urlencode(query)
    return Up(url, spec['label'])


def _same_record(current, other):
    for key, value in current.items():
        if key in SAME_RECORD_IGNORE:
            continue
        if key in other and str(other[key]) != str(value):
            return False
    return True


CARRIED = ('month', 'year')


def _carry_month(url, request):
    """The month (or year) on the screen being left rides along to the parent: a person who paged to September on a close screen goes Up to September."""
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    changed = False
    for key in CARRIED:
        value = request.GET.get(key)
        if value and any(k == key for k, _ in query):
            query = [(k, value if k == key else v) for k, v in query]
            changed = True
    if not changed:
        return url
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def up(request, parents=None, default=None):
    """Where Up goes from this screen: the most recent wide screen on the trail that is one of `parents` ({url name: label, or a function of the request that returns the label}),
    else `default` (an Up, a (url, label) pair, or a D(...) spec). Always returns an Up (empty when there is nowhere to go)."""
    match = getattr(request, 'resolver_match', None)
    here = match.view_name if match else ''
    current = _kwargs(match) if match else {}
    wanted = parents or {}
    for name, url, kwargs in reversed(trail(request)):
        if name == here or name not in wanted:
            continue
        if not _same_record(current, kwargs):
            continue
        label = wanted[name]
        return Up(_carry_month(url, request), label(request) if callable(label) else label, True)
    return _default_up(request, default)


def resolve(request):
    """Up for the screen being shown, from the registry in core/nav_map.py (empty when the screen has no entry)."""
    from .nav_map import UP
    match = getattr(request, 'resolver_match', None)
    if not match:
        return Up('', '')
    entry = UP.get(match.view_name)
    if entry is None:
        return Up('', '')
    parents, default = entry
    return up(request, parents, default)
