"""Airbnb and VRBO links for each unit, and their guest ratings.

Each unit (or a single-unit property) can have one Airbnb link and one VRBO link. About once a
month the program opens each link's public page and reads the guest rating and review count the
platform publishes for search engines (a `aggregateRating` block, or the same numbers in the page's
data), keeping every reading so the trend shows.

Reading someone else's website is not something a platform promises to allow. Airbnb and VRBO
sometimes answer an automatic visitor with a "prove you are human" page, and that can start or stop
at any time. So this never pretends: a read that doesn't work is recorded with the reason, tried
again a couple of times, and then left for a person, who can type the rating in by hand (kept
apart from the automatic ones). Only airbnb / vrbo addresses are ever opened, only over https, and
redirects are not followed to any other site."""
import json
import logging
import re
import time
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

import requests
from django.db import transaction
from django.utils import timezone

from .models import ListingLink, ListingRating

logger = logging.getLogger(__name__)

CHECK_EVERY = timedelta(days=30)
RETRY_AFTER = timedelta(days=3)          # a failed read is tried again after this long ...
MAX_FAILURES = 3                         # ... up to this many in a row, then it waits for a person
TIMEOUT = 15
MAX_BYTES = 2_500_000
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
HOSTS = {
    ListingLink.Platform.AIRBNB: re.compile(r'^([a-z0-9-]+\.)*airbnb\.(com|co\.[a-z]{2}|com\.[a-z]{2}|[a-z]{2})$'),
    ListingLink.Platform.VRBO: re.compile(r'^([a-z0-9-]+\.)*vrbo\.com$'),
}
BLOCK_HINTS = ('captcha', 'access denied', 'unusual traffic', 'verify you are a human', 'are you a robot', 'pardon our interruption', 'px-captcha')


class ListingError(ValueError):
    """A link or a rating that can't be accepted, or a page that couldn't be read."""


# --- the links -----------------------------------------------------------------------------------

def host_ok(platform, url):
    parts = urlsplit(url)
    return parts.scheme == 'https' and bool(parts.hostname) and bool(HOSTS[platform].match(parts.hostname.lower()))


def clean_url(platform, raw):
    """The address as it will be stored, or a ListingError saying what is wrong. A blank is ''."""
    raw = (raw or '').strip()
    if not raw:
        return ''
    if platform not in HOSTS:
        raise ListingError('Choose Airbnb or VRBO.')
    if '://' not in raw:
        raw = 'https://' + raw
    parts = urlsplit(raw)
    if parts.scheme != 'https' or not host_ok(platform, raw):
        label = dict(ListingLink.Platform.choices)[platform]
        raise ListingError(f'That doesn\'t look like a {label} address (it should start with https:// and be on {label.lower()}).')
    if len(parts.path.strip('/')) < 2:
        raise ListingError('That address is for the site, not for one listing — paste the listing\'s own page.')
    return parts._replace(fragment='').geturl()[:500]


@transaction.atomic
def set_link(prop, unit, platform, raw_url):
    """Saves (or, when blank, removes) the link. Changing the address clears the old rating: it belonged
    to a different page. Returns the link or None."""
    url = clean_url(platform, raw_url)
    existing = ListingLink.objects.filter(property=prop, unit=unit, platform=platform).first()
    if not url:
        if existing:
            existing.delete()
        return None
    if existing is None:
        return ListingLink.objects.create(property=prop, unit=unit, platform=platform, url=url)
    if existing.url != url:
        existing.history.all().delete()
        existing.url, existing.rating, existing.review_count = url, None, None
        existing.rating_source, existing.rating_checked_at, existing.last_attempt_at, existing.check_error, existing.failures = '', None, None, '', 0
        existing.save()
    return existing


# --- reading a page ------------------------------------------------------------------------------

def _number(text):
    try:
        return Decimal(str(text))
    except (InvalidOperation, ValueError):
        return None


