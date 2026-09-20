"""The AGREED checkout and check-in times a cleaner needs to plan a turnover.

A booking's own times come from the property's defaults (the platform
calendars carry dates only). A change to either time, once APPROVED
(onsite.GuestRequest) — early or late checkout, early or late check-in —
moves that time. This is the one
place that works out the resulting times and words them, so the cleaner's
link page, the Google Calendar event and the staff visit screen all say
exactly the same thing."""
from datetime import datetime

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from ..models import GuestRequest


def _clock(dt):
    local = timezone.localtime(dt)
    return f'{local.hour % 12 or 12}:{local:%M} {"AM" if local.hour < 12 else "PM"}'


def _approved(booking, kinds):
    """The approved time changes of these kinds, oldest first (a booking has at
    most one live change per time, so the last one is the one that stands)."""
    return [r for r in booking.guest_requests.all() if r.kind in kinds and r.status == GuestRequest.Status.APPROVED]


def _agreed(booking, normal, kinds):
    best = normal
    for r in _approved(booking, kinds):
        best = timezone.make_aware(datetime.combine(normal.date(), r.requested_time))
    return best, best != normal, normal


def agreed_checkout(booking):
    """(datetime, moved, normal): the checkout time to plan around — the
    approved change (earlier or later) if there is one, else the booking's own."""
    return _agreed(booking, timezone.localtime(booking.check_out), GuestRequest.CHECKOUT_KINDS)


def agreed_checkin(booking):
    """(datetime, moved, normal): the approved check-in change (earlier or
    later) if there is one, else the booking's own."""
    return _agreed(booking, timezone.localtime(booking.check_in), GuestRequest.CHECKIN_KINDS)


def checkout_note(moved, agreed, normal):
    if not moved:
        return ''
    if agreed < normal:
        return f' (early checkout approved — normally {_clock(normal)} — you can start when they leave)'
    return f' (late checkout approved — normally {_clock(normal)})'


def checkin_note(moved, agreed, normal):
    if not moved:
        return ''
    if agreed > normal:
        return f' (late check-in approved — normally {_clock(normal)} — more time to clean)'
    return f' (early check-in approved — normally {_clock(normal)})'


def _when(dt, reference_date):
    """'4:00 PM', or 'Sat Oct 5, 4:00 PM' when it isn't on the reference day."""
    local = timezone.localtime(dt)
    if local.date() == reference_date:
        return _clock(local)
    return f'{local:%a %b} {local.day}, {_clock(local)}'


def visit_time_lines(visit):
    """The plain-language lines for a visit's turnover: when the guest leaves
    and when the next one arrives (with any approved change called out).
    Empty for a visit that isn't tied to a reservation (a recurring clean)."""
    if not visit.booking_id:
        return []
    ref = visit.scheduled_date or timezone.localtime(visit.booking.check_out).date()
    lines = []
    out, moved, normal = agreed_checkout(visit.booking)
    lines.append(f'Guest checks out: {_when(out, ref)}{checkout_note(moved, out, normal)}')
    if visit.next_booking_id:
        inn, moved, normal = agreed_checkin(visit.next_booking)
        lines.append(f'Next guest checks in: {_when(inn, ref)}{checkin_note(moved, inn, normal)}')
    else:
        lines.append('No next reservation is on the calendar yet.')
    return lines


def visit_link(visit):
    """The absolute address of the cleaner's link for this visit."""
    return f'{settings.SITE_BASE_URL}{reverse("onsite_visit_public", args=[visit.access_token])}'
