from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from core.models import Property
from core.views import _is_admin
from supplies.models import SupplyRequest
from vendorportal.models import AccessAttempt

from .importers import BookingFileError, detect_format, parse_booking_file
from .models import Booking, ImportBatch, PropertyChecklistItem, StandardChecklistItem, Visit, VisitChecklistItem, VisitIssue, VisitMedia
from .services import checklist as checklist_service
from .services.bookings import (
    apply_bookings_for_property, check_listing_name_conflict, diff_bookings, resolve_listing_names, save_listing_name,
)

# How far ahead of ready_by a visit starts showing as at-risk — a heads-up
# window, not just "already late". Not user-configurable; a fixed buffer is
# enough for a company this size and avoids a settings screen for one number.
AT_RISK_BUFFER_MINUTES = 90


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


@login_required
def dashboard(request):
    today = timezone.localdate()
    now = timezone.now()

    todays_visits = list(
        Visit.objects.filter(scheduled_date=today)
        .exclude(status__in=[Visit.Status.CANCELLED, Visit.Status.SKIPPED])
        .select_related('property', 'visit_type', 'assigned_staff__user', 'assigned_contact')
        .order_by('ready_by')
    )
    for visit in todays_visits:
        visit.derived_status = _visit_status(visit, now)

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
        'todays_visits': todays_visits,
        'checkouts_missing_visit': checkouts_missing_visit,
        'unassigned_visits': unassigned_visits,
        'at_risk_visits': at_risk_visits,
        'is_admin': _is_admin(request.user),
    })


