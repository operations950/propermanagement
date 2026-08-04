"""Booking-import diff/apply logic — the two-phase preview/apply flow
described in ONSITE_DESIGN.md's "Booking import" section. Kept separate
from services/checklist.py, which is about the checklist itself rather than
where a Visit comes from.

TURNOVER_SLUG names the VisitType a new booking spawns a Visit for — seeded
by the seed_checklist_templates management command (Phase 6). If it hasn't
been seeded yet (or was deactivated), Booking rows still import cleanly;
only visit creation is skipped, with a clear message back to the caller
rather than a crash — the same "degrade, don't break" house style used for
every other integration in this app."""
from datetime import datetime, time

from django.db import transaction
from django.utils import timezone

from .checklist import create_visit
from ..google_calendar_push import delete_visit_event, push_visit
from ..models import Booking, VisitType
from core.models import PropertyListingName

TURNOVER_SLUG = 'turnover'
DEFAULT_CHECK_IN_TIME = time(16, 0)
DEFAULT_CHECK_OUT_TIME = time(11, 0)


def _combine(property, date_value, which):
    if which == 'check_in':
        t = property.default_check_in_time or DEFAULT_CHECK_IN_TIME
    else:
        t = property.default_check_out_time or DEFAULT_CHECK_OUT_TIME
    naive = datetime.combine(date_value, t)
    return timezone.make_aware(naive) if timezone.is_naive(naive) else naive


def resolve_listing_names(raw_bookings, source):
    """Splits a portfolio-wide file's rows by whether their listing_name
    matches a stored PropertyListingName for this platform. Returns
    (matched: {Property: [RawBooking]}, unmatched: {listing_name: [RawBooking]})
    — unmatched is grouped by the exact listing_name string so a human
    resolves each distinct name once, not once per row. A property can
    have any number of listing names on file (e.g. several units at one
    address, not yet modeled as separate Property rows) — the name side
    is what's unique, not the property side."""
    names_seen = {r.listing_name for r in raw_bookings if r.listing_name}
    properties_by_name = {
        row.name: row.property
        for row in PropertyListingName.objects.filter(platform=source, name__in=names_seen).select_related('property')
    }
    matched, unmatched = {}, {}
    for row in raw_bookings:
        property = properties_by_name.get(row.listing_name)
        if property:
            matched.setdefault(property, []).append(row)
        else:
            unmatched.setdefault(row.listing_name, []).append(row)
    return matched, unmatched


def check_listing_name_conflict(property, source, listing_name):
    """Returns None if mapping listing_name -> property for this platform is
    conflict-free, otherwise a dict describing what to warn about:
    - {'type': 'cross', 'other_property': Property} — a DIFFERENT property
      already claims this exact name; must be fixed there first (hard block
      — the DB's unique constraint would reject it anyway).
    - {'type': 'additional', 'existing_names': [str, ...]} — this property
      already answers to other name(s) on this platform; adding one more is
      the normal multi-unit case, but still worth a confirmation click in
      case the wrong property got picked by accident (soft block)."""
    other = PropertyListingName.objects.filter(platform=source, name=listing_name).exclude(property=property).first()
    if other:
        return {'type': 'cross', 'other_property': other.property}
    existing_names = list(
        PropertyListingName.objects.filter(property=property, platform=source)
        .exclude(name=listing_name).values_list('name', flat=True),
    )
    if existing_names:
        return {'type': 'additional', 'existing_names': existing_names}
    return None


def save_listing_name(property, source, listing_name):
    PropertyListingName.objects.get_or_create(property=property, platform=source, name=listing_name)


