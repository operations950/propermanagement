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
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import coverage
from . import payouts as payouts_service
from .checklist import create_visit
from ..google_calendar_push import delete_visit_event, push_visit
from ..models import Booking, BookingFeedHealth, PayoutBatch, PayoutItem, PayoutLine, Visit, VisitType
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
    address) — the name side is what's unique, not the property side. Which
    specific Unit a name maps to (if any) is resolved separately, per-row,
    inside apply_bookings_for_property via PropertyListingName.unit — this
    function only groups by Property, since that's still what a human needs
    to confirm/resolve for an unmatched name."""
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


def save_listing_name(property, source, listing_name, unit=None):
    listing, created = PropertyListingName.objects.get_or_create(
        property=property, platform=source, name=listing_name, defaults={'unit': unit},
    )
    if not created and listing.unit_id != (unit.pk if unit else None):
        listing.unit = unit
        listing.save(update_fields=['unit'])


def _listing_unit_map(property, source):
    """Every listing name this property answers to on this platform, mapped
    to its Unit (or None) — see apply_bookings_for_property."""
    return {
        pln.name: pln.unit
        for pln in PropertyListingName.objects.filter(property=property, platform=source).select_related('unit')
    }


def _needs_visit(check_out_date):
    """Only a stay that has not yet ended needs a cleaning. A payout report
    lists a year of finished stays; turning each into an overdue, unassigned
    visit (and a calendar event) would bury the real work."""
    return check_out_date >= timezone.localdate()


def _cleaning_allowed(property_id, unit_id, source, from_calendar, covered, on_calendar=False):
    """A cleaning is made only for a stay the calendar shows. Where a listing's
    calendar is connected and healthy, a reservation that only a payment report
    knows about is a financial record: no cleaning, no board row. Where there is
    no working calendar yet, imports keep scheduling cleanings as before."""
    return bool(
        from_calendar or on_calendar or source == Booking.Source.MANUAL
        or (property_id, unit_id, source) not in covered
    )


def _record_paid_cancellations(property, source, raw_bookings, listing_unit_map, default_unit=None):
    """A cancelled reservation the guest was still charged for shows up in a
    payout report with money attached. If we never had that reservation, keep
    a cancelled record of it - no visit, no occupancy - so the payout can be
    reconciled and the cancellation counted."""
    for row in raw_bookings:
        if not (row.is_cancelled and row.payout_amount and row.payout_amount > 0):
            continue
        if Booking.objects.filter(source=source, external_uid=row.external_uid).exists():
            continue
        Booking.objects.create(
            property=property, unit=listing_unit_map.get(row.listing_name, default_unit), source=source,
            external_uid=row.external_uid, guest_name=row.guest_name, listing_name=row.listing_name,
            status=Booking.Status.CANCELLED, last_seen_at=timezone.now(),
            check_in=_combine(property, row.check_in, 'check_in'), check_out=_combine(property, row.check_out, 'check_out'),
        )


def _feed_twin(property, source, row, listing_unit_map, default_unit=None):
    """The booking a feed poll already created for this same reservation
    under a different key. A polled calendar (onsite/services/feeds.py) can
    only key a booking by the calendar's own UID unless it can recover the
    platform's confirmation code; a later CSV report then arrives with the
    real code for the same stay. Same property, same platform, same unit,
    same check-in and check-out dates, and exactly one such booking, means
    it is the same reservation — never a second one to clean. (The unit
    matters: two units of one building often check out the same day.) None
    when there's no candidate or it's ambiguous."""
    if row.is_cancelled:
        return None
    unit = listing_unit_map.get(row.listing_name, default_unit)
    matches = [
        b for b in Booking.objects.filter(
            Q(status=Booking.Status.ACTIVE) | Q(manually_cancelled=True),
            Q(from_feed=True) | Q(on_calendar=True),
            property=property, source=source, unit=unit,
        ).exclude(external_uid=row.external_uid)
        if timezone.localtime(b.check_in).date() == row.check_in and timezone.localtime(b.check_out).date() == row.check_out
    ]
    return matches[0] if len(matches) == 1 else None


def _adopt_feed_twins(property, source, raw_bookings, listing_unit_map, default_unit=None):
    """Gives each feed-created booking the confirmation code its CSV row
    carries (see _feed_twin), so everything downstream just finds it by UID."""
    # One query for every row's existence check instead of one per row — on a
    # large file (hundreds/thousands of rows) the per-row version was slow
    # enough on its own to help push a production import past its worker
    # timeout (see apply_bookings_for_property's docstring / commit note).
    existing_uids = set(Booking.objects.filter(
        source=source, external_uid__in=[row.external_uid for row in raw_bookings],
    ).values_list('external_uid', flat=True))
    for row in raw_bookings:
        if row.external_uid in existing_uids:
            continue
        twin = _feed_twin(property, source, row, listing_unit_map, default_unit)
        if twin:
            twin.external_uid = row.external_uid
            twin.from_feed = False
            twin.save(update_fields=['external_uid', 'from_feed'])


def update_feed_health(source, raw_bookings):
    """Called once a batch (or feed poll) for `source` actually applies
    successfully — see BookingFeedHealth's docstring for what each field
    means and why they're kept separate. All three only ever move forward."""
    health, _ = BookingFeedHealth.objects.get_or_create(source=source)
    update_fields = ['last_upload_at']
    health.last_upload_at = timezone.now()

    booked_dates = [r.booked_at for r in raw_bookings if r.booked_at]
    if booked_dates:
        newest = max(booked_dates)
        if not health.newest_booked_date or newest > health.newest_booked_date:
            health.newest_booked_date = newest
            update_fields.append('newest_booked_date')

    payouts_service.note_coverage(source, [getattr(r, 'payout_date', None) for r in raw_bookings])

    checkouts = [r.check_out for r in raw_bookings]
    if checkouts:
        furthest = max(checkouts)
        if not health.coverage_through or furthest > health.coverage_through:
            health.coverage_through = furthest
            update_fields.append('coverage_through')

    health.save(update_fields=update_fields)


