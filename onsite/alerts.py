"""Everything on On-Site that needs a look, in one list for the Alerts button on every On-Site screen (the plum button at the right of the tabs).

  * reservations to review (overlapping stays, upcoming bookings with no payout)            everyone
  * booking calendar lines that are not connected or need attention                       admins
  * Google Calendar is not accepting visit updates                                         admins
  * today: check-outs with no cleaning visit, visits with nobody on them, visits at risk   everyone

The reminders about the PROPERTIES (listing links, ratings, size details, trash schedules) are not here: they are on the Properties screen's Alerts button (core/property_alerts.py).
Each item links to where it is dealt with. A problem working out one of them never takes a page down."""
import logging
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger(__name__)


def _plural(n, one, many=None):
    return one if n == 1 else (many or one + 's')


def _item(key, count, level, icon, url, text, detail=''):
    return {'section': 'onsite', 'key': key, 'count': count, 'level': level, 'icon': icon, 'url': url, 'text': text, 'detail': detail}


def _reservations(user, today):
    from .services import review
    overlaps, unpaid = review.counts(today)
    n = overlaps + unpaid
    if not n:
        return []
    return [_item('reservations', n, 'warn', 'calendar-check', reverse('onsite_reservation_review'), f'{n} {_plural(n, "reservation")} to review',
                  'Overlapping stays, or upcoming bookings with no payout.')]


def _feeds(user, today):
    from core.permissions import has_admin
    if not has_admin(user):
        return []
    from .services import feeds
    attention = feeds.feeds_needing_attention()
    if not attention:
        return []
    n = len(attention)
    return [_item('feeds', n, 'warn', 'calendar-sync', reverse('onsite_booking_feeds'), f'{n} booking calendar {_plural(n, "line")} not connected or {_plural(n, "needs", "need")} attention',
                  'Reservations from those listings may be missing from the boards.')]


def _google(user, today):
    from core.permissions import has_admin
    if not has_admin(user):
        return []
    from . import google_calendar_push
    status = google_calendar_push.status()
    if status.get('ok'):
        return []
    return [_item('google', 1, 'warn', 'calendar-x', reverse('admin_tools'), "Google Calendar isn't updating", status.get('message', ''))]


def _today(user, today):
    from .models import Booking, Visit
    from .services import checkouts, coverage
    from .views import _visit_status          # the same "at risk" rule the Visits page uses
    now = timezone.now()
    items = []
    todays = list(Visit.objects.filter(scheduled_date=today).exclude(status__in=[Visit.Status.CANCELLED, Visit.Status.SKIPPED]).select_related('property', 'visit_type'))
    checkouts_today = (Booking.objects.filter(check_out__date=today, status=Booking.Status.ACTIVE, property__is_general=False).select_related('property').prefetch_related('visits'))
    missing = checkouts.without_visit(coverage.operational(checkouts_today))
    if missing:
        n = len(missing)
        items.append(_item('no_visit', n, 'warn', 'sparkles', f'{reverse("onsite_visit_create")}?property={missing[0].property_id}',
                           f'Today: {n} check-out{"" if n == 1 else "s"} with no cleaning scheduled', ', '.join(sorted({b.property.name for b in missing})[:6])))
    unassigned = [v for v in todays if v.status == Visit.Status.UNASSIGNED]
    if unassigned:
        n = len(unassigned)
        items.append(_item('unassigned', n, 'warn', 'user-x', reverse('onsite_visit_detail', args=[unassigned[0].pk]),
                           f'Today: {n} {_plural(n, "visit")} with nobody on {"it" if n == 1 else "them"}', ', '.join(sorted({v.property.name for v in unassigned})[:6])))
    at_risk = [v for v in todays if _visit_status(v, now) == 'at_risk']
    if at_risk:
        n = len(at_risk)
        items.append(_item('at_risk', n, 'warn', 'alarm-clock', reverse('onsite_visit_detail', args=[at_risk[0].pk]), f'Today: {n} {_plural(n, "visit")} at risk of not being ready',
                           ', '.join(sorted({v.property.name for v in at_risk})[:6])))
    return items


COLLECTORS = (_reservations, _feeds, _google, _today)


def collect(user, today=None):
    """[item] for the On-Site Alerts button, most pressing first (amber before plain, then the larger count)."""
    today = today or timezone.localdate()
    items = []
    for collector in COLLECTORS:
        try:
            items += collector(user, today)
        except Exception:
            logger.exception('An On-Site alert could not be worked out')
    return sorted(items, key=lambda i: (i['level'] != 'warn', -i['count']))
