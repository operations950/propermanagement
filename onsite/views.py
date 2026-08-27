import calendar as calendar_module
import json
import logging
import traceback
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Max, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_http_methods

from core.models import Contact, Property, StaffProfile, Unit
from core.views import _is_admin, _parse_decimal
from supplies import services as supply_services
from supplies.models import SupplyItem, SupplyReading
from vendorportal.models import AccessAttempt

from .importers import BookingFileError, detect_format, parse_booking_file, read_csv_header
from .models import (
    Booking, BookingFeedHealth, CleaningPaymentBatch, CleaningPricingSettings, DailyUploadSlot, ImportBatch,
    PropertyChecklistItem, StandardChecklistItem, Visit, VisitChecklistItem, VisitIssue, VisitMedia, VisitRule,
    VisitType,
)
from .services import checklist as checklist_service
from .services import notify as notify_service
from .services.bookings import (
    apply_bookings_for_property, check_listing_name_conflict, diff_bookings, resolve_listing_names, save_listing_name,
)

logger = logging.getLogger(__name__)

# How far ahead of ready_by a visit starts showing as at-risk — a heads-up
# window, not just "already late". Not user-configurable; a fixed buffer is
# enough for a company this size and avoids a settings screen for one number.
AT_RISK_BUFFER_MINUTES = 90

# TEMPORARY — see the diagnostic try/except in booking_import_apply. Remove
# this along with that block once the production-only Apply 500 is found.
DEBUG_LAST_IMPORT_ERROR_SESSION_KEY = 'booking_import_last_error_tb'


def _visit_status(visit, now):
    """Derived, never stored — see ONSITE_DESIGN.md's Dashboard section for
    why: a stored status can drift from the visit it summarizes, a computed
    one can't."""
    if visit.status in (Visit.Status.CANCELLED, Visit.Status.SKIPPED, Visit.Status.SUBMITTED, Visit.Status.VERIFIED):
        return visit.status
    at_risk = visit.ready_by and now >= visit.ready_by - timedelta(minutes=AT_RISK_BUFFER_MINUTES)
    if at_risk:
        return 'at_risk'
    if visit.status == Visit.Status.IN_PROGRESS:
        return 'in_progress'
    return 'dirty'


# Every forward-looking range the board's tab strip can show, as an offset
# from today. 'yesterday' isn't in here — it's the one backward-looking tab,
# handled separately in dashboard() since it doesn't fit the
# start=today/end=today+N shape the others share. 'day' isn't a tab at all —
# it's what a single ?date= drill-in (e.g. from the calendar view) renders
# as, with its own "back to Today + Tomorrow" link instead of a tab.
_RANGE_SPANS = {
    'default': 1,  # today, tomorrow
    'week': 6,     # today .. +6 (7 days)
    'month': 29,   # today .. +29 (30 days)
}


@login_required
def dashboard(request):
    """Today's urgent items never move regardless of range (that banner is
    about what needs attention *right now*), but the board itself can look
    further out than just today — a same-day-only board was hiding every
    visit created by an import unless it happened to land on today, which
    is confusing right after an upload covering the next several weeks.
    ?range=week|month broadens it; ?date=YYYY-MM-DD (from the calendar
    view) narrows it to one specific day anywhere on the calendar."""
    today = timezone.localdate()
    now = timezone.now()

    date_param = request.GET.get('date')
    if date_param:
        try:
            start = end = datetime.strptime(date_param, '%Y-%m-%d').date()
        except ValueError:
            start = end = today
        range_key = 'day'
    elif request.GET.get('range') == 'yesterday':
        range_key = 'yesterday'
        start = end = today - timedelta(days=1)
    else:
        range_key = request.GET.get('range') if request.GET.get('range') in _RANGE_SPANS else 'default'
        start = today
        end = today + timedelta(days=_RANGE_SPANS[range_key])

    visits_qs = Visit.objects.filter(scheduled_date__gte=start, scheduled_date__lte=end)
    if range_key != 'yesterday':
        # Cancelled/skipped visits drop off the forward-looking board — they're
        # resolved, not something to prepare for. Yesterday is a look-back, not
        # a to-do list: what actually got skipped or cancelled is exactly the
        # kind of thing worth seeing in a same-day review, so nothing is
        # filtered out of it.
        visits_qs = visits_qs.exclude(status__in=[Visit.Status.CANCELLED, Visit.Status.SKIPPED])
    visits = list(
        visits_qs
        .select_related('property', 'visit_type', 'assigned_staff__user', 'assigned_contact', 'booking', 'next_booking')
        .order_by('scheduled_date', 'ready_by')
    )
    for visit in visits:
        visit.derived_status = _visit_status(visit, now)

    visits_by_date = {}
    for visit in visits:
        visits_by_date.setdefault(visit.scheduled_date, []).append(visit)

    # The 2-day default (and a single drilled-in day) always show every day
    # in range, even empty ones — "I uploaded bookings and see nothing" is
    # exactly the confusing case an empty-but-visible Today/Tomorrow avoids.
    # A week/month view skips empty days instead — 30 "no visits" sections
    # would bury the days that actually matter.
    show_empty_days = range_key in ('default', 'day', 'yesterday')
    board_days = []
    span = (end - start).days
    for offset in range(span + 1):
        day = start + timedelta(days=offset)
        day_visits = visits_by_date.get(day, [])
        if day_visits or show_empty_days:
            board_days.append({
                'date': day, 'visits': day_visits, 'is_today': day == today,
                'is_yesterday': day == today - timedelta(days=1),
            })

    todays_visits = visits_by_date.get(today, [])
    checkouts_today = (
        Booking.objects.filter(check_out__date=today, status=Booking.Status.ACTIVE)
        .select_related('property')
        .prefetch_related('visits')
    )
    checkouts_missing_visit = [
        b for b in checkouts_today
        if not any(v.status not in (Visit.Status.CANCELLED, Visit.Status.SKIPPED) for v in b.visits.all())
    ]
    unassigned_visits = [v for v in todays_visits if v.status == Visit.Status.UNASSIGNED]
    at_risk_visits = [v for v in todays_visits if v.derived_status == 'at_risk']

    return render(request, 'onsite/dashboard.html', {
        'today': today,
        'range_key': range_key,
        'drilled_in_date': start if range_key == 'day' else None,
        'board_days': board_days,
        'checkouts_missing_visit': checkouts_missing_visit,
        'unassigned_visits': unassigned_visits,
        'at_risk_visits': at_risk_visits,
        'is_admin': _is_admin(request.user),
    })


@login_required
def calendar_view(request):
    """Zoomed-out month view — a grid of days with a visit count on each,
    clicking a day drills into dashboard's ?date= single-day board. The
    "expand further" half of the today/tomorrow/expand model; the board
    itself is the "zoomed in" half."""
    today = timezone.localdate()
    month_param = request.GET.get('month', '')
    try:
        year, month = (int(p) for p in month_param.split('-', 1))
        first_day = date(year, month, 1)
    except (ValueError, TypeError):
        first_day = today.replace(day=1)
        year, month = first_day.year, first_day.month
    last_day = date(year, month, calendar_module.monthrange(year, month)[1])

    counts_by_day = dict(
        Visit.objects.filter(scheduled_date__gte=first_day, scheduled_date__lte=last_day)
        .exclude(status__in=[Visit.Status.CANCELLED, Visit.Status.SKIPPED])
        .values_list('scheduled_date')
        .annotate(n=Count('id'))
        .values_list('scheduled_date', 'n')
    )

    cal = calendar_module.Calendar(firstweekday=6)  # Sunday-first
    weeks = [
        [{'date': day, 'in_month': day.month == month, 'is_today': day == today, 'count': counts_by_day.get(day, 0)} for day in week]
        for week in cal.monthdatescalendar(year, month)
    ]

    prev_month = (first_day - timedelta(days=1)).replace(day=1)
    next_month = (last_day + timedelta(days=1))

    return render(request, 'onsite/calendar.html', {
        'weeks': weeks,
        'month_label': first_day.strftime('%B %Y'),
        'prev_month': prev_month.strftime('%Y-%m'),
        'next_month': next_month.strftime('%Y-%m'),
        'this_month': today.strftime('%Y-%m'),
    })


def _str_properties():
    return Property.objects.filter(property_type=Property.Type.SHORT_TERM_RENTAL, is_active=True).order_by('name')