def _save_amounts(source, raw_bookings):
    """Stores the money a report carried onto the bookings it describes. A
    value only ever moves UP: a long stay's payout report lists just the
    installments still pending, so a later, partial file must never shrink a
    figure an earlier one gave in full. Fields the file left blank are left
    alone."""
    # Batched once for the whole file rather than one SELECT per row — see
    # _adopt_feed_twins' comment; this loop is the other big multiplier on a
    # large import (every row with any money on it did its own query here).
    candidates = [row for row in raw_bookings if row.has_amounts() and not (row.is_cancelled and not (row.payout_amount and row.payout_amount > 0))]
    bookings_by_uid = {
        b.external_uid: b
        for b in Booking.objects.filter(source=source, external_uid__in=[row.external_uid for row in candidates])
    }
    payout_line_writes = {}    # (booking_id, kind, date) -> amount, collected here and written once below
    for row in candidates:
        booking = bookings_by_uid.get(row.external_uid)
        if booking is None:
            continue
        changed = []
        for field in ('gross_amount', 'payout_amount', 'cleaning_fee', 'other_fees', 'tax_amount', 'platform_fee'):
            new_value = getattr(row, field)
            old_value = getattr(booking, field)
            if new_value is not None and (old_value is None or new_value > old_value):
                setattr(booking, field, new_value)
                changed.append(field)
        # The pass-through tax and other cash lines are sums for this code, taken as the file gives them
        # (they can be negative, so "only ever moves up" doesn't apply).
        for field in ('pass_through_amount', 'other_payout_amount'):
            new_value = getattr(row, field)
            if new_value is not None and getattr(booking, field) != new_value:
                setattr(booking, field, new_value)
                changed.append(field)
        # Each dated line is stored as the file gives it (a later file for the same day corrects it, other
        # days stay). Two rows in the same file landing on the same (booking, kind, date) — a duplicated CSV
        # line — just means the later one wins, same as re-uploading the file twice would.
        for kind, paid, amount in row.payout_lines:
            payout_line_writes[(booking.pk, kind, paid)] = amount
        if row.payout_date and booking.payout_date != row.payout_date:
            booking.payout_date = row.payout_date
            changed.append('payout_date')
        if row.payout_date and booking.payout_amount is not None:
            status = payouts_service.status_for(row.payout_date)
            if booking.payout_status != status:
                booking.payout_status = status
                changed.append('payout_status')
        if changed:
            booking.amount_source = 'csv upload'
            booking.save(update_fields=changed + ['amount_source'])

    # Written as one batch instead of a get-or-create round trip per dated line — on a file covering a long
    # stay's many installments, or two years of history, this was the single biggest query multiplier of all
    # (it's what actually pushed a large real import past its worker timeout in production).
    if payout_line_writes:
        booking_ids = {key[0] for key in payout_line_writes}
        existing = {
            (pl.booking_id, pl.kind, pl.date): pl
            for pl in PayoutLine.objects.filter(booking_id__in=booking_ids)
        }
        to_create, to_update = [], []
        for (booking_id, kind, date), amount in payout_line_writes.items():
            line = existing.get((booking_id, kind, date))
            if line is None:
                to_create.append(PayoutLine(booking_id=booking_id, kind=kind, date=date, amount=amount))
            elif line.amount != amount:
                line.amount = amount
                to_update.append(line)
        if to_create:
            PayoutLine.objects.bulk_create(to_create)
        if to_update:
            PayoutLine.objects.bulk_update(to_update, ['amount'])


