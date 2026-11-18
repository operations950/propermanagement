"""Everything that needs a look and is about the PROPERTIES, gathered for the Alerts button on the Properties screen.

Missing documents are for everyone on the team. The rest are the reminders the system already works out for the people who run the business (Admin or Financial): assistant answers waiting for
a review, rentals with no listing link, a fallen or unreadable rating, missing size details, no trash schedule, rentals not tied to QuickBooks accounts, and associations whose late fee or
interest terms were typed in but never confirmed. Each item links straight to where it is dealt with. Nothing here changes anything, and a problem in one collector never takes the page down.
"""
import logging

from django.urls import reverse
from django.utils import timezone

from . import nudges, required_docs

logger = logging.getLogger(__name__)

ONSITE_PROPERTY_KEYS = ('listing_links', 'rating_drop', 'rating_unreadable', 'specs', 'trash')     # the On-Site reminders that are about a property (not a reservation)


def _item(key, count, level, icon, url, text, detail=''):
    return {'section': 'properties', 'key': key, 'count': count, 'level': level, 'icon': icon, 'url': url, 'text': text, 'detail': detail}


def _missing_documents():
    missing = required_docs.all_missing()
    if not missing:
        return []
    n = len(missing)
    names = ', '.join(p.name for p, _ in missing[:4]) + (f' and {n - 4} more' if n > 4 else '')
    return [_item('documents', n, 'warn', 'file-warning', reverse('missing_documents'), f'{n} {nudges._plural(n, "property", "properties")} missing a required document',
                  f'{names}. A management agreement for every property, and a lease on every commercial and long-term rental.')]


def _terms_not_confirmed():
    """Associations we keep the books for whose late fee or interest terms are not confirmed (until they are, nothing late is ever ready to charge)."""
    from accounting import closing
    out = []
    for prof in closing.associations(commercial=False):
        what = [word for word, ok in (('late fee', prof.late_fee_is_set()), ('interest', prof.interest_is_set())) if not ok]
        if what:
            out.append((prof.property, what))
    if not out:
        return []
    n = len(out)
    detail = '; '.join(f'{p.name}: {" and ".join(w)}' for p, w in out[:5]) + (f'; and {n - 5} more' if n > 5 else '')
    return [_item('terms', n, 'info', 'scale', reverse('accounting:settings', args=[out[0][0].pk]), f'{n} {nudges._plural(n, "association")} with late fee or interest terms not confirmed',
                  f'{detail}. Until they are confirmed, no late fees or interest are ever ready to charge.')]


def collect(user, today=None):
    """[item] for the Alerts button, most pressing first (amber before plain, then the larger count)."""
    today = today or timezone.localdate()
    items = []
    collectors = [_missing_documents]
    if nudges._is_admin(user):
        collectors += [lambda: nudges._properties(today),
                       lambda: [i for i in nudges._onsite(today) if i['key'] in ONSITE_PROPERTY_KEYS],
                       lambda: [i for i in nudges._financials(today, True) if i['key'] == 'qb'],
                       _terms_not_confirmed]
    for collector in collectors:
        try:
            items += collector()
        except Exception:
            logger.exception('A property alert could not be worked out')
    return sorted(items, key=lambda i: (i['level'] != 'warn', -i['count']))