def _visit_eligible_properties():
    """(str_properties, other_properties) for the manual "schedule a
    visit" and "recurring rule" screens. STR is the default-visible pool
    — on-site visits are overwhelmingly STR turnovers/deep-cleans/
    inspections, so that's what the picker should show without asking
    first. `other_properties` (every other active type) stays reachable
    behind an explicit "show other property types" toggle in the
    template — the escape hatch for the real but rare exception case
    (a commercial common-area clean, a one-off non-turnover house
    cleaning), never the default view. See _str_properties() above for
    the (unrelated) booking-import property list, which stays STR-only
    outright since a booking file is inherently an STR/checkout concept
    with no exception case to expand into."""
    properties = Property.objects.filter(is_active=True).order_by('property_type', 'name')
    str_properties = [p for p in properties if p.property_type == Property.Type.SHORT_TERM_RENTAL]
    other_properties = [p for p in properties if p.property_type != Property.Type.SHORT_TERM_RENTAL]
    return str_properties, other_properties


def _portfolio_preview_context(batch, raw_bookings, source, posted=None):
    """Builds the preview context for a portfolio-wide .csv: which rows
    auto-matched an existing Property (with their diff), and which distinct
    listing names didn't. `posted`, when given, is request.POST from a just
    -submitted (and blocked) Apply attempt — used to keep the human's picks
    sticky and surface per-group conflict warnings without losing their
    work, per the two-scenario warning the user asked for."""
    matched, unmatched = resolve_listing_names(raw_bookings, source)
    property_diffs = [
        {'property': property, 'diff': diff_bookings(property, source, rows)}
        for property, rows in sorted(matched.items(), key=lambda kv: kv[0].name)
    ]
    all_properties = list(_str_properties())

    unmatched_groups = []
    for i, (listing_name, rows) in enumerate(sorted(unmatched.items())):
        posted_property_id = (posted.get(f'map_{i}') if posted else '') or ''
        conflict = None
        if posted is not None and posted_property_id:
            chosen = Property.objects.filter(pk=posted_property_id).first()
            if chosen:
                conflict = check_listing_name_conflict(chosen, source, listing_name)
                if conflict and conflict['type'] == 'additional' and posted.get(f'confirm_{i}'):
                    conflict = None
        unmatched_groups.append({
            'index': i,
            'listing_name': listing_name,
            'count': len(rows),
            'sample_checkout': rows[0].check_out,
            'properties': all_properties,
            'posted_property_id': posted_property_id,
            'posted_unit_id': (posted.get(f'unit_{i}') if posted else '') or '',
            'unresolved': posted is not None and not posted_property_id,
            'conflict': conflict,
        })

    return {
        'batch': batch, 'portfolio': True, 'source': source,
        'property_diffs': property_diffs, 'unmatched_groups': unmatched_groups,
        'units_by_property_json': _units_by_property_json(),
    }


def _units_by_property_json():
    """{property_id: [{id, label}, ...]} for every property that has at
    least one active Unit — fed into the unmatched-listing-name resolution
    UI's per-group Unit picker, which shows/repopulates itself client-side
    off this map as soon as a property is chosen for that group. Mirrors
    tickets/views.py's identically-shaped helper for its own unit picker."""
    grouped = {}
    for unit in Unit.objects.filter(is_active=True).select_related('property').order_by('label'):
        grouped.setdefault(str(unit.property_id), []).append({'id': unit.pk, 'label': unit.label})
    return json.dumps(grouped)


def _create_import_batch(user, source, uploaded_file, property=None):
    """Shared by the generic upload form and each daily-upload-slot drop —
    parses the file, decides single-property vs. portfolio-wide the same
    way either time, and saves the not-yet-applied ImportBatch. Returns
    (batch, error_message); batch is None on error."""
    try:
        fmt = detect_format(uploaded_file.name)
        raw_bookings = parse_booking_file(uploaded_file)
    except BookingFileError as e:
        return None, str(e)

    portfolio_mode = fmt == 'csv' and not property and any(r.listing_name for r in raw_bookings)
    if not portfolio_mode and not property:
        return None, (
            'Choose a property — only a portfolio-wide .csv with a listing/property column can skip this.'
        )

    covers_start = min(r.check_out for r in raw_bookings)
    covers_end = max(r.check_out for r in raw_bookings)
    batch = ImportBatch.objects.create(
        property=property if not portfolio_mode else None, source=source, raw_file=uploaded_file,
        covers_start=covers_start, covers_end=covers_end, imported_by=user,
    )
    return batch, None


def _update_feed_health(source, raw_bookings):
    """Called once a batch for `source` actually applies successfully —
    see BookingFeedHealth's docstring for what each field means and why
    they're kept separate. All three only ever move forward."""
    health, _ = BookingFeedHealth.objects.get_or_create(source=source)
    update_fields = ['last_upload_at']
    health.last_upload_at = timezone.now()

    booked_dates = [r.booked_at for r in raw_bookings if r.booked_at]
    if booked_dates:
        newest = max(booked_dates)
        if not health.newest_booked_date or newest > health.newest_booked_date:
            health.newest_booked_date = newest
            update_fields.append('newest_booked_date')

    checkouts = [r.check_out for r in raw_bookings]
    if checkouts:
        furthest = max(checkouts)
        if not health.coverage_through or furthest > health.coverage_through:
            health.coverage_through = furthest
            update_fields.append('coverage_through')

    health.save(update_fields=update_fields)


@login_required
def booking_import_upload(request):
    """Phase 1 of the two-phase import. Two shapes:
    - a single-listing file (.ics always; a .csv with no listing/property
      column) — staff pick the property, same as before.
    - a portfolio-wide .csv (has a listing/property column with data) — no
      property picked upfront; every row's listing name is auto-resolved
      against stored PropertyListingName rows, and whatever doesn't match
      gets a resolution UI on the preview screen instead.
    Nothing is written to Booking/Visit yet either way. The file itself is
    saved onto a not-yet-applied ImportBatch so the preview/apply views can
    act on exactly what was uploaded without re-uploading — a plain
    redirect to the batch's own preview URL once it's created, so refreshing
    or coming back to it (e.g. after a quick-add-property round trip) never
    resubmits the file.

    The daily-upload-slots repository above this form on the same page
    (see upload_slot) covers the small fixed set of reports staff actually
    pull every day — this generic form stays as the fallback for anything
    outside that set."""
    str_properties = _str_properties()
    slots = DailyUploadSlot.objects.filter(is_active=True)

    if request.method == 'POST':
        property_id = request.POST.get('property')
        source = request.POST.get('source')
        uploaded_file = request.FILES.get('file')

        if not (source and uploaded_file):
            messages.error(request, 'Choose a source and a file.')
            return render(request, 'onsite/booking_import_upload.html', {'properties': str_properties, 'slots': slots})

        property = get_object_or_404(Property, pk=property_id) if property_id else None
        batch, error = _create_import_batch(request.user, source, uploaded_file, property)
        if error:
            messages.error(request, error)
            return render(request, 'onsite/booking_import_upload.html', {'properties': str_properties, 'slots': slots})
        return redirect('onsite_booking_import_preview', batch_id=batch.pk)

    return render(request, 'onsite/booking_import_upload.html', {'properties': str_properties, 'slots': slots})


@login_required
@require_http_methods(['POST'])
def upload_slot(request, slot_id):
    """Drop target for one of the named daily-upload slots — the source
    (and, implicitly, "this is a portfolio-wide file") is already known
    from the slot, so this is a single click/drop with nothing to pick,
    unlike the generic form. Reuses the exact same parse/preview/apply
    machinery as the generic upload — a slot is just a shortcut to the
    same ImportBatch flow, not a separate code path."""
    slot = get_object_or_404(DailyUploadSlot, pk=slot_id, is_active=True)
    uploaded_file = request.FILES.get('file')
    if not uploaded_file:
        messages.error(request, 'No file received.')
        return redirect('onsite_booking_import')

    # Reject outright, before anything is parsed or saved, if this file's
    # header row doesn't match what this slot expects — catches the wrong
    # platform's export (or a reformatted one) getting dropped into a slot
    # rather than letting it silently misparse. No-op when the slot has no
    # required_columns configured.
    fieldnames = read_csv_header(uploaded_file)
    missing = slot.missing_columns(fieldnames)
    if missing:
        messages.error(
            request,
            f'{slot.label}: this file doesn\'t look right for this slot — missing column(s) '
            f'{", ".join(missing)}. Double-check you dropped the correct export here.',
        )
        return redirect('onsite_booking_import')

    batch, error = _create_import_batch(request.user, slot.source, uploaded_file)
    if error:
        messages.error(request, f'{slot.label}: {error}')
        return redirect('onsite_booking_import')

    slot.last_uploaded_at = timezone.now()
    slot.last_uploaded_by = request.user
    slot.last_batch = batch
    slot.save(update_fields=['last_uploaded_at', 'last_uploaded_by', 'last_batch'])
    return redirect('onsite_booking_import_preview', batch_id=batch.pk)