def _str_properties():
    return Property.objects.filter(property_type=Property.Type.SHORT_TERM_RENTAL, is_active=True).order_by('name')


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
            'unresolved': posted is not None and not posted_property_id,
            'conflict': conflict,
        })

    return {
        'batch': batch, 'portfolio': True, 'source': source,
        'property_diffs': property_diffs, 'unmatched_groups': unmatched_groups,
    }


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
    resubmits the file."""
    str_properties = _str_properties()

    if request.method == 'POST':
        property_id = request.POST.get('property')
        source = request.POST.get('source')
        uploaded_file = request.FILES.get('file')

        if not (source and uploaded_file):
            messages.error(request, 'Choose a source and a file.')
            return render(request, 'onsite/booking_import_upload.html', {'properties': str_properties})

        try:
            fmt = detect_format(uploaded_file.name)
            raw_bookings = parse_booking_file(uploaded_file)
        except BookingFileError as e:
            messages.error(request, str(e))
            return render(request, 'onsite/booking_import_upload.html', {'properties': str_properties})

        portfolio_mode = fmt == 'csv' and not property_id and any(r.listing_name for r in raw_bookings)
        covers_start = min(r.check_out for r in raw_bookings)
        covers_end = max(r.check_out for r in raw_bookings)

        if not portfolio_mode and not property_id:
            messages.error(
                request,
                'Choose a property — only a portfolio-wide .csv with a listing/property column can skip this.',
            )
            return render(request, 'onsite/booking_import_upload.html', {'properties': str_properties})

        property = get_object_or_404(Property, pk=property_id) if property_id else None
        batch = ImportBatch.objects.create(
            property=property, source=source, raw_file=uploaded_file,
            covers_start=covers_start, covers_end=covers_end, imported_by=request.user,
        )
        return redirect('onsite_booking_import_preview', batch_id=batch.pk)

    return render(request, 'onsite/booking_import_upload.html', {'properties': str_properties})


@login_required
def booking_import_preview(request, batch_id):
    """Re-shows the diff for a not-yet-applied batch by re-parsing its saved
    file — the GET counterpart to booking_import_upload's POST, so the
    preview has a real URL to land on (after the initial upload, or after a
    quick_add_property round trip) rather than only existing as an inline
    render of the upload POST."""
    batch = get_object_or_404(ImportBatch, pk=batch_id, applied_at__isnull=True)
    try:
        raw_bookings = parse_booking_file(batch.raw_file)
    except BookingFileError as e:
        messages.error(request, f'Could not re-read the saved file: {e}')
        return redirect('onsite_booking_import')

    if batch.property_id:
        diff = diff_bookings(batch.property, batch.source, raw_bookings)
        return render(request, 'onsite/booking_import_preview.html', {
            'batch': batch, 'property': batch.property, 'diff': diff, 'portfolio': False,
        })

    return render(
        request, 'onsite/booking_import_preview.html',
        _portfolio_preview_context(batch, raw_bookings, batch.source),
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

    if batch.property_id:
        new_count, changed_count, cancelled_count, visit_note = apply_bookings_for_property(
            batch.property, batch.source, raw_bookings,
        )
        batch.new_count, batch.changed_count, batch.cancelled_count = new_count, changed_count, cancelled_count
        batch.applied_at = timezone.now()
        batch.save(update_fields=['new_count', 'changed_count', 'cancelled_count', 'applied_at'])
        messages.success(request, f'Imported: {new_count} new, {changed_count} changed, {cancelled_count} cancelled.')
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
        conflict = check_listing_name_conflict(property, batch.source, listing_name)
        if conflict and conflict['type'] == 'additional' and request.POST.get(f'confirm_{i}'):
            conflict = None
        if conflict:
            blocked = True
        else:
            pending_mappings.append((listing_name, property, rows))

    if blocked:
        messages.error(
            request,
            'Every listing name needs to be mapped to a property (and any conflicts resolved) before this import can be applied.',
        )
        context = _portfolio_preview_context(batch, raw_bookings, batch.source, posted=request.POST)
        return render(request, 'onsite/booking_import_preview.html', context)

    for listing_name, property, rows in pending_mappings:
        save_listing_name(property, batch.source, listing_name)
        matched[property] = matched.get(property, []) + rows

    total_new = total_changed = total_cancelled = 0
    visit_notes = set()
    for property, rows in matched.items():
        n, c, x, note = apply_bookings_for_property(property, batch.source, rows)
        total_new += n
        total_changed += c
        total_cancelled += x
        if note:
            visit_notes.add(note)

    batch.new_count, batch.changed_count, batch.cancelled_count = total_new, total_changed, total_cancelled
    batch.applied_at = timezone.now()
    batch.save(update_fields=['new_count', 'changed_count', 'cancelled_count', 'applied_at'])

    property_word = 'property' if len(matched) == 1 else 'properties'
    messages.success(
        request,
        f'Imported: {total_new} new, {total_changed} changed, {total_cancelled} cancelled, '
        f'across {len(matched)} {property_word}.',
    )
    for note in visit_notes:
        messages.warning(request, note)
    return redirect('onsite_dashboard')


@login_required
def visit_detail(request, pk):
    visit = get_object_or_404(Visit, pk=pk)
    return render(request, 'onsite/visit_detail.html', {'visit': visit})


def _client_ip(request):
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '0.0.0.0')


@require_http_methods(['GET', 'POST'])
def visit_public(request, token):
    """The cleaner-facing no-login page — same shape as vendorportal's
    token-keyed ticket view. The checklist doesn't render until the visit
    is started (see ONSITE_DESIGN.md), which is what makes "Start" a real
    signal rather than a formality."""
    if AccessAttempt.is_rate_limited(_client_ip(request)):
        return HttpResponse('Too many requests. Please try again later.', status=429)

    visit = get_object_or_404(Visit, access_token=token)
    if not visit.is_access_token_valid():
        return render(request, 'onsite/visit_public_expired.html', status=410)

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'start' and not visit.started_at:
            visit.started_at = timezone.now()
            visit.status = Visit.Status.IN_PROGRESS
            visit.save(update_fields=['started_at', 'status'])

        elif action == 'update_item':
            item = get_object_or_404(VisitChecklistItem, pk=request.POST.get('item_id'), visit=visit)
            skip_reason = request.POST.get('skip_reason', '').strip()
            item.note = request.POST.get('note', '').strip()
            item.skip_reason = skip_reason
            if skip_reason:
                item.is_completed = False
            else:
                item.is_completed = bool(request.POST.get('is_completed'))
            item.completed_at = timezone.now() if item.is_completed else None
            item.save(update_fields=['note', 'skip_reason', 'is_completed', 'completed_at'])
            photo = request.FILES.get('photo')
            if photo:
                VisitMedia.objects.create(
                    visit=visit, checklist_item=item, file=photo,
                    media_type=VisitMedia.MediaType.VIDEO if photo.content_type.startswith('video') else VisitMedia.MediaType.PHOTO,
                )

        elif action == 'add_issue':
            description = request.POST.get('description', '').strip()
            if description:
                issue = VisitIssue.objects.create(visit=visit, description=description)
                for photo in request.FILES.getlist('photos'):
                    VisitMedia.objects.create(visit=visit, issue=issue, file=photo)

        elif action == 'add_supply_request':
            text = request.POST.get('supplies_text', '').strip()
            if text:
                SupplyRequest.objects.get_or_create(
                    property=visit.property, source_reference=f'onsite-visit-{visit.pk}', item_guess='',
                    defaults={'raw_text': text},
                )

        elif action == 'submit':
            try:
                checklist_service.submit_visit(visit)
                return redirect('onsite_visit_public', token=token)
            except ValidationError as e:
                messages.error(request, '; '.join(e.messages) if hasattr(e, 'messages') else str(e))

        return redirect('onsite_visit_public', token=token)

    checklist_items = list(visit.checklist_items.all())
    return render(request, 'onsite/visit_public.html', {
        'visit': visit,
        'checklist_items': checklist_items,
        'issues': visit.issues.all(),
        'is_submitted': visit.status in (Visit.Status.SUBMITTED, Visit.Status.VERIFIED),
    })


@require_http_methods(['POST'])
def visit_public_signature(request, token):
    """Upload target for static/js/signature-pad.js (already built for the
    processes app) — generic multipart file+caption POST, reused as-is."""
    if AccessAttempt.is_rate_limited(_client_ip(request)):
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
