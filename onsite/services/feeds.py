"""Polls each BookingFeed (an Airbnb/VRBO calendar link) and applies what it
shows through the same apply_bookings_for_property the file import uses, so
a reservation appearing, moving or disappearing on the platform creates,
moves or cancels its turnover visit (and calendar event) with no upload.

Matching a calendar event to a booking we already have, in order:
  1. the platform's confirmation code (recovered from the event when the
     platform includes it — Airbnb does) or the event's own UID;
  2. failing that, an active booking on this listing with exactly the same
     check-in and check-out dates (e.g. one a CSV report brought in under
     a confirmation code the calendar doesn't show);
  3. otherwise it's new.
This is what stops one reservation becoming two bookings and two cleanings.

A reservation the feed no longer shows is treated as cancelled ONLY under
strict conditions — the app's rule elsewhere is that absence from a file is
never evidence of cancellation (a partial/paginated CSV made live bookings
look cancelled), and a live feed can glitch the same way:
  - the feed downloaded fine and really is a calendar, and shows at least
    one upcoming reservation;
  - the booking hasn't started (in-progress and past stays are never
    touched) and falls within the span of dates the feed actually shows
    (a feed that only reaches N months out says nothing about later ones);
  - no entry at all — reservation OR block — sits on exactly its dates;
  - it has been missing on BOOKING_FEED_MISSING_POLLS_BEFORE_CANCEL
    consecutive successful polls in a row (default 2), so one bad response
    can't cancel anything;
  - and it isn't part of a mass disappearance: if more than
    MASS_CANCEL_MIN vanish at once and they're at least half of the
    upcoming bookings, nothing is cancelled and the feed is flagged for a
    person to confirm (see poll_feed's allow_mass_cancel).
A cancellation then goes through the normal cancel path (visit cancelled,
calendar event deleted); if the reservation reappears it's reactivated."""
import ipaddress
import logging
import socket
import threading
import traceback
from datetime import timedelta
from urllib.parse import urljoin, urlparse

import requests
from django.conf import settings
from django.utils import timezone

from ..feed_ics import parse_feed_ics
from ..importers import BookingFileError, RawBooking
from ..models import Booking, BookingFeed
from .bookings import apply_bookings_for_property, update_feed_health

logger = logging.getLogger(__name__)

MAX_FEED_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 3
MASS_CANCEL_MIN = 5
STALE_AFTER = timedelta(hours=3)

# One poll at a time: the timer and a "Check now" click could otherwise
# race and each create the same new booking.
_poll_lock = threading.RLock()


class FeedError(Exception):
    """A problem talking to the platform. The message is written for staff
    and never contains the feed link (it's a secret)."""


def _assert_public_host(url):
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise FeedError('That link is not a web address.')
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80))
    except OSError:
        raise FeedError("Couldn't look up the calendar's web address.") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise FeedError('That link points at a private address, not a booking platform.')


def fetch_feed(url):
    """The calendar's bytes. Follows a few redirects, re-checking every hop
    is a public address; refuses anything huge."""
    current = url
    try:
        for _ in range(MAX_REDIRECTS + 1):
            _assert_public_host(current)
            resp = requests.get(
                current, timeout=(5, 20), allow_redirects=False, stream=True,
                headers={'User-Agent': 'ProperManagement-calendar-sync/1.0', 'Accept': 'text/calendar, */*'},
            )
            if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get('Location'):
                current = urljoin(current, resp.headers['Location'])
                resp.close()
                continue
            resp.raise_for_status()
            data = b''
            for chunk in resp.iter_content(65536):
                data += chunk
                if len(data) > MAX_FEED_BYTES:
                    resp.close()
                    raise FeedError('The calendar was unexpectedly large.')
            resp.close()
            return data
    except FeedError:
        raise
    except requests.RequestException as exc:
        status = getattr(getattr(exc, 'response', None), 'status_code', None)
        raise FeedError(
            f"Couldn't download the calendar (HTTP {status})." if status else "Couldn't reach the booking platform."
        ) from None
    raise FeedError('The calendar link redirected too many times.')


def _local_date(dt):
    return timezone.localtime(dt).date()


def _resolve_uid(feed, event, taken):
    """Which external_uid this event should be filed under — see the module
    docstring's matching order."""
    candidates = [c for c in (event.code, event.uid) if c]
    known = set(
        Booking.objects.filter(source=feed.source, external_uid__in=candidates).values_list('external_uid', flat=True)
    )
    for candidate in candidates:
        if candidate in known:
            return candidate
    twins = [
        b for b in Booking.objects.filter(
            property=feed.property, unit=feed.unit, source=feed.source, status=Booking.Status.ACTIVE,
        ).exclude(external_uid__in=taken)
        if _local_date(b.check_in) == event.check_in and _local_date(b.check_out) == event.check_out
    ]
    if len(twins) == 1:
        return twins[0].external_uid
    return event.code or event.uid