def save_money_only(source, money_rows):
    """Dated money lines for reservations whose own row was not in the file (see importers.ParsedBookings): added
    to the booking on record, if there is one. Nothing is created for a code we don't know."""
    money_rows = list(money_rows or ())
    bookings_by_uid = {
        b.external_uid: b
        for b in Booking.objects.filter(source=source, external_uid__in=[row.external_uid for row in money_rows])
    }
    payout_line_writes = {}
    for row in money_rows:
        booking = bookings_by_uid.get(row.external_uid)
        if booking is None:
            continue
        for kind, paid, amount in row.payout_lines:
            payout_line_writes[(booking.pk, kind, paid)] = amount
    if payout_line_writes:
        booking_ids = {key[0] for key in payout_line_writes}
        existing = {
            (pl.booking_id, pl.kind, pl.date): pl
            for pl in PayoutLine.objects.filter(booking_id__in=booking_ids)
        }
        to_create, to_update = [], []
        for (booking_id, kind, date), amount in payout_line_writes.items():
            line = existing.get((booking_id, kind, date))
            if line is None:
                to_create.append(PayoutLine(booking_id=booking_id, kind=kind, date=date, amount=amount))
            elif line.amount != amount:
                line.amount = amount
                to_update.append(line)
        if to_create:
            PayoutLine.objects.bulk_create(to_create)
        if to_update:
            PayoutLine.objects.bulk_update(to_update, ['amount'])

    # Summed from one query across every affected booking, not one payout_lines.all() query PER
    # booking (that refetch-per-booking loop was its own O(n) - the exact mistake this whole fix
    # is about, just reintroduced one line down).
    sums = {}   # booking_id -> {kind: total}
    if bookings_by_uid:
        for booking_id, kind, amount in PayoutLine.objects.filter(
            booking_id__in=[b.pk for b in bookings_by_uid.values()],
            kind__in=[PayoutLine.Kind.PASS_THROUGH, PayoutLine.Kind.OTHER],
        ).values_list('booking_id', 'kind', 'amount'):
            sums.setdefault(booking_id, {})[kind] = sums.get(booking_id, {}).get(kind, Decimal('0')) + amount
    to_save = []
    for booking in bookings_by_uid.values():
        totals = sums.get(booking.pk, {})
        pass_through = totals.get(PayoutLine.Kind.PASS_THROUGH, Decimal('0')) or booking.pass_through_amount
        other = totals.get(PayoutLine.Kind.OTHER, Decimal('0')) or booking.other_payout_amount
        if pass_through != booking.pass_through_amount or other != booking.other_payout_amount:
            booking.pass_through_amount = pass_through
            booking.other_payout_amount = other
            to_save.append(booking)
    if to_save:
        Booking.objects.bulk_update(to_save, ['pass_through_amount', 'other_payout_amount'])


def save_payout_batches(source, payout_batches):
    """The file's own 'Payout' rows (see importers.PayoutBatchRow): the actual bank transfers, kept whether or
    not we can say which property they belong to (a portfolio-wide file's Payout rows carry no listing) — see
    onsite.PayoutBatch. Keyed on (source, date, amount, reference) so re-uploading the same file changes
    nothing; a transfer with the same amount as one already on file but no reference of its own is still added
    (nothing here says they're the same transfer)."""
    for row in payout_batches or ():
        batch, _created = PayoutBatch.objects.get_or_create(
            source=source, date=row.date, amount=row.amount, reference=row.reference,
            defaults={'detail': row.detail, 'arriving_by': row.arriving_by},
        )
        save_payout_items(batch, row)