@login_required
def booking_import_preview(request, batch_id):
    """Re-shows the diff for a not-yet-applied batch by re-parsing its saved
    file — the GET counterpart to booking_import_upload's POST, so the
    preview has a real URL to land on (after the initial upload, or after a
    quick_add_property round trip) rather than only existing as an inline
    render of the upload POST."""
    batch = get_object_or_404(ImportBatch, pk=batch_id, applied_at__isnull=True)
    # TEMPORARY — see booking_import_apply's matching diagnostic try/except.
    # Popped (not just read) so it only ever shows once, right after the
    # crash that produced it.
    debug_last_import_error = request.session.pop(DEBUG_LAST_IMPORT_ERROR_SESSION_KEY, None)
    try:
        raw_bookings = parse_booking_file(batch.raw_file)
    except BookingFileError as e:
        messages.error(request, f'Could not re-read the saved file: {e}')
        return redirect('onsite_booking_import')

    if batch.property_id:
        diff = diff_bookings(batch.property, batch.source, raw_bookings)
        return render(request, 'onsite/booking_import_preview.html', {
            'batch': batch, 'property': batch.property, 'diff': diff, 'portfolio': False,
            'debug_last_import_error': debug_last_import_error,
        })

    context = _portfolio_preview_context(batch, raw_bookings, batch.source)
    context['debug_last_import_error'] = debug_last_import_error
    return render(
        request, 'onsite/booking_import_preview.html',
        context,
    )


@login_required
@require_http_methods(['POST'])
def quick_add_property(request, batch_id):
    """Lets staff resolving an unmatched listing name create the missing
    property on the spot — "we forgot to add it before this upload" — rather
    than cancelling the import to go do it elsewhere. Created as a plain
    active short-term rental with just a name; everything else (address,
    access info, ...) gets filled in later the normal way. Redirects back to
    the same batch's preview, where the new property now shows up in every
    listing-name picker."""
    batch = get_object_or_404(ImportBatch, pk=batch_id, applied_at__isnull=True)
    name = request.POST.get('new_property_name', '').strip()
    if not name:
        messages.error(request, 'Enter a name for the new property.')
    elif Property.objects.filter(name__iexact=name).exists():
        messages.error(request, f'A property named "{name}" already exists — pick it from the list instead.')
    else:
        Property.objects.create(name=name, property_type=Property.Type.SHORT_TERM_RENTAL, is_active=True)
        messages.success(request, f'Added "{name}" — pick it below for whichever listing name(s) it belongs to.')
    return redirect('onsite_booking_import_preview', batch_id=batch.pk)


@login_required
def booking_import_apply(request, batch_id):
    """Phase 2: staff confirmed the diff shown by booking_import_upload —
    re-parses the same saved file (rather than trusting anything from the
    client) and writes the changes. For a portfolio batch, also validates
    every submitted unmatched-listing-name -> property mapping first: any
    still-unresolved name, or any unconfirmed conflict, blocks the whole
    apply and re-shows the preview with the specific problem(s) flagged —
    an all-or-nothing pass so a partial mapping never leaves some bookings
    silently un-imported with no record of why."""
    batch = get_object_or_404(ImportBatch, pk=batch_id, applied_at__isnull=True)
    if request.method != 'POST':
        return redirect('onsite_booking_import')

    try:
        raw_bookings = parse_booking_file(batch.raw_file)
    except BookingFileError as e:
        messages.error(request, f'Could not re-read the saved file: {e}')
        return redirect('onsite_booking_import')

    # TEMPORARY diagnostic wrap (see the matching block in booking_import_preview
    # that reads DEBUG_LAST_IMPORT_ERROR_SESSION_KEY) — added while tracking down
    # a production-only 500 on Apply that hasn't reproduced locally. Remove once
    # that's found and fixed; this is not meant to be permanent app behavior.
    try:
        if batch.property_id:
            new_count, changed_count, reactivated_count, cancelled_count, visit_note = apply_bookings_for_property(
                batch.property, batch.source, raw_bookings,
            )
            batch.new_count, batch.changed_count = new_count, changed_count
            batch.reactivated_count, batch.cancelled_count = reactivated_count, cancelled_count
            batch.applied_at = timezone.now()
            batch.save(update_fields=['new_count', 'changed_count', 'reactivated_count', 'cancelled_count', 'applied_at'])
            _update_feed_health(batch.source, raw_bookings)
            messages.success(
                request,
                f'Imported: {new_count} new, {changed_count} changed, {reactivated_count} reactivated, '
                f'{cancelled_count} cancelled.',
            )
            if visit_note:
                messages.warning(request, visit_note)
            return redirect('onsite_dashboard')

        matched, unmatched = resolve_listing_names(raw_bookings, batch.source)
        pending_mappings = []
        blocked = False
        for i, (listing_name, rows) in enumerate(sorted(unmatched.items())):
            property_id = request.POST.get(f'map_{i}')
            if not property_id:
                blocked = True
                continue
            property = get_object_or_404(Property, pk=property_id)
            unit_id = request.POST.get(f'unit_{i}') or None
            unit = property.units.filter(pk=unit_id).first() if unit_id else None
            conflict = check_listing_name_conflict(property, batch.source, listing_name)
            if conflict and conflict['type'] == 'additional' and request.POST.get(f'confirm_{i}'):
                conflict = None
            if conflict:
                blocked = True
            else:
                pending_mappings.append((listing_name, property, unit, rows))

        if blocked:
            messages.error(
                request,
                'Every listing name needs to be mapped to a property (and any conflicts resolved) before this import can be applied.',
            )
            context = _portfolio_preview_context(batch, raw_bookings, batch.source, posted=request.POST)
            return render(request, 'onsite/booking_import_preview.html', context)

        for listing_name, property, unit, rows in pending_mappings:
            save_listing_name(property, batch.source, listing_name, unit=unit)
            matched[property] = matched.get(property, []) + rows

        total_new = total_changed = total_reactivated = total_cancelled = 0
        visit_notes = set()
        for property, rows in matched.items():
            n, c, r, x, note = apply_bookings_for_property(property, batch.source, rows)
            total_new += n
            total_changed += c
            total_reactivated += r
            total_cancelled += x
            if note:
                visit_notes.add(note)

        batch.new_count, batch.changed_count = total_new, total_changed
        batch.reactivated_count, batch.cancelled_count = total_reactivated, total_cancelled
        batch.applied_at = timezone.now()
        batch.save(update_fields=['new_count', 'changed_count', 'reactivated_count', 'cancelled_count', 'applied_at'])
        _update_feed_health(batch.source, raw_bookings)

        property_word = 'property' if len(matched) == 1 else 'properties'
        messages.success(
            request,
            f'Imported: {total_new} new, {total_changed} changed, {total_reactivated} reactivated, '
            f'{total_cancelled} cancelled, across {len(matched)} {property_word}.',
        )
        for note in visit_notes:
            messages.warning(request, note)
        return redirect('onsite_dashboard')
    except Exception:
        tb = traceback.format_exc()
        logger.exception('booking_import_apply crashed for batch %s', batch_id)
        request.session[DEBUG_LAST_IMPORT_ERROR_SESSION_KEY] = tb
        messages.error(
            request,
            'Something went wrong applying this import. The full error is shown below the file review '
            'on this page — please copy it and send it over so this can get fixed.',
        )
        return redirect('onsite_booking_import_preview', batch_id=batch_id)


