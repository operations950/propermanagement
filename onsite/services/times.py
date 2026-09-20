"""The AGREED checkout and check-in times a cleaner needs to plan a turnover.

A booking's own times come from the property's defaults (the platform
calendars carry dates only). A guest's early check-in / late checkout,
once APPROVED (onsite.GuestRequest), moves that time. This is the one
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


def _approved(booking, kind):
    return [r for r in booking.guest_requests.all() if r.kind == kind and r.status == GuestRequest.Status.APPROVED]


def agreed_checkout(booking):
    """(datetime, moved, normal): the checkout time to plan around — the
    latest approved late checkout, else the booking's own."""
    normal = timezone.localtime(booking.check_out)
    best = normal
    for r in _approved(booking, GuestRequest.Kind.LATE_CHECKOUT):
        candidate = timezone.make_aware(datetime.combine(normal.date(), r.requested_time))
        if candidate > best:
            best = candidate
    return best, best != normal, normal


def agreed_checkin(booking):
    """(datetime, moved, normal): the earliest approved early check-in, else
    the booking's own."""
    normal = timezone.localtime(booking.check_in)
    best = normal
    for r in _approved(booking, GuestRequest.Kind.EARLY_CHECKIN):
        candidate = timezone.make_aware(datetime.combine(normal.date(), r.requested_time))
        if candidate < best:
            best = candidate
    return best, best != normal, normal


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
    line = f'Guest checks out: {_when(out, ref)}'
    if moved:
        line += f' (late checkout approved — normally {_clock(normal)})'
    lines.append(line)
    if visit.next_booking_id:
        inn, moved, normal = agreed_checkin(visit.next_booking)
        line = f'Next guest checks in: {_when(inn, ref)}'
        if moved:
            line += f' (early check-in approved — normally {_clock(normal)})'
        lines.append(line)
    else:
        lines.append('No next reservation is on the calendar yet.')
    return lines


def visit_link(visit):
    """The absolute address of the cleaner's link for this visit."""
    return f'{settings.SITE_BASE_URL}{reverse("onsite_visit_public", args=[visit.access_token])}'