def save_payout_items(batch, row):
    """The lines that make up a payout, as the file listed them (importers.assign_breakdowns). A file that cuts a payout
    short (the first or last one of a date range) must not replace a complete breakdown saved before, so the lines are
    only replaced by ones that add up, or when none were saved yet."""
    items = list(getattr(row, 'items', ()) or ())
    if not items or not (row.breakdown_ok or not batch.items.exists()):
        return
    codes = {i.external_uid for i in items if i.external_uid}
    bookings = {b.external_uid: b for b in Booking.objects.filter(source=batch.source, external_uid__in=codes)} if codes else {}
    batch.items.all().delete()
    PayoutItem.objects.bulk_create([
        PayoutItem(payout=batch, booking=bookings.get(i.external_uid), external_uid=i.external_uid, type_label=i.type_label[:80], date=i.date, amount=i.amount,
                   listing_name=i.listing_name[:200], guest_name=i.guest_name[:200])
        for i in items
    ])
    # (the property stays unattributed on the batch itself, as before: where a payout belongs is read from its lines)
    PayoutBatch.objects.filter(pk=batch.pk).update(items_total=sum((i.amount for i in items), Decimal('0')), breakdown_ok=row.breakdown_ok, sequence=row.sequence)


def _fill_details(source, raw_bookings):
    """A payment report knows the guest; a calendar event usually doesn't. Where a
    row matches a reservation already on record (by code, or merged with the
    calendar stay it lines up with), fill in what that reservation is missing —
    never overwriting what it already has."""
    candidates = [row for row in raw_bookings if row.guest_name or row.guest_phone_last4 or row.listing_name]
    bookings_by_uid = {
        b.external_uid: b
        for b in Booking.objects.filter(source=source, external_uid__in=[row.external_uid for row in candidates])
    }
    for row in candidates:
        booking = bookings_by_uid.get(row.external_uid)
        if booking is None:
            continue
        changed = []
        for field, value in (('guest_name', row.guest_name), ('guest_phone_last4', row.guest_phone_last4), ('listing_name', row.listing_name)):
            if value and not getattr(booking, field):
                setattr(booking, field, value)
                changed.append(field)
        if changed:
            booking.save(update_fields=changed)


def diff_bookings(property, source, raw_bookings, default_unit=None):
    """Read-only preview diff — nothing written. Returns a dict with 'new'/
    'changed'/'reactivated'/'missing_visit' (lists of RawBooking) and
    'cancelled' (list of existing Booking rows).

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
    the file and its code reappears, no manual fix-up needed.

    Looked up by (source, external_uid) ONLY — never also property. That
    pair is the real DB-level unique constraint (Booking.Meta), because an
    Airbnb/VRBO confirmation code is unique to the platform, not to
    whichever property we happened to file it under. If a listing name's
    property/unit mapping ever changes after a reservation was first
    imported (staff re-pointing it, or fixing a wrong pick — see the
    Unit-model listing-name-to-unit work), the SAME confirmation code
    shows up again under a DIFFERENT property. Scoping this lookup by
    property too used to miss that existing row entirely, misclassify the
    row as 'new', and crash with IntegrityError trying to INSERT a second
    row for a (source, external_uid) the database already has — a real
    production bug this comment is now guarding against. A moved
    reservation is folded into 'changed' below (never 'new') specifically
    so apply_bookings_for_property relocates the existing row instead of
    attempting a duplicate insert."""
    uids = {row.external_uid for row in raw_bookings}
    listing_unit_map = _listing_unit_map(property, source)
    covered = coverage.covered_keys()
    existing_by_uid = {
        b.external_uid: b
        for b in Booking.objects.filter(source=source, external_uid__in=uids)
    }
    new_rows, changed_rows, reactivated_rows, missing_visit_rows = [], [], [], []
    cancelled = []
    for row in raw_bookings:
        existing = existing_by_uid.get(row.external_uid) or _feed_twin(property, source, row, listing_unit_map, default_unit)
        if existing is not None and existing.manually_cancelled:
            continue    # a person cancelled it; a file or calendar still listing it must not bring it back
        if row.is_cancelled:
            # Only this property's own row can be cancelled by a row it
            # received — a reservation currently filed under some OTHER
            # property is none of this property's business to touch.
            if existing is not None and existing.property_id == property.pk and existing.status == Booking.Status.ACTIVE:
                cancelled.append(existing)
            continue
        if existing is None:
            new_rows.append(row)
        elif existing.property_id != property.pk:
            # Same confirmation code, filed under a different property —
            # see the docstring above. Routed through 'changed' so the
            # apply side relocates (never duplicates) it.
            changed_rows.append(row)
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
            # A calendar feed is for ONE listing, so it knows the unit a reservation belongs to. A stay that was
            # filed earlier from a report with no unit (or a listing name not pinned to one) gets it from the
            # calendar - without this it stays "800 Tropic" forever, invisible to that unit's turnovers and to
            # its same-day check-in flag (both look only at the same unit).
            or (default_unit is not None and existing.unit_id is None)
        ):
            changed_rows.append(row)
        elif (
            not property.is_general and _needs_visit(row.check_out)
            and _cleaning_allowed(existing.property_id, existing.unit_id, source, False, covered, existing.on_calendar)
            and not existing.visits.exclude(status=Visit.Status.CANCELLED).exists()
        ):
            # Active booking, same property, nothing about the reservation
            # itself changed — normally a pure no-op. EXCEPT its cleaning
            # Visit can go missing independently of the Booking surviving
            # (wipe_unfinished_visits deliberately deletes unfinished
            # visits while preserving Booking history; a Visit can also be
            # hand-deleted). Without this, re-uploading the exact same file
            # looked identical to "nothing to do" and silently never
            # re-scheduled the cleaning — this is the one signal staff have
            # that a cleaning never got (re-)scheduled for an otherwise
            # untouched reservation, so surface and act on it explicitly
            # rather than assuming "unchanged" always means "nothing to do".
            missing_visit_rows.append(row)

    return {
        'new': new_rows, 'changed': changed_rows, 'reactivated': reactivated_rows,
        'missing_visit': missing_visit_rows, 'cancelled': cancelled,
    }