@login_required
def visit_create(request):
    """Manually schedule a one-off visit — an owner-requested extra
    cleaning, an ad-hoc inspection, anything not tied to a booking-file
    checkout, for ANY property type (not just STR) — a commercial
    common-area clean or a non-turnover house cleaning goes through here
    exactly the same way. Booking import is still how the bulk of STR
    turnovers get created; this is the escape hatch for everything else,
    since Django admin's plain "Add Visit" form bypasses create_visit
    entirely (so it wouldn't get a checklist at all). STR is the
    default-visible property pool (the overwhelming common case); every
    other type is one tap away behind "show other property types" —
    see _visit_eligible_properties()."""
    str_properties, other_properties = _visit_eligible_properties()
    visit_types = VisitType.objects.filter(is_active=True, is_addon=False).order_by('name')
    staff_options = StaffProfile.objects.select_related('user').filter(
        user__is_active=True, role=StaffProfile.Role.CLEANER,
    )
    contact_options = Contact.objects.filter(
        Q(contact_type=Contact.ContactType.VENDOR, trade__icontains='clean')
        | Q(contact_type=Contact.ContactType.ON_SITE_STAFF),
    )
    units_by_property_json = _units_by_property_json()

    if request.method == 'POST':
        prop = get_object_or_404(Property, pk=request.POST.get('property'), is_active=True) \
            if request.POST.get('property') else None
        visit_type = get_object_or_404(VisitType, pk=request.POST.get('visit_type'), is_addon=False) \
            if request.POST.get('visit_type') else None

        if not prop or not visit_type:
            messages.error(request, 'Choose a property and a visit type.')
            return render(request, 'onsite/visit_create.html', {
                'str_properties': str_properties, 'other_properties': other_properties, 'visit_types': visit_types,
                'staff_options': staff_options, 'contact_options': contact_options,
                'units_by_property_json': units_by_property_json,
            })

        unit_id = request.POST.get('unit') or None
        unit = prop.units.filter(pk=unit_id).first() if unit_id else None

        kwargs = {
            'unit': unit,
            'scheduled_date': parse_date(request.POST.get('scheduled_date', '').strip()) or None,
            'scheduled_start': request.POST.get('scheduled_start', '').strip() or None,
            'notes': request.POST.get('notes', '').strip(),
        }
        kind, _, raw_id = request.POST.get('assignee', '').partition('-')
        if kind == 'staff' and raw_id.isdigit():
            kwargs['assigned_staff_id'] = int(raw_id)
            kwargs['status'] = Visit.Status.SCHEDULED
        elif kind == 'contact' and raw_id.isdigit():
            kwargs['assigned_contact_id'] = int(raw_id)
            kwargs['status'] = Visit.Status.SCHEDULED

        ready_by_date = request.POST.get('ready_by_date', '').strip()
        if ready_by_date:
            parsed_date = parse_date(ready_by_date)
            ready_by_time = request.POST.get('ready_by_time', '').strip()
            hour, _, minute = ready_by_time.partition(':')
            time_part = datetime.min.time().replace(
                hour=int(hour) if hour.isdigit() else 17, minute=int(minute) if minute.isdigit() else 0,
            )
            if parsed_date:
                kwargs['ready_by'] = timezone.make_aware(datetime.combine(parsed_date, time_part))

        visit = checklist_service.create_visit(
            prop, visit_type, is_deep_clean=request.POST.get('is_deep_clean') == '1', **kwargs,
        )
        if visit.assigned_staff_id or visit.assigned_contact_id:
            notify_service.notify_assignee(visit, request)
        messages.success(request, f'Visit scheduled for {prop.name}{f" — {unit.label}" if unit else ""}.')
        return redirect('onsite_visit_detail', pk=visit.pk)

    return render(request, 'onsite/visit_create.html', {
        'str_properties': str_properties, 'other_properties': other_properties, 'visit_types': visit_types,
        'staff_options': staff_options, 'contact_options': contact_options,
        'units_by_property_json': units_by_property_json,
    })


@login_required
def visit_rule_list(request):
    """Staff-facing management screen for VisitRule — the recurring
    generation path for deep cleans/inspections (see
    generate_scheduled_visits). Previously reachable only through Django
    admin's plain model form; this is the first real staff-facing UI for
    it, built specifically so a multi-unit property can get one rule per
    unit instead of being stuck with property-wide-only scheduling. A
    single list-plus-inline-add-form page, matching property_detail.html's
    Units/System Locations card pattern, rather than separate create/edit
    pages. A rule's cadence/assignee can be edited inline (see
    update_rule below); property/unit/visit_type stay fixed once created
    — delete-and-re-add is still the clearer path for changing those,
    since it's a bigger structural change (different checklist scope).

    Open to any active property type, not just STR — a weekly commercial
    common-area clean is exactly what this is for, alongside the
    STR-focused deep-clean/inspection rules it originally shipped with.
    STR is the default-visible property pool; everything else is behind
    "show other property types" (see _visit_eligible_properties())."""
    str_properties, other_properties = _visit_eligible_properties()
    visit_types = VisitType.objects.filter(is_active=True, is_addon=False).order_by('name')
    staff_options = StaffProfile.objects.select_related('user').filter(
        user__is_active=True, role=StaffProfile.Role.CLEANER,
    )
    units_by_property_json = _units_by_property_json()

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'add_rule':
            prop = get_object_or_404(Property, pk=request.POST.get('property'), is_active=True) \
                if request.POST.get('property') else None
            visit_type = get_object_or_404(
                VisitType, pk=request.POST.get('visit_type'), is_addon=False,
            ) if request.POST.get('visit_type') else None
            if not prop or not visit_type:
                messages.error(request, 'Choose a property and a visit type.')
                return redirect('onsite_visit_rule_list')

            unit_id = request.POST.get('unit') or None
            unit = prop.units.filter(pk=unit_id).first() if unit_id else None

            raw_value = request.POST.get('interval_value', '').strip()
            interval_value = int(raw_value) if raw_value.isdigit() and int(raw_value) > 0 else 3
            # "Every N weeks" is stored as interval_days (N*7) — see
            # VisitRule.interval_days's own docstring for why relativedelta
            # months can't express this. interval_months always keeps a
            # valid value regardless (the model field isn't nullable) but
            # is simply ignored by generate_scheduled_visits whenever
            # interval_days is set.
            if request.POST.get('interval_unit') == 'weeks':
                interval_kwargs = {'interval_days': interval_value * 7, 'interval_months': 3}
            else:
                interval_kwargs = {'interval_days': None, 'interval_months': interval_value}

            default_assignee = None
            kind, _, raw_id = request.POST.get('default_assignee', '').partition('-')
            if kind == 'staff' and raw_id.isdigit():
                default_assignee = StaffProfile.objects.filter(pk=raw_id).first()

            rule = VisitRule.objects.create(
                property=prop, unit=unit, visit_type=visit_type,
                default_assignee=default_assignee, **interval_kwargs,
            )
            messages.success(
                request,
                f'Recurring rule added: {prop.name}{f" — {unit.label}" if unit else ""} — '
                f'{visit_type.name} every {rule.cadence_display()}.',
            )
        elif action == 'update_rule':
            # Cadence/assignee are the two fields staff actually need to
            # change day-to-day (e.g. bump a commercial clean from weekly
            # to biweekly, or reassign it) — property/unit/visit_type stay
            # fixed here since changing those is a big enough structural
            # change (different checklist, different scope) that delete-
            # and-re-add is still the clearer path for it.
            rule = get_object_or_404(VisitRule, pk=request.POST.get('rule_id'))
            raw_value = request.POST.get('interval_value', '').strip()
            interval_value = int(raw_value) if raw_value.isdigit() and int(raw_value) > 0 else 3
            if request.POST.get('interval_unit') == 'weeks':
                rule.interval_days, rule.interval_months = interval_value * 7, 3
            else:
                rule.interval_days, rule.interval_months = None, interval_value
            kind, _, raw_id = request.POST.get('default_assignee', '').partition('-')
            rule.default_assignee = (
                StaffProfile.objects.filter(pk=raw_id).first() if kind == 'staff' and raw_id.isdigit() else None
            )
            rule.save(update_fields=['interval_days', 'interval_months', 'default_assignee'])
            messages.success(request, f'Rule updated — now every {rule.cadence_display()}.')
        elif action == 'toggle_active':
            rule = get_object_or_404(VisitRule, pk=request.POST.get('rule_id'))
            rule.is_active = not rule.is_active
            rule.save(update_fields=['is_active'])
            messages.success(request, f'Rule {"resumed" if rule.is_active else "paused"}.')
        elif action == 'delete_rule':
            VisitRule.objects.filter(pk=request.POST.get('rule_id')).delete()
            messages.success(request, 'Rule deleted.')
        return redirect('onsite_visit_rule_list')

    rules = VisitRule.objects.select_related('property', 'unit', 'visit_type', 'default_assignee__user').order_by(
        'property__name', 'unit__label', 'visit_type__name',
    )
    return render(request, 'onsite/visit_rule_list.html', {
        'rules': rules, 'str_properties': str_properties, 'other_properties': other_properties, 'visit_types': visit_types,
        'staff_options': staff_options, 'units_by_property_json': units_by_property_json,
    })


