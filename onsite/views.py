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

from .importers import BookingFileError, parse_booking_file
from .models import Booking, ImportBatch, PropertyChecklistItem, StandardChecklistItem, Visit, VisitChecklistItem, VisitIssue, VisitMedia
from .services import checklist as checklist_service
from .services.bookings import apply_bookings, diff_bookings

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


@login_required
def booking_import_upload(request):
    """Phase 1 of the two-phase import: parse the uploaded file, diff it
    against existing Booking rows, and show the diff — nothing is written
    to Booking/Visit yet. The file itself is saved onto a not-yet-applied
    ImportBatch so booking_import_apply can act on exactly what was
    previewed without re-uploading."""
    str_properties = Property.objects.filter(
        property_type=Property.Type.SHORT_TERM_RENTAL, is_active=True,
    ).order_by('name')

    if request.method == 'POST':
        property_id = request.POST.get('property')
        source = request.POST.get('source')
        uploaded_file = request.FILES.get('file')

        if not (property_id and source and uploaded_file):
            messages.error(request, 'Choose a property, a source, and a file.')
            return render(request, 'onsite/booking_import_upload.html', {'properties': str_properties})

        property = get_object_or_404(Property, pk=property_id)
        try:
            raw_bookings = parse_booking_file(uploaded_file)
            diff = diff_bookings(property, source, raw_bookings)
        except BookingFileError as e:
            messages.error(request, str(e))
            return render(request, 'onsite/booking_import_upload.html', {'properties': str_properties})

        covers_start = min(r.check_out for r in raw_bookings)
        covers_end = max(r.check_out for r in raw_bookings)
        batch = ImportBatch.objects.create(
            property=property, source=source, raw_file=uploaded_file,
            covers_start=covers_start, covers_end=covers_end, imported_by=request.user,
        )
        return render(request, 'onsite/booking_import_preview.html', {
            'batch': batch, 'property': property, 'diff': diff,
        })

    return render(request, 'onsite/booking_import_upload.html', {'properties': str_properties})


@login_required
def booking_import_apply(request, batch_id):
    """Phase 2: staff confirmed the diff shown by booking_import_upload —
    re-parses the same saved file (rather than trusting anything from the
    client) and writes the changes."""
    batch = get_object_or_404(ImportBatch, pk=batch_id, applied_at__isnull=True)
    if request.method != 'POST':
        return redirect('onsite_booking_import')

    try:
        raw_bookings = parse_booking_file(batch.raw_file)
    except BookingFileError as e:
        messages.error(request, f'Could not re-read the saved file: {e}')
        return redirect('onsite_booking_import')

    new_count, changed_count, cancelled_count, visit_note = apply_bookings(
        batch.property, batch.source, raw_bookings, batch,
    )
    messages.success(
        request,
        f'Imported: {new_count} new, {changed_count} changed, {cancelled_count} cancelled.',
    )
    if visit_note:
        messages.warning(request, visit_note)
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
