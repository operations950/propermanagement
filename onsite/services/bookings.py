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
from ..models import Booking, Visit, VisitType
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
    'changed'/'reactivated' (lists of RawBooking) and 'cancelled' (list of
    existing Booking rows).

    Cancellation is ALWAYS explicit — driven solely by the row's own Status
    column (RawBooking.is_cancelled, set by the importer as "the word
    'cancel' appears in the Status value"; both Airbnb and VRBO's real
    exports carry one). A booking is never inferred cancelled just because
    it's absent from a re-uploaded file — that heuristic caused two real
    production bugs (a booking that's still active but happens to fall on
    the "other half" of a paginated/partial file — e.g. Airbnb's Page 1 vs
    Page 2 — looked cancelled purely because it wasn't in THIS particular
    file) and the user explicitly asked that absence never be treated as
    evidence of cancellation, for either platform. A cancelled row is never
    treated as new/changed, whether or not it matches an existing Booking.

    'reactivated' is the flip side: a row whose code we already have on
    file as CANCELLED, but which shows up again in a non-cancellation row.
    That means it's actually still active — most concretely, this is how a
    booking wrongly cancelled by the old absence-inference bug (before
    explicit-only detection existed) gets itself corrected: just re-upload
    the file and its code reappears, no manual fix-up needed."""
    existing_by_uid = {
        b.external_uid: b
        for b in Booking.objects.filter(property=property, source=source)
    }
    new_rows, changed_rows, reactivated_rows = [], [], []
    cancelled = []
    for row in raw_bookings:
        existing = existing_by_uid.get(row.external_uid)
        if row.is_cancelled:
            if existing is not None and existing.status == Booking.Status.ACTIVE:
                cancelled.append(existing)
            continue
        if existing is None:
            new_rows.append(row)
        elif existing.status == Booking.Status.CANCELLED:
            reactivated_rows.append(row)
        elif (
            existing.check_in.date() != row.check_in
            or existing.check_out.date() != row.check_out
            # A blank row.listing_name never counts as a "change" (a
            # single-property .ics import has no listing column at all —
            # this must never blank out a listing_name a portfolio CSV
            # already set). This is also how an already-imported Booking
            # from before this field existed picks one up on its very next
            # ordinary re-upload, with no separate backfill needed.
            or (row.listing_name and existing.listing_name != row.listing_name)
        ):
            changed_rows.append(row)

    return {'new': new_rows, 'changed': changed_rows, 'reactivated': reactivated_rows, 'cancelled': cancelled}


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
    property's rows. Returns (new_count, changed_count, reactivated_count,
    cancelled_count, visit_note) — visit_note is a user-facing message when
    visit creation had to be skipped. Does not touch any ImportBatch; a
    portfolio-wide import calls this once per resolved property and
    aggregates the counts itself (see onsite/views.py)."""
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
            listing_name=row.listing_name,
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
        if row.listing_name:
            booking.listing_name = row.listing_name
        booking.last_seen_at = timezone.now()
        booking.save(update_fields=['check_in', 'check_out', 'listing_name', 'last_seen_at'])
        visit = booking.visits.exclude(status__in=['submitted', 'verified', 'cancelled']).first()
        if visit:
            next_booking = _find_next_booking(property, booking.check_out, exclude_pk=booking.pk)
            visit.scheduled_date = booking.check_out.date()
            visit.next_booking = next_booking
            visit.ready_by = next_booking.check_in if next_booking else None
            visit.save(update_fields=['scheduled_date', 'next_booking', 'ready_by'])
            transaction.on_commit(lambda visit=visit: push_visit(visit))

    for row in diff['reactivated']:
        booking = Booking.objects.get(property=property, source=source, external_uid=row.external_uid)
        booking.status = Booking.Status.ACTIVE
        booking.check_in = _combine(property, row.check_in, 'check_in')
        booking.check_out = _combine(property, row.check_out, 'check_out')
        if row.listing_name:
            booking.listing_name = row.listing_name
        booking.last_seen_at = timezone.now()
        booking.save(update_fields=['status', 'check_in', 'check_out', 'listing_name', 'last_seen_at'])

        next_booking = _find_next_booking(property, booking.check_out, exclude_pk=booking.pk)
        cancelled_visit = booking.visits.filter(status=Visit.Status.CANCELLED).order_by('-pk').first()
        if cancelled_visit:
            # Bring the same Visit back rather than creating a duplicate —
            # its checklist/assignee history is still intact underneath the
            # cancellation, exactly as it was before.
            cancelled_visit.status = (
                Visit.Status.SCHEDULED
                if cancelled_visit.assigned_staff_id or cancelled_visit.assigned_contact_id
                else Visit.Status.UNASSIGNED
            )
            cancelled_visit.scheduled_date = booking.check_out.date()
            cancelled_visit.next_booking = next_booking
            cancelled_visit.ready_by = next_booking.check_in if next_booking else None
            cancelled_visit.save(update_fields=['status', 'scheduled_date', 'next_booking', 'ready_by'])
            transaction.on_commit(lambda visit=cancelled_visit: push_visit(visit))
        elif turnover_type:
            # No Visit at all survived (shouldn't normally happen, but
            # don't leave a reactivated booking with no cleaning scheduled).
            create_visit(
                property, turnover_type, booking=booking, next_booking=next_booking,
                scheduled_date=row.check_out, ready_by=next_booking.check_in if next_booking else None,
            )

    for booking in diff['cancelled']:
        booking.status = Booking.Status.CANCELLED
        booking.save(update_fields=['status'])
        active_visits = list(booking.visits.exclude(status__in=['submitted', 'verified', 'cancelled']))
        booking.visits.filter(pk__in=[v.pk for v in active_visits]).update(status='cancelled')
        for visit in active_visits:
            transaction.on_commit(lambda visit=visit: delete_visit_event(visit))

    return len(diff['new']), len(diff['changed']), len(diff['reactivated']), len(diff['cancelled']), visit_note