def diff_bookings(property, source, raw_bookings):
    """Read-only preview diff — nothing written. Returns a dict with 'new'/
    'changed' (lists of RawBooking) and 'cancelled' (list of existing
    Booking rows the file's date range no longer contains)."""
    existing_by_uid = {
        b.external_uid: b
        for b in Booking.objects.filter(property=property, source=source)
    }
    seen_uids = set()
    new_rows, changed_rows = [], []
    for row in raw_bookings:
        seen_uids.add(row.external_uid)
        existing = existing_by_uid.get(row.external_uid)
        if existing is None:
            new_rows.append(row)
        elif existing.check_in.date() != row.check_in or existing.check_out.date() != row.check_out:
            changed_rows.append(row)

    # A real Airbnb/VRBO ICS calendar export lists every future reservation
    # from today onward, however far out — a booking that's genuinely still
    # on the books would still appear in it. So a booking absent from the
    # new file is presumed cancelled as long as its checkout falls between
    # today and the furthest-out checkout date we know about (either from
    # this file or from what we already had on record) — bounding by only
    # this file's own rows would wrongly ignore an existing booking further
    # out than anything the new file happens to contain (e.g. everything
    # else on the books was already checked out, leaving this file with a
    # nearer max date than a genuinely-cancelled later booking's).
    known_checkouts = [r.check_out for r in raw_bookings] + [
        b.check_out.date() for b in existing_by_uid.values() if b.status == Booking.Status.ACTIVE
    ]
    if not known_checkouts:
        return {'new': [], 'changed': [], 'cancelled': []}

    covers_start = timezone.localdate()
    covers_end = max(known_checkouts)
    cancelled = [
        b for uid, b in existing_by_uid.items()
        if uid not in seen_uids
        and b.status == Booking.Status.ACTIVE
        and covers_start <= b.check_out.date() <= covers_end
    ]
    return {'new': new_rows, 'changed': changed_rows, 'cancelled': cancelled}


def _find_next_booking(property, after_datetime, exclude_pk=None):
    qs = Booking.objects.filter(
        property=property, status=Booking.Status.ACTIVE, check_in__gte=after_datetime,
    ).order_by('check_in')
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    return qs.first()


@transaction.atomic
def apply_bookings_for_property(property, source, raw_bookings):
    """Writes the diff computed the same way diff_bookings does, for ONE
    property's rows. Returns (new_count, changed_count, cancelled_count,
    visit_note) — visit_note is a user-facing message when visit creation
    had to be skipped. Does not touch any ImportBatch; a portfolio-wide
    import calls this once per resolved property and aggregates the counts
    itself (see onsite/views.py)."""
    diff = diff_bookings(property, source, raw_bookings)
    turnover_type = VisitType.objects.filter(slug=TURNOVER_SLUG, is_active=True).first()
    visit_note = '' if turnover_type else (
        'Bookings were imported, but no active "Turnover" visit type exists yet — no visits were '
        'created. Run seed_checklist_templates, or create one manually, then re-import.'
    )

    for row in diff['new']:
        check_in_dt = _combine(property, row.check_in, 'check_in')
        check_out_dt = _combine(property, row.check_out, 'check_out')
        booking = Booking.objects.create(
            property=property, source=source, external_uid=row.external_uid,
            guest_name=row.guest_name, guest_phone_last4=row.guest_phone_last4,
            check_in=check_in_dt, check_out=check_out_dt, last_seen_at=timezone.now(),
        )
        if turnover_type:
            next_booking = _find_next_booking(property, check_out_dt, exclude_pk=booking.pk)
            create_visit(
                property, turnover_type, booking=booking, next_booking=next_booking,
                scheduled_date=row.check_out, ready_by=next_booking.check_in if next_booking else None,
            )

    for row in diff['changed']:
        booking = Booking.objects.get(property=property, source=source, external_uid=row.external_uid)
        booking.check_in = _combine(property, row.check_in, 'check_in')
        booking.check_out = _combine(property, row.check_out, 'check_out')
        booking.last_seen_at = timezone.now()
        booking.save(update_fields=['check_in', 'check_out', 'last_seen_at'])
        visit = booking.visits.exclude(status__in=['submitted', 'verified', 'cancelled']).first()
        if visit:
            next_booking = _find_next_booking(property, booking.check_out, exclude_pk=booking.pk)
            visit.scheduled_date = booking.check_out.date()
            visit.next_booking = next_booking
            visit.ready_by = next_booking.check_in if next_booking else None
            visit.save(update_fields=['scheduled_date', 'next_booking', 'ready_by'])
            transaction.on_commit(lambda visit=visit: push_visit(visit))

    for booking in diff['cancelled']:
        booking.status = Booking.Status.CANCELLED
        booking.save(update_fields=['status'])
        active_visits = list(booking.visits.exclude(status__in=['submitted', 'verified', 'cancelled']))
        booking.visits.filter(pk__in=[v.pk for v in active_visits]).update(status='cancelled')
        for visit in active_visits:
            transaction.on_commit(lambda visit=visit: delete_visit_event(visit))

    return len(diff['new']), len(diff['changed']), len(diff['cancelled']), visit_note
