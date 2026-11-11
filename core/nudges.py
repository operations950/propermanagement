"""Gentle reminders about work that piles up: things the system can't force anyone to do
(there are no hard validations), so it says so, where people look every day.

Each reminder belongs to the part of the system it is about: short-term rentals (On-Site), Contacts, Properties or Financials. The menu shows a small number beside
that part (how many reminders it has, not how many things), and the page itself opens with a short strip saying what each one is. `section_items(user, 'onsite')`
returns one part's reminders; `nav_counts(user)` the numbers for the menu; `collect(user)` all of them. Nothing here blocks anything. A reminder appears only
while there is something to do, gets amber once it has waited a while, and links straight to where it is done."""
from datetime import timedelta

from django.core.cache import cache
from django.db.models import Count, Min
from django.urls import reverse
from django.utils import timezone

from . import listings, property_specs, qb_accounts, trash
from .models import (ContactImportCandidate, ContactUpdateCandidate, MonthClose, Property, PropertyFAQ, QuickBooksToken)

FAQ_OVERDUE_DAYS = 7          # an unreviewed assistant answer that has waited this long turns amber
CLOSE_GRACE_DAY = 5           # last month's books are "late" from this day of the month on
SECTIONS = ('onsite', 'contacts', 'properties', 'financials')
SECTION_TITLE = {'onsite': 'On-Site', 'contacts': 'Contacts', 'properties': 'Properties', 'financials': 'Financials'}
NAV_CACHE_SECONDS = 120


def _plural(n, one, many=None):
    return one if n == 1 else (many or one + 's')


def faq_stats(today=None):
    """{'count', 'properties', 'oldest_days', 'week'} for assistant answers nobody has reviewed."""
    today = today or timezone.localdate()
    qs = PropertyFAQ.objects.filter(status=PropertyFAQ.Status.ACTIVE, reviewed=False)
    agg = qs.aggregate(n=Count('id'), props=Count('property', distinct=True), oldest=Min('created_at'))
    oldest = timezone.localtime(agg['oldest']).date() if agg['oldest'] else None
    return {
        'count': agg['n'], 'properties': agg['props'], 'oldest_days': (today - oldest).days if oldest else 0,
        'week': qs.filter(created_at__gte=timezone.now() - timedelta(days=7)).count(),
    }


def _close_items(today):
    """Admin: last month's books are not closed for some rentals, once the month has been over a few days."""
    if today.day < CLOSE_GRACE_DAY or not QuickBooksToken.objects.exists():
        return None
    from . import ledger
    month = ledger.previous_month(today)
    open_count = mapped = 0
    for prop in ledger.rentals():
        if qb_accounts.status(prop) != 'mapped':
            continue
        mapped += 1
        books = ledger.books_for(prop, month)
        if not books or not all(ledger.is_closed(b, month) for b in books):
            open_count += 1
    if not open_count:
        return None
    return {
        'key': 'close', 'count': open_count, 'level': 'warn', 'icon': 'lock-keyhole', 'url': f'{reverse("close_overview")}?month={month:%Y-%m}',
        'text': f'{month:%B} isn\'t closed for {open_count} of {mapped} {_plural(mapped, "rental")}',
        'detail': 'Owner payments are made from the closed books.',
    }


def _item(section, key, count, level, icon, url, text, detail=''):
    return {'section': section, 'key': key, 'count': count, 'level': level, 'icon': icon, 'url': url, 'text': text, 'detail': detail}