def _rating_from(value, ten_point=True):
    rating = _number(value)
    if rating is None:
        return None
    if ten_point and Decimal('5') < rating <= Decimal('10'):        # a ten-point scale (read from a page, never typed)
        rating = rating / 2
    if not (Decimal('1') <= rating <= Decimal('5')):
        return None
    return rating.quantize(Decimal('0.01'))


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def parse_rating(html):
    """(rating, review_count) found in a listing page's text, or (None, None). Looks first at the
    structured data the page publishes for search engines, then at the numbers in the page's own data."""
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html or '', flags=re.DOTALL | re.IGNORECASE):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        for node in _walk(data):
            agg = node.get('aggregateRating')
            if isinstance(agg, dict):
                rating = _rating_from(agg.get('ratingValue'))
                if rating is not None:
                    count = agg.get('reviewCount', agg.get('ratingCount'))
                    return rating, int(count) if str(count).isdigit() else None
    rating = None
    for pattern in (r'"ratingValue"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)', r'"starRating"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)', r'"guestSatisfactionOverall"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)',
                    r'"averageRating"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)'):
        m = re.search(pattern, html or '')
        if m and _rating_from(m.group(1)) is not None:
            rating = _rating_from(m.group(1))
            break
    if rating is None:
        return None, None
    count = None
    for pattern in (r'"(?:reviewCount|reviewsCount|ratingCount|numberOfReviews|totalReviewCount)"\s*:\s*"?(\d+)',):
        m = re.search(pattern, html or '')
        if m:
            count = int(m.group(1))
            break
    return rating, count


def fetch_page(platform, url):
    """The listing's page text. Raises ListingError with a reason a person can read."""
    if not host_ok(platform, url):
        raise ListingError('The address is not an Airbnb / VRBO page, so it was not opened.')
    headers = {'User-Agent': USER_AGENT, 'Accept': 'text/html,application/xhtml+xml', 'Accept-Language': 'en-US,en;q=0.9'}
    try:
        response = requests.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=False, stream=True)
        hops = 0
        while response.is_redirect and hops < 4:
            target = requests.compat.urljoin(url, response.headers.get('Location', ''))
            if not host_ok(platform, target):
                raise ListingError('The page sent us to another site, which is not followed.')
            url = target
            response = requests.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=False, stream=True)
            hops += 1
        body = response.raw.read(MAX_BYTES, decode_content=True)
    except requests.RequestException as exc:
        raise ListingError(f'Couldn\'t reach the page ({exc.__class__.__name__}).') from None
    text = body.decode('utf-8', errors='replace')
    if response.status_code in (403, 429, 503) or any(h in text[:6000].lower() for h in BLOCK_HINTS) and 'ratingValue' not in text:
        raise ListingError(f'The platform blocked the automatic read (HTTP {response.status_code}); it wants a person, not a program. Enter the rating by hand.')
    if response.status_code == 404:
        raise ListingError('The listing page was not found — check the link.')
    if response.status_code >= 400:
        raise ListingError(f'The platform answered HTTP {response.status_code}.')
    return text


def read_rating(platform, url, fetch=None):
    """(rating, review_count) from the live page. A page with no rating (a brand-new listing) raises
    ListingError saying so."""
    rating, count = parse_rating((fetch or fetch_page)(platform, url))
    if rating is None:
        raise ListingError('The page loaded but showed no rating (a new listing has none yet, or the page changed shape).')
    return rating, count


# --- keeping the readings ------------------------------------------------------------------------

def refresh(link, fetch=None, now=None):
    """Reads one link now. Returns True on success; on failure records why and returns False."""
    now = now or timezone.now()
    try:
        rating, count = read_rating(link.platform, link.url, fetch=fetch)
    except ListingError as exc:
        ListingLink.objects.filter(pk=link.pk).update(last_attempt_at=now, check_error=str(exc)[:200], failures=link.failures + 1)
        link.last_attempt_at, link.check_error, link.failures = now, str(exc)[:200], link.failures + 1
        return False
    _store(link, rating, count, ListingLink.Source.AUTO, now)
    return True


def _store(link, rating, count, source, now):
    link.rating, link.review_count, link.rating_source = rating, count, source
    link.rating_checked_at, link.last_attempt_at, link.check_error, link.failures = now, now, '', 0
    link.save(update_fields=['rating', 'review_count', 'rating_source', 'rating_checked_at', 'last_attempt_at', 'check_error', 'failures'])
    ListingRating.objects.create(link=link, rating=rating, review_count=count, source=source)


