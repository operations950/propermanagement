"""Gentle reminders about work that piles up: things the system can't force anyone to do
(there are no hard validations), so it says so, where people look every day.

`collect(user)` returns the open reminders as plain dicts, most pressing first; the dashboards
show them in one "Needs attention" card. Nothing here blocks anything. A reminder appears only
while there is something to do, gets amber once it has waited a while, and links straight to
where it is done."""
from datetime import timedelta

from django.db.models import Count, Min
from django.urls import reverse
from django.utils import timezone

from . import listings, property_specs, qb_accounts, trash
from .models import (ContactImportCandidate, ContactUpdateCandidate, MonthClose, Property, PropertyFAQ, QuickBooksToken)

FAQ_OVERDUE_DAYS = 7          # an unreviewed assistant answer that has waited this long turns amber
CLOSE_GRACE_DAY = 5           # last month's books are "late" from this day of the month on


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


def collect(user, today=None):
    today = today or timezone.localdate()
    staff = getattr(user, 'staff_profile', None)
    is_admin = bool(staff and staff.is_company_admin)
    items = []

    faq = faq_stats(today)
    if faq['count']:
        late = faq['oldest_days'] >= FAQ_OVERDUE_DAYS
        items.append({
            'key': 'faq', 'count': faq['count'], 'level': 'warn' if late else 'info', 'icon': 'messages-square', 'url': reverse('faq_review_queue'),
            'text': f'{faq["count"]} assistant FAQ {_plural(faq["count"], "answer")} to review across {faq["properties"]} {_plural(faq["properties"], "property", "properties")}',
            'detail': f'The oldest has waited {faq["oldest_days"]} days.' if late else 'The assistant is already using them; a quick look makes them permanent.',
        })

    try:
        from onsite.services import review as review_service
        overlaps, unpaid = review_service.counts(today)
    except Exception:
        overlaps = unpaid = 0
    if overlaps + unpaid:
        items.append({
            'key': 'reservations', 'count': overlaps + unpaid, 'level': 'warn', 'icon': 'calendar-check', 'url': reverse('onsite_reservation_review'),
            'text': f'{overlaps + unpaid} {_plural(overlaps + unpaid, "reservation")} to review',
            'detail': 'Overlapping stays, or upcoming bookings with no payout.',
        })

    contacts = (ContactImportCandidate.objects.filter(status=ContactImportCandidate.Status.PENDING).count()
                + ContactUpdateCandidate.objects.filter(status=ContactUpdateCandidate.Status.PENDING).count())
    if contacts:
        items.append({'key': 'contacts', 'count': contacts, 'level': 'info', 'icon': 'contact', 'url': reverse('contact_review'),
                      'text': f'{contacts} {_plural(contacts, "contact")} to review', 'detail': 'Imported or changed contacts waiting for a yes or no.'})

    incomplete = sum(
        1 for p in Property.objects.filter(is_active=True, is_general=False, property_type=Property.Type.SHORT_TERM_RENTAL).prefetch_related('units')
        if property_specs.missing_specs(p)
    )
    if incomplete:
        items.append({'key': 'specs', 'count': incomplete, 'level': 'info', 'icon': 'ruler', 'url': f'{reverse("property_list")}?needs=details',
                      'text': f'{incomplete} short-term {_plural(incomplete, "rental")} missing bedrooms, beds, baths or square footage',
                      'detail': 'Cleaning time estimates and prices use these.'})

    missing_trash = trash.missing_count()
    if missing_trash:
        items.append({'key': 'trash', 'count': missing_trash, 'level': 'info', 'icon': 'trash-2', 'url': reverse('property_list'),
                      'text': f'{missing_trash} short-term {_plural(missing_trash, "rental")} with no trash schedule',
                      'detail': 'Open the property and press "New Trash Schedule". Guests ask, and the assistant only answers from what is recorded.'})
    attention = listings.attention()
    if attention['dropped']:
        n = attention['dropped']
        items.append({'key': 'rating_drop', 'count': n, 'level': 'warn', 'icon': 'star', 'url': reverse('property_list'),
                      'text': f'A guest rating fell on {n} {_plural(n, "listing")}', 'detail': 'By a tenth of a point or more since the reading before. Worth a look at recent reviews.'})
    if attention['unreadable']:
        n = attention['unreadable']
        items.append({'key': 'rating_unreadable', 'count': n, 'level': 'info', 'icon': 'star', 'url': reverse('property_list'),
                      'text': f'{n} listing {_plural(n, "rating")} could not be read automatically',
                      'detail': 'The platform won\'t let a program read that page. Type the rating in by hand on the unit\'s card, or check the link.'})
    if attention['places_without_links']:
        n = attention['places_without_links']
        items.append({'key': 'listing_links', 'count': n, 'level': 'info', 'icon': 'link', 'url': reverse('property_list'),
                      'text': f'{n} {_plural(n, "unit")} with no Airbnb or VRBO link yet', 'detail': 'Add the link on the unit\'s card and its guest rating is kept up to date each month.'})

    if is_admin:
        close = _close_items(today)
        if close:
            items.append(close)
        if QuickBooksToken.objects.exists():
            unmapped = len(qb_accounts.rentals_needing_accounts())
            if unmapped:
                items.append({'key': 'qb', 'count': unmapped, 'level': 'info', 'icon': 'landmark', 'url': reverse('quickbooks_accounts'),
                              'text': f'{unmapped} {_plural(unmapped, "rental")} not tied to QuickBooks accounts yet',
                              'detail': 'They can\'t be closed each month until they are.'})

    items.sort(key=lambda i: (i['level'] != 'warn', -i['count']))
    return items