def _onsite(today):
    """Short-term rentals: reservations to look at, and what is missing from the rentals themselves."""
    items = []
    try:
        from onsite.services import review as review_service
        overlaps, unpaid = review_service.counts(today)
    except Exception:
        overlaps = unpaid = 0
    if overlaps + unpaid:
        n = overlaps + unpaid
        items.append(_item('onsite', 'reservations', n, 'warn', 'calendar-check', reverse('onsite_reservation_review'), f'{n} {_plural(n, "reservation")} to review',
                           'Overlapping stays, or upcoming bookings with no payout.'))

    attention = listings.attention()
    if attention['places_without_links']:
        n = attention['places_without_links']
        where = attention['missing']
        names = ', '.join(p.name for p, labels in where)
        first = reverse('property_detail', args=[where[0][0].pk]) + '#units' if where else reverse('property_list')
        items.append(_item('onsite', 'listing_links', n, 'info', 'link', first, f'{n} {_plural(n, "unit")} with no Airbnb or VRBO listing link',
                           f'{len(where)} {_plural(len(where), "property", "properties")}: {names}. Open one and add the link, or tick "Not on Airbnb or VRBO" so it stops counting.'))
    if attention['dropped']:
        n = attention['dropped']
        items.append(_item('onsite', 'rating_drop', n, 'warn', 'star', reverse('property_list'), f'A guest rating fell on {n} {_plural(n, "listing")}',
                           'By a tenth of a point or more since the reading before. Worth a look at recent reviews.'))
    if attention['unreadable']:
        n = attention['unreadable']
        items.append(_item('onsite', 'rating_unreadable', n, 'info', 'star', reverse('property_list'), f'{n} listing {_plural(n, "rating")} could not be read automatically',
                           'The platform won\'t let a program read that page. Type the rating in by hand on the unit\'s card, or check the link.'))

    incomplete = sum(
        1 for p in Property.objects.filter(is_active=True, is_general=False, property_type=Property.Type.SHORT_TERM_RENTAL).prefetch_related('units')
        if property_specs.missing_specs(p)
    )
    if incomplete:
        items.append(_item('onsite', 'specs', incomplete, 'info', 'ruler', f'{reverse("property_list")}?needs=details',
                           f'{incomplete} short-term {_plural(incomplete, "rental")} missing bedrooms, beds, baths or square footage', 'Cleaning time estimates and prices use these.'))
    missing_trash = trash.missing_count()
    if missing_trash:
        items.append(_item('onsite', 'trash', missing_trash, 'info', 'trash-2', reverse('property_list'), f'{missing_trash} short-term {_plural(missing_trash, "rental")} with no trash schedule',
                           'Open the property and press "New Trash Schedule". Guests ask, and the assistant only answers from what is recorded.'))
    return items


def _contacts(today):
    n = (ContactImportCandidate.objects.filter(status=ContactImportCandidate.Status.PENDING).count()
         + ContactUpdateCandidate.objects.filter(status=ContactUpdateCandidate.Status.PENDING).count())
    if not n:
        return []
    return [_item('contacts', 'contacts', n, 'info', 'contact', reverse('contact_review'), f'{n} {_plural(n, "contact")} waiting for a yes or no',
                  'Imported or changed contacts. Nothing changes until you say yes.')]


def _properties(today):
    faq = faq_stats(today)
    if not faq['count']:
        return []
    late = faq['oldest_days'] >= FAQ_OVERDUE_DAYS
    return [_item('properties', 'faq', faq['count'], 'warn' if late else 'info', 'messages-square', reverse('faq_review_queue'),
                  f'{faq["count"]} assistant FAQ {_plural(faq["count"], "answer")} to review across {faq["properties"]} {_plural(faq["properties"], "property", "properties")}',
                  f'The oldest has waited {faq["oldest_days"]} days.' if late else 'The assistant is already using them; a quick look makes them permanent.')]


def _financials(today, is_admin):
    items = []
    if is_admin:
        close = _close_items(today)
        if close:
            items.append(dict(close, section='financials'))
        if QuickBooksToken.objects.exists():
            unmapped = len(qb_accounts.rentals_needing_accounts())
            if unmapped:
                items.append(_item('financials', 'qb', unmapped, 'info', 'landmark', reverse('quickbooks_accounts'), f'{unmapped} {_plural(unmapped, "rental")} not tied to QuickBooks accounts yet',
                                   'They can\'t be closed each month until they are.'))
    return items


def _is_admin(user):
    staff = getattr(user, 'staff_profile', None)
    return bool(staff and staff.is_company_admin)


def _sorted(items):
    return sorted(items, key=lambda i: (i['level'] != 'warn', -i['count']))


def section_items(user, section, today=None):
    """One part of the system's reminders, most pressing first."""
    today = today or timezone.localdate()
    if section == 'onsite':
        return _sorted(_onsite(today))
    if section == 'contacts':
        return _sorted(_contacts(today))
    if section == 'properties':
        return _sorted(_properties(today))
    if section == 'financials':
        return _sorted(_financials(today, _is_admin(user)))
    return []


def collect(user, today=None):
    today = today or timezone.localdate()
    items = []
    for section in SECTIONS:
        items += section_items(user, section, today)
    return _sorted(items)


def nav_counts(user):
    """{'onsite': 4, 'contacts': 1, ...} for the menu: how many reminders each part has (a part with none is left out). Company admins only, and kept for a couple of minutes
    so the menu does not cost a pile of queries on every page. A problem here must never take a page down."""
    if not _is_admin(user):
        return {}
    cached = cache.get('nudges:nav')
    if cached is not None:
        return cached
    counts = {}
    try:
        today = timezone.localdate()
        for section in SECTIONS:
            n = len(section_items(user, section, today))
            if n:
                counts[section] = n
    except Exception:
        return {}
    cache.set('nudges:nav', counts, NAV_CACHE_SECONDS)
    return counts