@login_required
def visit_detail(request, pk):
    """Staff-facing management screen for one visit — reassign, edit the
    schedule/notes, override status, correct a checklist item, and (admin
    only) delete outright. The cleaner's own token link (visit_public)
    stays the primary place a checklist actually gets WORKED; this is
    where staff fix a mistake or manage the visit from the office side."""
    visit = get_object_or_404(Visit.objects.select_related('property', 'booking', 'next_booking'), pk=pk)
    is_admin = _is_admin(request.user)

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'reassign':
            kind, _, raw_id = request.POST.get('assignee', '').partition('-')
            if kind == 'staff' and raw_id.isdigit():
                visit.assigned_staff_id = int(raw_id)
                visit.assigned_contact = None
            elif kind == 'contact' and raw_id.isdigit():
                visit.assigned_contact_id = int(raw_id)
                visit.assigned_staff = None
            else:
                visit.assigned_staff = None
                visit.assigned_contact = None
            if visit.status == Visit.Status.UNASSIGNED and (visit.assigned_staff_id or visit.assigned_contact_id):
                visit.status = Visit.Status.SCHEDULED
            visit.full_clean()
            visit.save()
            if visit.assigned_staff_id or visit.assigned_contact_id:
                notify_service.notify_assignee(visit, request)
            messages.success(request, 'Visit reassigned.')

        elif action == 'save_schedule':
            raw_date = request.POST.get('scheduled_date', '').strip()
            visit.scheduled_date = parse_date(raw_date) if raw_date else None
            raw_start = request.POST.get('scheduled_start', '').strip()
            visit.scheduled_start = raw_start or None
            visit.notes = request.POST.get('notes', '').strip()
            visit.save(update_fields=['scheduled_date', 'scheduled_start', 'notes'])
            messages.success(request, 'Visit updated.')

        elif action == 'set_status':
            new_status = request.POST.get('status')
            if new_status in Visit.Status.values:
                visit.status = new_status
                visit.save(update_fields=['status'])
                # A manual override lands here instead of checklist_service.
                # submit_visit() (that's the cleaner's own Submit tap on
                # visit_public) — without this, pushing a visit straight to
                # Submitted/Verified by hand silently skipped converting its
                # reported issues into tickets. Safe to call regardless of
                # which status it's headed to: create_issue_tickets only
                # ever touches issues that don't already have one.
                if new_status in (Visit.Status.SUBMITTED, Visit.Status.VERIFIED):
                    checklist_service.create_issue_tickets(visit, created_by=request.user)
                messages.success(request, f'Status updated to {visit.get_status_display()}.')

        elif action == 'toggle_checklist_item':
            item = get_object_or_404(VisitChecklistItem, pk=request.POST.get('item_id'), visit=visit)
            item.is_completed = not item.is_completed
            item.completed_at = timezone.now() if item.is_completed else None
            item.save(update_fields=['is_completed', 'completed_at'])

        elif action == 'toggle_deep_clean':
            try:
                checklist_service.set_deep_clean(visit, enabled=request.POST.get('enabled') == '1')
                messages.success(
                    request,
                    'Deep clean extras added to this visit.' if visit.is_deep_clean else 'Deep clean extras removed.',
                )
            except ValidationError as e:
                messages.error(request, '; '.join(e.messages) if hasattr(e, 'messages') else str(e))

        elif action == 'delete':
            if not is_admin:
                messages.error(request, 'Only an admin can delete a visit.')
                return redirect('onsite_visit_detail', pk=visit.pk)
            visit.delete()
            messages.success(request, 'Visit deleted.')
            return redirect('onsite_dashboard')

        return redirect('onsite_visit_detail', pk=visit.pk)

    # Scoped to actual cleaners, not every active staff member / every
    # vendor of every trade — a plumber or an accountant has no business
    # showing up as a candidate to assign a cleaning to. In-house staff
    # means role=CLEANER specifically; outside contractors means a Vendor
    # whose trade is (some form of) cleaning — matched loosely
    # (icontains, not an exact 'Cleaning') so "House Cleaning" or
    # "Turnover Cleaning" still counts, not just the bubble-picker's exact
    # canonical wording. On-site Staff contacts stay unfiltered — that
    # whole contact type exists specifically for on-property staff without
    # a portal login (frequently the cleaner), so it's already scoped by
    # definition. Whoever the visit is CURRENTLY assigned to always stays
    # in the list even if they don't match — so an existing legacy/atypical
    # assignment still renders correctly rather than showing a blank chip.
    staff_options = StaffProfile.objects.select_related('user').filter(
        Q(user__is_active=True, role=StaffProfile.Role.CLEANER) | Q(pk=visit.assigned_staff_id),
    )
    contact_options = Contact.objects.filter(
        Q(contact_type=Contact.ContactType.VENDOR, trade__icontains='clean')
        | Q(contact_type=Contact.ContactType.ON_SITE_STAFF)
        | Q(pk=visit.assigned_contact_id),
    )

    media_qs = list(visit.media.select_related('checklist_item', 'issue'))
    # Plain-dict form for the lightbox's json_script blob — a queryset of
    # model instances isn't JSON-serializable, and this is also where "which
    # checklist line was this photo actually for" gets resolved into a
    # caption, once, instead of re-deriving it in the template for both the
    # thumbnail grid and the lightbox.
    media_gallery = [
        {
            'url': m.file.url,
            'is_video': m.media_type == VisitMedia.MediaType.VIDEO,
            'caption': (
                m.checklist_item.text if m.checklist_item_id
                else f'Issue: {m.issue.description}' if m.issue_id
                else 'General'
            ),
        }
        for m in media_qs
    ]

    return render(request, 'onsite/visit_detail.html', {
        'visit': visit,
        'status_choices': Visit.Status.choices,
        'checklist_items': list(visit.checklist_items.all()),
        'media': media_qs,
        'media_gallery': media_gallery,
        'issues': visit.issues.select_related('created_ticket'),
        'staff_options': staff_options,
        'contact_options': contact_options,
        'is_admin': is_admin,
    })


def _client_ip(request):
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '0.0.0.0')


def _item_payload(item):
    """One VisitChecklistItem's state, as JSON — everything the page-local
    script in visit_public.html needs to redraw an item's Done/Skip chip
    and photo-required note in place, without a reload."""
    return {
        'id': item.pk,
        'is_completed': item.is_completed,
        'skip_reason': item.skip_reason,
        'note': item.note,
    }


def _media_payload(media):
    return {'id': media.pk, 'url': media.file.url, 'is_video': media.media_type == VisitMedia.MediaType.VIDEO}