def _find_next_booking(property, after_datetime, exclude_pk=None, unit=None):
    """Scoped by `unit` when given (or explicitly to unit-less bookings when
    not) — without this, two units sharing one Property would each see the
    OTHER unit's check-ins as "the next booking," which is wrong. A
    single-unit property (every Booking.unit stays None) behaves exactly as
    before this parameter existed: `unit=None` still means "match rows with
    no unit," which is every row it has."""
    qs = Booking.objects.filter(
        property=property, unit=unit, status=Booking.Status.ACTIVE, check_in__gte=after_datetime,
    ).order_by('check_in')
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    return qs.first()


def _refresh_next_bookings_for_property(property):
    """Every branch below only ever recomputes next_booking/ready_by for
    the ONE visit generated from the specific booking row being processed
    — never for any OTHER visit at this property that might reference a
    booking a few rows up or down in the same file. That leaves a real gap:
    if guest B's reservation (which was visit A's "next" check-in) moves,
    cancels, or a closer guest C gets added, visit A's next_booking/
    ready_by/same-day-checkin badge silently goes stale — nothing else
    re-derives it. Called once at the end of apply_bookings_for_property,
    after every diff branch has run, so every active booking-linked visit
    at this property reflects the current picture regardless of which row
    actually changed."""
    active_visits = (
        Visit.objects.filter(property=property, booking__isnull=False)
        .exclude(status__in=[Visit.Status.CANCELLED, Visit.Status.SUBMITTED, Visit.Status.VERIFIED])
        .select_related('booking')
    )
    for visit in active_visits:
        correct_next = _find_next_booking(
            property, visit.booking.check_out, exclude_pk=visit.booking_id, unit=visit.booking.unit,
        )
        correct_next_id = correct_next.id if correct_next else None
        if correct_next_id != visit.next_booking_id:
            visit.next_booking = correct_next
            visit.ready_by = correct_next.check_in if correct_next else None
            visit.save(update_fields=['next_booking', 'ready_by'])