def set_manual(link, rating_text, reviews_text, now=None):
    """A person types the rating in (when the platform won't let a program read it)."""
    rating = _rating_from(rating_text, ten_point=False)
    if rating is None:
        raise ListingError('Enter the rating as a number from 1 to 5, like 4.85.')
    reviews = None
    if (reviews_text or '').strip():
        if not str(reviews_text).strip().isdigit():
            raise ListingError('Enter the number of reviews as a whole number.')
        reviews = int(str(reviews_text).strip())
    _store(link, rating, reviews, ListingLink.Source.MANUAL, now or timezone.now())
    return link


def due_links(now=None):
    """Links whose rating is a month old (or never read), skipping ones that keep failing."""
    now = now or timezone.now()
    out = []
    for link in ListingLink.objects.select_related('property', 'unit').filter(property__is_active=True):
        if link.rating_source == ListingLink.Source.MANUAL and link.rating_checked_at and now - link.rating_checked_at < CHECK_EVERY:
            continue
        if link.rating_checked_at and now - link.rating_checked_at < CHECK_EVERY:
            continue
        if link.failures >= MAX_FAILURES:
            continue
        if link.failures and link.last_attempt_at and now - link.last_attempt_at < RETRY_AFTER:
            continue
        out.append(link)
    return out


def run_due(limit=25, pause=8, fetch=None, now=None, sleep=time.sleep):
    """The monthly job: reads the links that are due, a few seconds apart so it is gentle. Returns
    {'read': n, 'failed': n}."""
    result = {'read': 0, 'failed': 0}
    for i, link in enumerate(due_links(now)[:limit]):
        if i:
            sleep(pause)
        try:
            ok = refresh(link, fetch=fetch, now=now)
        except Exception:                      # one odd page must not stop the rest
            logger.exception('Rating read failed for link %s', link.pk)
            ok = False
        result['read' if ok else 'failed'] += 1
    return result


# --- what the screens show -----------------------------------------------------------------------

def summary(link):
    """A link with its rating, how long ago, and how it moved since the reading before."""
    recent = list(link.history.all()[:2])            # newest first
    previous = recent[1] if len(recent) > 1 else None
    change = (link.rating - previous.rating) if (previous is not None and link.rating is not None) else None
    stale = bool(link.rating_checked_at and timezone.now() - link.rating_checked_at > CHECK_EVERY * 2)
    return {'link': link, 'change': change, 'stale': stale, 'gave_up': link.failures >= MAX_FAILURES}


DROP_ALERT = Decimal('0.10')


def attention():
    """What needs a person: {'places_without_links', 'unreadable', 'dropped'} counts, for the reminders."""
    from .models import Property
    active = Property.objects.filter(is_active=True, is_general=False, property_type=Property.Type.SHORT_TERM_RENTAL).prefetch_related('units', 'listing_links')
    without = 0
    for prop in active:
        units = [u for u in prop.units.all() if u.is_active]
        have = {(l.unit_id) for l in prop.listing_links.all()}
        places = [u.pk for u in units] if units else [None]
        without += sum(1 for place in places if place not in have)
    links = ListingLink.objects.filter(property__is_active=True)
    unreadable = links.filter(failures__gte=MAX_FAILURES).exclude(rating_source=ListingLink.Source.MANUAL, rating_checked_at__gte=timezone.now() - CHECK_EVERY).count()
    dropped = 0
    for link in links.filter(rating__isnull=False):
        recent = list(link.history.all()[:2])
        if len(recent) == 2 and recent[0].rating - recent[1].rating <= -DROP_ALERT and timezone.now() - recent[0].checked_at < CHECK_EVERY * 2:
            dropped += 1
    return {'places_without_links': without, 'unreadable': unreadable, 'dropped': dropped}


def links_for(prop):
    """{unit id or None: {platform: summary}} for one property."""
    out = {}
    for link in ListingLink.objects.filter(property=prop).select_related('unit'):
        out.setdefault(link.unit_id, {})[link.platform] = summary(link)
    return out