@require_http_methods(['GET', 'POST'])
def visit_public(request, token):
    """The cleaner-facing no-login page — same shape as vendorportal's
    token-keyed ticket view. The checklist doesn't render until the visit
    is started (see ONSITE_DESIGN.md), which is what makes "Start" a real
    signal rather than a formality.

    Every per-item/per-row action (checklist done/skip/note/photo, issue
    report, supply reading) is AJAX-only in practice — visit_public.html's
    script always sends X-Requested-With — so a cleaner working through a
    40+ item checklist never loses their scroll position to a full page
    reload after every single tap. The non-AJAX branch below is kept only
    as a plain-POST fallback; Start and Submit stay plain POST+redirect on
    purpose, since those genuinely change what's on the whole page."""
    # 30 requests / 5 minutes (the shared default) is calibrated for
    # vendorportal's lighter completion-link flow, not a 40+ item checklist
    # where marking items done/skipped and uploading several photos
    # legitimately generates far more requests than that in normal use —
    # this was the actual cause of the "Too many requests" errors cleaners
    # were hitting. This endpoint only ever serves one real person at a
    # time (whoever holds the token), so a much higher ceiling is still a
    # meaningful guard against automated abuse without throttling normal work.
    if AccessAttempt.is_rate_limited(_client_ip(request), limit=600, window_minutes=5):
        return HttpResponse('Too many requests. Please try again later.', status=429)

    visit = get_object_or_404(Visit, access_token=token)
    if not visit.is_access_token_valid():
        return render(request, 'onsite/visit_public_expired.html', status=410)

    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'start' and not visit.started_at:
            # The button reads "Get Code" (visit_public.html) whenever
            # visit.unit.access_code is set — this single action still just
            # marks the visit started; the template's own started_at check
            # is what reveals that unit's code, tying "cleaner retrieved the
            # code" and "cleaner started" together as one real signal
            # instead of two separate, fakeable steps.
            visit.started_at = timezone.now()
            visit.status = Visit.Status.IN_PROGRESS
            visit.save(update_fields=['started_at', 'status'])
            return redirect('onsite_visit_public', token=token)

        elif action == 'mark_item_done':
            # Each of these action branches touches only the field(s) it
            # owns — deliberately NOT one shared "update_item" branch that
            # overwrites note/skip_reason/is_completed from whatever
            # happened to be in the POST body every time. This page renders
            # each as its own small independent action, so a single-purpose
            # "Done" tap must never blank out a note that isn't part of it.
            item = get_object_or_404(VisitChecklistItem, pk=request.POST.get('item_id'), visit=visit)
            item.is_completed = True
            item.skip_reason = ''
            item.completed_at = timezone.now()
            item.save(update_fields=['is_completed', 'skip_reason', 'completed_at'])
            if is_ajax:
                done, total = _checklist_progress(visit)
                return JsonResponse({'success': True, 'item': _item_payload(item), 'done': done, 'total': total})

        elif action == 'reopen_item':
            # Undoes either a Done tap or a Skip — one action, since both are
            # "closed" states a cleaner might want to back out of.
            item = get_object_or_404(VisitChecklistItem, pk=request.POST.get('item_id'), visit=visit)
            item.is_completed = False
            item.skip_reason = ''
            item.completed_at = None
            item.save(update_fields=['is_completed', 'skip_reason', 'completed_at'])
            if is_ajax:
                done, total = _checklist_progress(visit)
                return JsonResponse({'success': True, 'item': _item_payload(item), 'done': done, 'total': total})

        elif action == 'skip_item':
            item = get_object_or_404(VisitChecklistItem, pk=request.POST.get('item_id'), visit=visit)
            reason = request.POST.get('skip_reason', '').strip()
            if reason:
                item.skip_reason = reason
                item.is_completed = False
                item.completed_at = None
                item.save(update_fields=['skip_reason', 'is_completed', 'completed_at'])
                if is_ajax:
                    done, total = _checklist_progress(visit)
                    return JsonResponse({'success': True, 'item': _item_payload(item), 'done': done, 'total': total})
            elif is_ajax:
                return JsonResponse({'success': False, 'error': 'Enter a reason before confirming the skip.'})

        elif action == 'note_item':
            item = get_object_or_404(VisitChecklistItem, pk=request.POST.get('item_id'), visit=visit)
            item.note = request.POST.get('note', '').strip()
            item.save(update_fields=['note'])
            if is_ajax:
                return JsonResponse({'success': True, 'item': _item_payload(item)})

        elif action == 'upload_item_photo':
            item = get_object_or_404(VisitChecklistItem, pk=request.POST.get('item_id'), visit=visit)
            # getlist, not .get — the file input now allows selecting (or
            # capturing) more than one photo/video at once, uploaded together
            # as soon as they're picked rather than one at a time. In
            # practice checklist-actions.js always sends exactly one per
            # request (see uploadOnePhoto) so each gets its own independent
            # pass/fail, but this handles a genuine multi-file POST too.
            photos = request.FILES.getlist('photo')
            created = []
            errors = []
            # Same limits as the vendor portal's own phone-camera upload —
            # this is the same real situation (a cleaner's phone, JPEG/HEIC
            # photos or a short video), and this endpoint had never had any
            # size/type check at all: an oversized video (a full-length 4K
            # walkthrough clip is easily 200MB+) or an odd content-type
            # would previously just fail with no real error surfaced —
            # exactly "photo upload doesn't work" with nothing to go on.
            for photo in photos:
                if photo.content_type not in settings.VENDOR_UPLOAD_ALLOWED_CONTENT_TYPES:
                    errors.append(f'{photo.name}: unsupported file type ({photo.content_type or "unknown"}).')
                    continue
                if photo.size > settings.VENDOR_UPLOAD_MAX_BYTES:
                    max_mb = settings.VENDOR_UPLOAD_MAX_BYTES // (1024 * 1024)
                    errors.append(f'{photo.name} is too large (max {max_mb}MB).')
                    continue
                try:
                    media = VisitMedia.objects.create(
                        visit=visit, checklist_item=item, file=photo,
                        media_type=VisitMedia.MediaType.VIDEO if photo.content_type.startswith('video') else VisitMedia.MediaType.PHOTO,
                    )
                except Exception:
                    # Whatever the actual cause (storage backend rejecting
                    # it, a network blip talking to Cloudinary, etc.) — log
                    # the real traceback for us, but never leave the cleaner
                    # facing a bare 500 with an unparseable-as-JSON response
                    # (that's what silently produced the old "tap to retry,
                    # fails again, no explanation" loop).
                    logger.exception('onsite upload_item_photo failed for visit %s item %s', visit.pk, item.pk)
                    errors.append(f'{photo.name}: upload failed on our end — try again in a moment.')
                    continue
                created.append(media)
            if is_ajax:
                if not created:
                    return JsonResponse({
                        'success': False,
                        'error': errors[0] if errors else 'No file received — try again.',
                    })
                return JsonResponse({
                    'success': True, 'item_id': item.pk,
                    'media': [_media_payload(m) for m in created],
                    'errors': errors,
                })

        elif action == 'add_issue':
            description = request.POST.get('description', '').strip()
            if description:
                issue = VisitIssue.objects.create(visit=visit, description=description)
                issue_errors = []
                # Same validation/error-handling as the checklist item photo
                # upload above — same real risk (an oversized video, or a
                # storage-backend hiccup) previously left with no check at all.
                for photo in request.FILES.getlist('photos'):
                    if photo.content_type not in settings.VENDOR_UPLOAD_ALLOWED_CONTENT_TYPES:
                        issue_errors.append(f'{photo.name}: unsupported file type ({photo.content_type or "unknown"}).')
                        continue
                    if photo.size > settings.VENDOR_UPLOAD_MAX_BYTES:
                        max_mb = settings.VENDOR_UPLOAD_MAX_BYTES // (1024 * 1024)
                        issue_errors.append(f'{photo.name} is too large (max {max_mb}MB).')
                        continue
                    try:
                        VisitMedia.objects.create(
                            visit=visit, issue=issue, file=photo,
                            media_type=VisitMedia.MediaType.VIDEO if photo.content_type.startswith('video') else VisitMedia.MediaType.PHOTO,
                        )
                    except Exception:
                        logger.exception('onsite add_issue photo upload failed for visit %s issue %s', visit.pk, issue.pk)
                        issue_errors.append(f'{photo.name}: upload failed on our end — the issue itself was still reported.')
                if is_ajax:
                    return JsonResponse({'success': True, 'description': issue.description, 'errors': issue_errors})
            elif is_ajax:
                return JsonResponse({'success': False, 'error': "Describe what's wrong first."})

        elif action == 'record_supply_reading':
            # Validated against this visit's own resolved supply list
            # (resolve_supplies(visit.property, visit.unit)) rather than
            # trusting the posted id directly — a tampered supply_item_id
            # for an item this property/unit doesn't actually resolve to
            # (hidden by an override, or just never on the standard list)
            # can't sneak a reading onto a row this visit was never shown.
            resolved_item_ids = {
                r['supply_item'].pk for r in supply_services.resolve_supplies(visit.property, visit.unit)
            }
            supply_item = get_object_or_404(
                SupplyItem.objects.filter(pk__in=resolved_item_ids),
                pk=request.POST.get('supply_item_id'), is_active=True,
            )
            level = request.POST.get('level')
            if level in SupplyReading.Level.values:
                supply_services.record_reading(visit, supply_item, level)
                if is_ajax:
                    return JsonResponse({
                        'success': True, 'supply_item_id': supply_item.pk,
                        'level': level, 'level_display': dict(SupplyReading.Level.choices)[level],
                    })
            elif is_ajax:
                return JsonResponse({'success': False, 'error': 'Invalid level.'})

        elif action == 'submit':
            try:
                checklist_service.submit_visit(visit)
                return redirect('onsite_visit_public', token=token)
            except checklist_service.VisitSubmitBlocked as e:
                # Rendered directly here (not the generic redirect below) so
                # the blocking item ids — only available on this exception,
                # only known right now — can flow straight into this same
                # response instead of needing to survive a redirect (session
                # flash storage, or similar, for what's really just a
                # same-request rendering need).
                messages.error(request, str(e))
                return _render_visit_public(
                    request, visit,
                    blocking_item_ids=set(e.missing_item_ids) | set(e.missing_photo_item_ids),
                    blocking_photo_item_ids=set(e.missing_photo_item_ids),
                )
            except ValidationError as e:
                messages.error(request, '; '.join(e.messages) if hasattr(e, 'messages') else str(e))

        if is_ajax:
            return JsonResponse({'success': False, 'error': 'Something went wrong — try again.'})
        return redirect('onsite_visit_public', token=token)

    return _render_visit_public(request, visit)