@transaction.atomic
def apply_bookings_for_property(property, source, raw_bookings, default_unit=None, from_feed=False):
    """Writes the diff computed the same way diff_bookings does, for ONE
    property's rows. Returns (new_count, changed_count, reactivated_count,
    cancelled_count, visit_note) — visit_note is a user-facing message when
    visit creation had to be skipped. Does not touch any ImportBatch; a
    portfolio-wide import calls this once per resolved property and
    aggregates the counts itself (see onsite/views.py)."""
    listing_unit_map = _listing_unit_map(property, source)
    covered = coverage.covered_keys()
    _adopt_feed_twins(property, source, raw_bookings, listing_unit_map, default_unit)
    diff = diff_bookings(property, source, raw_bookings, default_unit)
    turnover_type = VisitType.objects.filter(slug=TURNOVER_SLUG, is_active=True).first()
    if property.is_general:
        # A general placeholder (e.g. "Short-Term Rentals (general)") is the
        # bucket for listings nobody wants cleanings scheduled for: the
        # bookings are still recorded, but no visit is ever created.
        turnover_type = None
    visit_note = '' if (turnover_type or property.is_general) else (
        'Bookings were imported, but no active "Turnover" visit type exists yet — no visits were '
        'created. Run seed_checklist_templates, or create one manually, then re-import.'
    )
    # Every listing name this property answers to on this platform, resolved
    # to its Unit (or None) once up front rather than per-row — this is the
    # actual fix for "3 units, 1 property record": a row's listing_name
    # tells us which unit its Booking/Visit belongs to.

    for row in diff['new']:
        # default_unit: a polled calendar feed is for one specific listing, so
        # its rows carry no listing name to look up — the feed's own unit applies.
        unit = listing_unit_map.get(row.listing_name, default_unit)
        check_in_dt = _combine(property, row.check_in, 'check_in')
        check_out_dt = _combine(property, row.check_out, 'check_out')
        booking = Booking.objects.create(
            property=property, unit=unit, source=source, external_uid=row.external_uid,
            guest_name=row.guest_name, guest_phone_last4=row.guest_phone_last4,
            listing_name=row.listing_name, from_feed=from_feed,
            on_calendar=from_feed, calendar_seen_at=timezone.now() if from_feed else None,
            check_in=check_in_dt, check_out=check_out_dt, last_seen_at=timezone.now(),
        )
        if turnover_type and _needs_visit(row.check_out) and _cleaning_allowed(property.pk, unit.pk if unit else None, source, from_feed, covered):
            next_booking = _find_next_booking(property, check_out_dt, exclude_pk=booking.pk, unit=unit)
            create_visit(
                property, turnover_type, unit=unit, booking=booking, next_booking=next_booking,
                scheduled_date=row.check_out, ready_by=next_booking.check_in if next_booking else None,
            )

    for row in diff['missing_visit']:
        # See diff_bookings' docstring — an otherwise-untouched active
        # booking whose cleaning Visit went missing independently (e.g.
        # wipe_unfinished_visits, or a hand-deleted Visit). Re-checked here
        # rather than trusted from the diff, since diff_bookings is called
        # fresh at the top of this same function — this guard is just
        # defensive, not covering any real staleness window.
        booking = Booking.objects.get(source=source, external_uid=row.external_uid)
        if (
            turnover_type and _needs_visit(row.check_out)
            and _cleaning_allowed(property.pk, booking.unit_id, source, from_feed, covered, booking.on_calendar)
            and not booking.visits.exclude(status=Visit.Status.CANCELLED).exists()
        ):
            unit = listing_unit_map.get(row.listing_name) if row.listing_name else booking.unit
            next_booking = _find_next_booking(property, booking.check_out, exclude_pk=booking.pk, unit=unit)
            create_visit(
                property, turnover_type, unit=unit, booking=booking, next_booking=next_booking,
                scheduled_date=booking.check_out.date(), ready_by=next_booking.check_in if next_booking else None,
            )

    for row in diff['changed']:
        # Looked up by (source, external_uid) only, NOT also property — see
        # diff_bookings' docstring. This row may currently be filed under a
        # DIFFERENT property than `property` (a moved reservation, folded
        # into 'changed' rather than 'new' specifically so this relocates
        # the existing row instead of colliding with the DB's unique
        # constraint on a duplicate insert).
        booking = Booking.objects.get(source=source, external_uid=row.external_uid)
        booking.property = property
        booking.check_in = _combine(property, row.check_in, 'check_in')
        booking.check_out = _combine(property, row.check_out, 'check_out')
        # A 'changed' row is by construction never a cancelled one (those
        # are filtered out earlier in diff_bookings) — always ACTIVE here,
        # which also correctly revives a moved booking that was CANCELLED
        # under its old property (diff_bookings routes that case through
        # 'changed' too, since the property mismatch is checked first).
        booking.status = Booking.Status.ACTIVE
        if row.listing_name:
            booking.listing_name = row.listing_name
            booking.unit = listing_unit_map.get(row.listing_name)
        if booking.unit_id is None and default_unit is not None:
            booking.unit = default_unit       # the calendar's own unit, for a stay that was filed without one
        booking.last_seen_at = timezone.now()
        booking.save(update_fields=['property', 'status', 'check_in', 'check_out', 'listing_name', 'unit', 'last_seen_at'])
        visit = booking.visits.exclude(status__in=['submitted', 'verified', 'cancelled']).first()
        if visit:
            next_booking = _find_next_booking(property, booking.check_out, exclude_pk=booking.pk, unit=booking.unit)
            visit.property = property
            visit.unit = booking.unit
            new_day = booking.check_out.date()
            extra = []
            if visit.date_set_at and visit.scheduled_date != new_day:
                # The calendar wins over a date a person chose: the guest's stay changed. Say so, so it isn't a mystery.
                who = (visit.date_set_by.get_full_name() or visit.date_set_by.username) if visit.date_set_by_id else 'someone'
                visit.calendar_override_note = (f'The reservation calendar moved this to {new_day:%b} {new_day.day}, replacing '
                                                f'{visit.scheduled_date:%b} {visit.scheduled_date.day}, which {who} set on {timezone.localtime(visit.date_set_at):%b} {timezone.localtime(visit.date_set_at).day}.')[:300]
                visit.date_set_by, visit.date_set_at = None, None
                extra = ['date_set_by', 'date_set_at', 'calendar_override_note']
            visit.scheduled_date = new_day
            visit.next_booking = next_booking
            visit.ready_by = next_booking.check_in if next_booking else None
            visit.save(update_fields=['property', 'unit', 'scheduled_date', 'next_booking', 'ready_by'] + extra)
            transaction.on_commit(lambda visit=visit: push_visit(visit))
        else:
            # No active visit survived — either genuinely none was ever
            # created, or (a moved-while-cancelled booking, per this loop's
            # comment above) the only one on file is CANCELLED, which the
            # exclude() above deliberately skips. Mirrors the 'reactivated'
            # loop just below: revive the cancelled one (moving it here
            # too) rather than leaving this booking with no cleaning
            # scheduled, or creating a duplicate Visit.
            next_booking = _find_next_booking(property, booking.check_out, exclude_pk=booking.pk, unit=booking.unit)
            cancelled_visit = booking.visits.filter(status=Visit.Status.CANCELLED).order_by('-pk').first()
            if cancelled_visit:
                cancelled_visit.status = (
                    Visit.Status.SCHEDULED
                    if cancelled_visit.assigned_staff_id or cancelled_visit.assigned_contact_id
                    else Visit.Status.UNASSIGNED
                )
                cancelled_visit.property = property
                cancelled_visit.unit = booking.unit
                cancelled_visit.scheduled_date = booking.check_out.date()
                cancelled_visit.next_booking = next_booking
                cancelled_visit.ready_by = next_booking.check_in if next_booking else None
                cancelled_visit.save(update_fields=['property', 'unit', 'status', 'scheduled_date', 'next_booking', 'ready_by'])
                transaction.on_commit(lambda visit=cancelled_visit: push_visit(visit))
            elif turnover_type and _needs_visit(row.check_out) and _cleaning_allowed(property.pk, booking.unit_id, source, from_feed, covered, booking.on_calendar):
                create_visit(
                    property, turnover_type, unit=booking.unit, booking=booking, next_booking=next_booking,
                    scheduled_date=row.check_out, ready_by=next_booking.check_in if next_booking else None,
                )

    for row in diff['reactivated']:
        booking = Booking.objects.get(property=property, source=source, external_uid=row.external_uid)
        booking.status = Booking.Status.ACTIVE
        booking.check_in = _combine(property, row.check_in, 'check_in')
        booking.check_out = _combine(property, row.check_out, 'check_out')
        if row.listing_name:
            booking.listing_name = row.listing_name
            booking.unit = listing_unit_map.get(row.listing_name)
        booking.last_seen_at = timezone.now()
        booking.save(update_fields=['status', 'check_in', 'check_out', 'listing_name', 'unit', 'last_seen_at'])

        next_booking = _find_next_booking(property, booking.check_out, exclude_pk=booking.pk, unit=booking.unit)
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
            cancelled_visit.unit = booking.unit
            cancelled_visit.scheduled_date = booking.check_out.date()
            cancelled_visit.next_booking = next_booking
            cancelled_visit.ready_by = next_booking.check_in if next_booking else None
            cancelled_visit.save(update_fields=['unit', 'status', 'scheduled_date', 'next_booking', 'ready_by'])
            transaction.on_commit(lambda visit=cancelled_visit: push_visit(visit))
        elif turnover_type and _needs_visit(row.check_out) and _cleaning_allowed(property.pk, booking.unit_id, source, from_feed, covered, booking.on_calendar):
            # No Visit at all survived (shouldn't normally happen, but
            # don't leave a reactivated booking with no cleaning scheduled).
            create_visit(
                property, turnover_type, unit=booking.unit, booking=booking, next_booking=next_booking,
                scheduled_date=row.check_out, ready_by=next_booking.check_in if next_booking else None,
            )

    _record_paid_cancellations(property, source, raw_bookings, listing_unit_map, default_unit)
    _save_amounts(source, raw_bookings)
    _fill_details(source, raw_bookings)
    payouts_service.attach_pending(source, [r.external_uid for r in raw_bookings])

    for booking in diff['cancelled']:
        booking.status = Booking.Status.CANCELLED
        booking.save(update_fields=['status'])
        active_visits = list(booking.visits.exclude(status__in=['submitted', 'verified', 'cancelled']))
        booking.visits.filter(pk__in=[v.pk for v in active_visits]).update(status='cancelled')
        for visit in active_visits:
            transaction.on_commit(lambda visit=visit: delete_visit_event(visit))

    _refresh_next_bookings_for_property(property)

    return len(diff['new']), len(diff['changed']), len(diff['reactivated']), len(diff['cancelled']), visit_note