def _poll(feed, allow_mass_cancel):
    now = timezone.now()
    today = timezone.localdate()
    events = parse_feed_ics(fetch_feed(feed.url))
    live = [e for e in events if e.is_reservation and e.check_out > e.check_in and e.check_out >= today]
    every_entry_dates = {(e.check_in, e.check_out) for e in events}

    rows, seen = [], set()
    for event in live:
        uid = _resolve_uid(feed, event, seen)
        if uid in seen:
            continue
        seen.add(uid)
        rows.append(RawBooking(
            external_uid=uid, check_in=event.check_in, check_out=event.check_out, guest_phone_last4=event.phone_last4,
        ))

    upcoming = list(Booking.objects.filter(
        property=feed.property, unit=feed.unit, source=feed.source, status=Booking.Status.ACTIVE, check_in__gt=now,
    ))
    window_end = max((e.check_out for e in live), default=None)
    missing = []
    if window_end:
        for booking in upcoming:
            if booking.external_uid in seen:
                continue
            check_in, check_out = _local_date(booking.check_in), _local_date(booking.check_out)
            if check_in > window_end or (check_in, check_out) in every_entry_dates:
                continue
            missing.append(booking)

    previous = feed.missing_streak or {}
    streak = {b.external_uid: previous.get(b.external_uid, 0) + 1 for b in missing}
    threshold = settings.BOOKING_FEED_MISSING_POLLS_BEFORE_CANCEL
    due = [b for b in missing if streak[b.external_uid] >= threshold]
    held = 0
    if due and not allow_mass_cancel and len(due) > MASS_CANCEL_MIN and len(due) * 2 >= len(upcoming):
        held, due = len(due), []
    for booking in due:
        rows.append(RawBooking(
            external_uid=booking.external_uid, check_in=_local_date(booking.check_in),
            check_out=_local_date(booking.check_out), is_cancelled=True,
        ))
        streak.pop(booking.external_uid, None)

    new = changed = reactivated = cancelled = 0
    note = ''
    if rows:
        new, changed, reactivated, cancelled, note = apply_bookings_for_property(
            feed.property, feed.source, rows, default_unit=feed.unit, from_feed=True,
        )
    update_feed_health(feed.source, [r for r in rows if not r.is_cancelled])

    feed.missing_streak = streak
    feed.cancellations_held = held
    summary = (
        f'{len(live)} upcoming reservation{"" if len(live) == 1 else "s"} — '
        f'{new} new, {changed} changed, {reactivated} restored, {cancelled} cancelled.'
    )
    if held:
        summary += (
            f' {held} reservations vanished from the calendar at once, so none were cancelled automatically — '
            'check the calendar, then use "Check now and apply cancellations" if they really are gone.'
        )
    if note:
        summary += f' {note}'
    return summary[:255]


def poll_feed(feed, allow_mass_cancel=False):
    """Fetch, apply and record the outcome on the feed. Never raises.
    allow_mass_cancel is only for a person confirming, on the feeds screen,
    that a burst of vanished reservations really is a burst of cancellations
    (a hurricane, a platform-wide cancellation)."""
    with _poll_lock:
        now = timezone.now()
        feed.last_polled_at = now
        try:
            feed.last_summary = _poll(feed, allow_mass_cancel)
            feed.last_success_at, feed.last_error = now, ''
        except (FeedError, BookingFileError) as exc:
            feed.last_error = str(exc)[:255]
            logger.warning('Booking feed %s (%s) poll failed: %s', feed.pk, feed.label(), feed.last_error)
        except Exception as exc:
            feed.last_error = 'Something went wrong reading this calendar — it will be retried.'
            # Frames only, not the exception message: an error out of the
            # HTTP layer would carry the feed link, which is a secret.
            logger.error(
                'Booking feed %s (%s) poll crashed: %s\n%s', feed.pk, feed.label(), type(exc).__name__,
                ''.join(traceback.format_tb(exc.__traceback__)),
            )
        feed.save(update_fields=[
            'last_polled_at', 'last_success_at', 'last_error', 'last_summary', 'missing_streak', 'cancellations_held',
        ])
    return feed


def poll_all():
    feeds = list(BookingFeed.objects.filter(is_active=True).select_related('property', 'unit'))
    for feed in feeds:
        poll_feed(feed)
    return feeds


def feeds_needing_attention():
    """Active feeds an admin should look at: the last poll failed, it hasn't
    succeeded in hours, or cancellations are being held for confirmation."""
    cutoff = timezone.now() - STALE_AFTER
    result = []
    for feed in BookingFeed.objects.filter(is_active=True).select_related('property', 'unit'):
        stale = feed.last_polled_at is not None and (feed.last_success_at is None or feed.last_success_at < cutoff)
        if feed.last_error or feed.cancellations_held or stale:
            result.append(feed)
    return result