def _render_visit_public(request, visit, blocking_item_ids=None, blocking_photo_item_ids=None):
    """Shared by the normal GET render and the submit-failed path above —
    same context either way, the latter just adds which items to highlight."""
    checklist_items = list(visit.checklist_items.all())
    return render(request, 'onsite/visit_public.html', {
        'visit': visit,
        'checklist_sections': _checklist_sections(checklist_items, blocking_item_ids),
        'checklist_done_count': sum(1 for i in checklist_items if i.is_completed or i.skip_reason),
        'checklist_total_count': len(checklist_items),
        'issues': visit.issues.all(),
        'supply_rows': supply_services.supply_check_context(visit),
        'is_submitted': visit.status in (Visit.Status.SUBMITTED, Visit.Status.VERIFIED),
        'blocking_item_ids': blocking_item_ids or set(),
        'blocking_photo_item_ids': blocking_photo_item_ids or set(),
    })


def _checklist_progress(visit):
    """(done, total) for the AJAX action responses — a fresh query, not
    whatever might already be loaded on `visit`, so it reflects the save
    that just happened above it in the same request."""
    items = list(visit.checklist_items.all())
    done = sum(1 for i in items if i.is_completed or i.skip_reason)
    return done, len(items)


def _checklist_sections(checklist_items, blocking_item_ids=None):
    """Groups an already section-ordered list of VisitChecklistItems (see
    VisitChecklistItem.Meta's ordering note) into
    [{name, items, done_count, total_count, has_blocking}, ...] for
    visit_public.html's collapsible-section layout — a "resolved" item (done
    OR skipped, same definition submit_visit's gate uses) counts toward
    done_count so the per-section badge and the top progress bar agree with
    what actually blocks submission.

    has_blocking is separate from done_count on purpose: an item can be
    marked Done (is_completed=True, counts toward done_count) while still
    missing a required photo — nothing stops a cleaner from tapping Done
    without attaching one. Without has_blocking, that section would look
    fully resolved and default-collapse, hiding the very item a failed
    Submit is complaining about. blocking_item_ids comes from
    checklist_service.VisitSubmitBlocked, so this stays in sync with
    submit_visit's own gate rather than recomputing it separately."""
    blocking_item_ids = blocking_item_ids or set()
    sections = []
    current = None
    for item in checklist_items:
        name = item.section or 'Checklist'
        if current is None or current['name'] != name:
            current = {'name': name, 'items': [], 'done_count': 0, 'total_count': 0, 'has_blocking': False}
            sections.append(current)
        current['items'].append(item)
        current['total_count'] += 1
        if item.is_completed or item.skip_reason:
            current['done_count'] += 1
        if item.pk in blocking_item_ids:
            current['has_blocking'] = True
    return sections


@require_http_methods(['POST'])
def visit_public_signature(request, token):
    """Upload target for static/js/signature-pad.js (already built for the
    processes app) — generic multipart file+caption POST, reused as-is.

    Same raised limit as visit_public itself, and for the same reason:
    AccessAttempt.is_rate_limited counts by IP across every view that
    calls it (there's no per-endpoint counter), so leaving this one at
    the shared 30-requests/5-minutes default meant a cleaner who'd
    already made 30+ AJAX checklist requests (trivial on a 40+ item
    checklist under the per-action design) had this endpoint pre-tripped
    by the time they reached Submit — surfacing as "Could not save
    signature" right at the end of a visit, the worst possible place for
    it to fail."""
    if AccessAttempt.is_rate_limited(_client_ip(request), limit=600, window_minutes=5):
        return HttpResponse('Too many requests.', status=429)

    visit = get_object_or_404(Visit, access_token=token)
    if not visit.is_access_token_valid():
        return HttpResponse('Link expired.', status=410)

    file = request.FILES.get('file')
    if file:
        visit.signature_image = file
        visit.signed_name = request.POST.get('signed_name', '').strip()
        visit.signed_at = timezone.now()
        visit.signed_ip = _client_ip(request)
        visit.save(update_fields=['signature_image', 'signed_name', 'signed_at', 'signed_ip'])
    return HttpResponse('OK')


@login_required
def checklist_custom_items(request):
    """Admin screen listing every property-specific checklist addition
    across the portfolio, grouped by (visit_type, text) — the same wording
    showing up at several properties is the signal that it belongs in the
    standard list instead. See ONSITE_DESIGN.md: custom items are "the most
    valuable content in the system" and deliberately kept visible rather
    than buried, so promoting a repeated one is one click."""
    if not _is_admin(request.user):
        return redirect('onsite_dashboard')

    if request.method == 'POST' and request.POST.get('action') == 'promote':
        visit_type_id = request.POST.get('visit_type_id')
        text = request.POST.get('text', '').strip()
        matching = PropertyChecklistItem.objects.filter(visit_type_id=visit_type_id, text=text, is_active=True)
        if matching.exists():
            sample = matching.first()
            StandardChecklistItem.objects.get_or_create(
                visit_type_id=visit_type_id, text=text,
                defaults={'mandatory': sample.mandatory, 'requires_photo': sample.requires_photo},
            )
            count = matching.count()
            matching.delete()
            messages.success(request, f'Promoted "{text}" to the standard checklist ({count} property row(s) now inherit it).')
        return redirect('onsite_checklist_custom_items')

    groups = {}
    for item in PropertyChecklistItem.objects.filter(is_active=True).select_related('property', 'visit_type').order_by('visit_type', 'text', 'property__name'):
        key = (item.visit_type_id, item.text)
        groups.setdefault(key, {'visit_type': item.visit_type, 'text': item.text, 'items': []})
        groups[key]['items'].append(item)

    rows = sorted(groups.values(), key=lambda g: (-len(g['items']), g['visit_type'].name, g['text']))
    return render(request, 'onsite/checklist_custom_items.html', {'rows': rows})