def next_arrival_diagnosis(visit):
    """Why a cleaning that SHOULD be a same-day check-in isn't flagged as one — answered from what is actually on
    file, since the flag is just "the next booking on file arrives the day this cleaning is scheduled" (see
    Visit.is_same_day_checkin). None when there is nothing to explain (no date, already same-day, or the visit
    is over). Otherwise {'arrivals': [...], 'feeds': [...], 'fixable': bool}: every reservation of this property
    arriving that day (in any state, under any unit) with what is wrong with it, or none at all - meaning the
    reservation never reached the system - plus the state of the property's calendar feeds."""
    from ..models import BookingFeed
    if not visit.scheduled_date or visit.status in (Visit.Status.SUBMITTED, Visit.Status.VERIFIED, Visit.Status.CANCELLED):
        return None
    if visit.is_same_day_checkin():
        return None
    day = visit.scheduled_date
    start = timezone.make_aware(datetime.combine(day, time(0, 0)))
    end = start + timedelta(days=1)
    visit_unit_id = visit.unit_id if visit.unit_id else (visit.booking.unit_id if visit.booking_id else None)
    arrivals, fixable = [], False
    for b in Booking.objects.filter(property=visit.property, check_in__gte=start, check_in__lt=end).exclude(pk=visit.booking_id).select_related('unit'):
        if b.status == Booking.Status.CANCELLED or b.manually_cancelled:
            problem, ok = 'is cancelled, so it does not count', False
        elif b.unit_id != visit_unit_id:
            problem, ok = f'is filed under {b.unit.label if b.unit else "no unit"}, but this cleaning is for {visit.unit.label if visit.unit else "no unit"} — the same-day flag only looks at the same unit', False
        else:
            problem, ok = 'is on file and should count — this visit had not picked it up yet', True
            fixable = True
        arrivals.append({'guest': b.guest_name or 'Guest', 'code': b.external_uid, 'source': b.get_source_display(), 'check_in': timezone.localtime(b.check_in), 'problem': problem, 'ok': ok})
    feeds = [{
        'label': f.label(), 'source': f.get_source_display(), 'last_success_at': f.last_success_at, 'last_error': f.last_error, 'not_listed': f.not_listed, 'is_active': f.is_active,
    } for f in BookingFeed.objects.filter(property=visit.property)]
    return {'day': day, 'arrivals': arrivals, 'feeds': feeds, 'fixable': fixable}