@login_required
def cleaning_payments(request):
    """Admin-only: what we still owe for completed internal-cleaner
    cleanings, and a way to mark a batch of them paid. Deliberately NOT
    payroll — this is per-cleaning-job pay (what the cleaning cost),
    not an employee wage/salary record. Scoped to assigned_staff visits
    whose staff has role=CLEANER — an assigned_contact visit is an
    external/contract cleaner paid a different way entirely, out of scope
    here. Only SUBMITTED/VERIFIED visits count (nothing owed for one still
    scheduled or in progress).

    Unpaid amounts (payment_batch is null) come from
    Visit.cleaner_payout_amount(), resolved live against today's Property/
    Unit pricing — an edited price changes what's still owed. Once a
    cleaning is marked paid, its amount is locked into paid_amount at that
    moment and never recomputed — a real payment already made shouldn't
    silently reprice itself just because a property's rate changed later."""
    if not _is_admin(request.user):
        return redirect('onsite_dashboard')

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'mark_paid':
            visit_ids = request.POST.getlist('visit_ids')
            unpaid = Visit.objects.filter(
                pk__in=visit_ids, assigned_staff__role=StaffProfile.Role.CLEANER,
                status__in=[Visit.Status.SUBMITTED, Visit.Status.VERIFIED], payment_batch__isnull=True,
            ).select_related('property', 'unit')
            if not unpaid:
                messages.error(request, 'Nothing selected — pick at least one cleaning to mark as paid.')
            else:
                amounts = {v.pk: v.cleaner_payout_amount() for v in unpaid}
                unpriced = [v for v in unpaid if amounts[v.pk] is None]
                if unpriced:
                    names = ', '.join(f'{v.property.name}{f" — {v.unit.label}" if v.unit else ""} ({v.scheduled_date:%b %d})' for v in unpriced[:5])
                    messages.error(
                        request,
                        f'{len(unpriced)} selected cleaning(s) have no price set and can\'t be marked paid: {names}'
                        + ('…' if len(unpriced) > 5 else '')
                        + ' — set a Cleaning pay amount on the property (or unit) first, or deselect them.',
                    )
                else:
                    total = sum(amounts.values(), Decimal('0'))
                    note = request.POST.get('note', '').strip()
                    with transaction.atomic():
                        batch = CleaningPaymentBatch.objects.create(
                            paid_by=request.user, total_amount=total, note=note,
                        )
                        for visit in unpaid:
                            visit.payment_batch = batch
                            visit.paid_amount = amounts[visit.pk]
                        Visit.objects.bulk_update(unpaid, ['payment_batch', 'paid_amount'])
                    messages.success(request, f'Marked {len(unpaid)} cleaning(s) as paid — ${total:.2f} total.')
        elif action == 'undo_batch':
            batch = get_object_or_404(CleaningPaymentBatch, pk=request.POST.get('batch_id'))
            with transaction.atomic():
                count = batch.visits.update(payment_batch=None, paid_amount=None)
                batch.delete()
            messages.success(request, f'Undone — {count} cleaning(s) are back in the unpaid queue.')
        elif action == 'update_deep_clean_percent':
            percent = _parse_decimal(request.POST.get('deep_clean_fee_percent'))
            if percent is None or percent < 0:
                messages.error(request, 'Enter a percent of 0 or higher.')
            else:
                pricing = CleaningPricingSettings.get()
                pricing.deep_clean_fee_percent = percent
                pricing.save(update_fields=['deep_clean_fee_percent'])
                messages.success(request, f'Deep clean pay is now {percent}% of the normal cleaning fee.')
        return redirect('onsite_cleaning_payments')

    unpaid_visits = (
        Visit.objects.filter(
            assigned_staff__role=StaffProfile.Role.CLEANER,
            status__in=[Visit.Status.SUBMITTED, Visit.Status.VERIFIED],
            payment_batch__isnull=True,
        )
        .select_related('property', 'unit', 'assigned_staff__user', 'visit_type')
        .order_by('assigned_staff__user__first_name', 'assigned_staff__user__last_name', 'scheduled_date')
    )

    by_staff = {}
    unpriced_count = 0
    for visit in unpaid_visits:
        amount = visit.cleaner_payout_amount()
        bucket = by_staff.setdefault(
            visit.assigned_staff_id, {'staff': visit.assigned_staff, 'rows': [], 'total': Decimal('0')},
        )
        bucket['rows'].append({'visit': visit, 'amount': amount})
        if amount is None:
            unpriced_count += 1
        else:
            bucket['total'] += amount

    cleaner_rows = sorted(by_staff.values(), key=lambda b: str(b['staff']))
    recent_batches = CleaningPaymentBatch.objects.select_related('paid_by').prefetch_related('visits')[:15]

    return render(request, 'onsite/cleaning_payments.html', {
        'cleaner_rows': cleaner_rows, 'unpriced_count': unpriced_count,
        'visit_count': len(unpaid_visits), 'recent_batches': recent_batches,
        'deep_clean_fee_percent': CleaningPricingSettings.get().deep_clean_fee_percent,
    })


@login_required
def checklist_templates(request):
    """Site-side answer to "where do I build a checklist in the first
    place" — a VisitType picker/creator. Previously this only existed in
    Django admin. Viewing is open to any logged-in staff; creating a new
    visit type is admin-gated, same convention as visit_detail's delete
    card. See checklist_template_detail for actually editing one's items."""
    is_admin = _is_admin(request.user)

    if request.method == 'POST':
        if not is_admin:
            messages.error(request, 'Only an admin can create a visit type.')
            return redirect('onsite_checklist_templates')
        name = request.POST.get('name', '').strip()
        if not name:
            messages.error(request, 'Enter a name for the new visit type.')
        elif VisitType.objects.filter(name__iexact=name).exists():
            messages.error(request, f'A visit type named "{name}" already exists.')
        else:
            visit_type = VisitType.objects.create(name=name)
            messages.success(request, f'Created "{visit_type.name}" — add its checklist items below.')
            return redirect('onsite_checklist_template_detail', type_id=visit_type.pk)
        return redirect('onsite_checklist_templates')

    visit_types = VisitType.objects.annotate(item_count=Count('standard_items')).order_by('name')
    return render(request, 'onsite/checklist_templates.html', {'visit_types': visit_types, 'is_admin': is_admin})


@login_required
def checklist_template_detail(request, type_id):
    """Manage one VisitType's standard checklist — add/edit/reorder/delete
    items, and edit the visit type's own settings — without going through
    Django admin. Mirrors visit_detail.html's action-dispatch shape and
    admin-only mutation gating; GET stays open to any logged-in staff so
    non-admins can still see what a checklist actually contains.

    Deliberately does NOT expose StandardChecklistItem.required_attributes
    (the property-tag gating) — a narrower, less time-critical feature that
    still exists via Django admin for the rare case it's needed; everything
    staff touch day-to-day (add/edit/reorder/delete an item, tweak the
    visit type itself) is covered here."""
    visit_type = get_object_or_404(VisitType, pk=type_id)
    is_admin = _is_admin(request.user)

    if request.method == 'POST':
        if not is_admin:
            messages.error(request, 'Only an admin can edit a checklist.')
            return redirect('onsite_checklist_template_detail', type_id=visit_type.pk)
        action = request.POST.get('action')

        if action == 'update_type':
            name = request.POST.get('name', '').strip()
            if name:
                visit_type.name = name
            raw_duration = request.POST.get('default_duration_minutes', '').strip()
            if raw_duration.isdigit():
                visit_type.default_duration_minutes = int(raw_duration)
            visit_type.requires_deadline = request.POST.get('requires_deadline') == 'on'
            visit_type.is_active = request.POST.get('is_active') == 'on'
            visit_type.save()
            messages.success(request, 'Visit type updated.')

        elif action == 'add_item':
            text = request.POST.get('text', '').strip()
            section = request.POST.get('section', '').strip()
            if text:
                max_order = visit_type.standard_items.filter(section=section).aggregate(Max('order'))['order__max'] or 0
                StandardChecklistItem.objects.create(
                    visit_type=visit_type, text=text, section=section, order=max_order + 1,
                    mandatory=request.POST.get('mandatory') == 'on',
                    requires_photo=request.POST.get('requires_photo') == 'on',
                    requires_note=request.POST.get('requires_note') == 'on',
                )
                messages.success(request, 'Item added.')
            else:
                messages.error(request, 'Enter the item text.')

        elif action == 'update_item':
            item = get_object_or_404(StandardChecklistItem, pk=request.POST.get('item_id'), visit_type=visit_type)
            text = request.POST.get('text', '').strip()
            if text:
                item.text = text
            item.section = request.POST.get('section', '').strip()
            item.mandatory = request.POST.get('mandatory') == 'on'
            item.requires_photo = request.POST.get('requires_photo') == 'on'
            item.requires_note = request.POST.get('requires_note') == 'on'
            item.is_active = request.POST.get('is_active') == 'on'
            item.save()
            messages.success(request, 'Item updated.')

        elif action == 'delete_item':
            StandardChecklistItem.objects.filter(pk=request.POST.get('item_id'), visit_type=visit_type).delete()
            messages.success(request, 'Item deleted.')

        elif action == 'move_item':
            item = get_object_or_404(StandardChecklistItem, pk=request.POST.get('item_id'), visit_type=visit_type)
            direction = request.POST.get('direction')
            neighbors = visit_type.standard_items.filter(section=item.section)
            neighbor = (
                neighbors.filter(order__lt=item.order).order_by('-order').first() if direction == 'up'
                else neighbors.filter(order__gt=item.order).order_by('order').first()
            )
            if neighbor:
                item.order, neighbor.order = neighbor.order, item.order
                item.save(update_fields=['order'])
                neighbor.save(update_fields=['order'])

        return redirect('onsite_checklist_template_detail', type_id=visit_type.pk)

    items = list(visit_type.standard_items.order_by('section', 'order'))
    sections = list(dict.fromkeys(item.section for item in items if item.section))
    return render(request, 'onsite/checklist_template_detail.html', {
        'visit_type': visit_type, 'items': items, 'sections': sections, 'is_admin': is_admin,
    })
