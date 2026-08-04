"""Checklist resolution and the Visit lifecycle. See ONSITE_DESIGN.md's "The
checklist model" section for the full rationale — a property's checklist is
never stored; it's computed from the standard reservoir plus that
property's overrides/additions, and only materialized (copied) once, at
Visit creation, into VisitChecklistItem rows."""
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from ..models import (
    PropertyChecklistItem,
    PropertyChecklistOverride,
    PropertyChecklistReview,
    StandardChecklistItem,
    Visit,
    VisitChecklistItem,
)


def resolve_checklist(property, visit_type):
    """Returns an ordered list of dicts describing what this property's
    checklist for this visit type looks like right now — the live,
    computed view (not yet tied to any particular Visit). Each dict has:
    source ('standard'/'property'), section, order, text, mandatory,
    requires_photo, requires_note, is_new_unreviewed, and (for 'standard'
    items) standard_item for the caller to look up its override.

    Resolution order: active StandardChecklistItems whose required_attributes
    are all present on the property, minus anything hidden by a
    PropertyChecklistOverride, with mandatory/order overridden where set;
    then that property's own PropertyChecklistItems appended."""
    property_attribute_ids = set(
        property.attribute_assignments.values_list('attribute_id', flat=True)
    )
    overrides_by_item = {
        o.standard_item_id: o
        for o in PropertyChecklistOverride.objects.filter(property=property, visit_type=visit_type)
    }
    try:
        review = PropertyChecklistReview.objects.get(property=property, visit_type=visit_type)
        reviewed_at = review.reviewed_at
    except PropertyChecklistReview.DoesNotExist:
        reviewed_at = None

    resolved = []
    standard_items = (
        StandardChecklistItem.objects.filter(visit_type=visit_type, is_active=True)
        .prefetch_related('required_attributes')
    )
    for item in standard_items:
        required_ids = {a.id for a in item.required_attributes.all()}
        if required_ids and not required_ids.issubset(property_attribute_ids):
            continue
        override = overrides_by_item.get(item.id)
        if override and override.is_hidden:
            continue
        resolved.append({
            'source': VisitChecklistItem.Source.STANDARD,
            'standard_item': item,
            'section': item.section,
            'order': override.order_override if override and override.order_override is not None else item.order,
            'text': item.text,
            'mandatory': override.mandatory_override if override and override.mandatory_override is not None else item.mandatory,
            'requires_photo': item.requires_photo,
            'requires_note': item.requires_note,
            'is_new_unreviewed': reviewed_at is None or item.created_at > reviewed_at,
        })

    for item in PropertyChecklistItem.objects.filter(property=property, visit_type=visit_type, is_active=True):
        resolved.append({
            'source': VisitChecklistItem.Source.PROPERTY,
            'standard_item': None,
            'section': '',
            'order': item.order,
            'text': item.text,
            'mandatory': item.mandatory,
            'requires_photo': item.requires_photo,
            'requires_note': False,
            'is_new_unreviewed': False,
        })

    resolved.sort(key=lambda r: (r['section'], r['order']))
    return resolved


def mark_checklist_reviewed(property, visit_type):
    PropertyChecklistReview.objects.update_or_create(
        property=property, visit_type=visit_type, defaults={'reviewed_at': timezone.now()},
    )


@transaction.atomic
def create_visit(property, visit_type, **visit_kwargs):
    """Creates a Visit and snapshots the currently-resolved checklist into
    VisitChecklistItem rows — the one place copying is correct, per the
    design doc, since a submitted visit's record must never change under it
    even as the standard list keeps evolving."""
    visit = Visit.objects.create(property=property, visit_type=visit_type, **visit_kwargs)
    resolved = resolve_checklist(property, visit_type)
    VisitChecklistItem.objects.bulk_create([
        VisitChecklistItem(
            visit=visit,
            source=row['source'],
            section=row['section'],
            order=row['order'],
            text=row['text'],
            mandatory=row['mandatory'],
            requires_photo=row['requires_photo'],
            requires_note=row['requires_note'],
            is_new_unreviewed=row['is_new_unreviewed'],
        )
        for row in resolved
    ])
    # Deferred to after commit — this is a network call, and shouldn't hold
    # the transaction (or block the caller) if Google is slow/unreachable.
    transaction.on_commit(lambda: _push_to_calendar(visit))
    return visit


def _push_to_calendar(visit):
    from ..google_calendar_push import push_visit
    push_visit(visit)


def submit_visit(visit):
    """Server-side gate: a visit can't be submitted until every mandatory
    checklist item is either completed or has a skip_reason, and every
    requires_photo item has at least one VisitMedia. Raises ValidationError
    listing what's missing rather than silently letting it through."""
    items = list(visit.checklist_items.all())
    missing = [
        item.text for item in items
        if item.mandatory and not item.is_completed and not item.skip_reason
    ]
    media_item_ids = set(visit.media.exclude(checklist_item__isnull=True).values_list('checklist_item_id', flat=True))
    missing_photos = [
        item.text for item in items
        if item.requires_photo and item.id not in media_item_ids
    ]
    if missing or missing_photos:
        problems = []
        if missing:
            problems.append(f"{len(missing)} item(s) not completed or skipped: {', '.join(missing[:5])}")
        if missing_photos:
            problems.append(f"{len(missing_photos)} item(s) missing a required photo: {', '.join(missing_photos[:5])}")
        raise ValidationError(' — '.join(problems))

    visit.status = Visit.Status.SUBMITTED
    visit.submitted_at = timezone.now()
    visit.save(update_fields=['status', 'submitted_at'])

    _create_issue_tickets(visit)
    return visit


def _create_issue_tickets(visit):
    """Each VisitIssue becomes a real Ticket on submit — the bridge into
    the existing ticket system (see ONSITE_DESIGN.md). Only issues that
    don't already have one are converted, so a resubmit (shouldn't happen,
    but the check is free) can't double-create tickets."""
    from tickets.models import StaffProfile, Ticket

    for issue in visit.issues.filter(created_ticket__isnull=True):
        ticket = Ticket.objects.create(
            title=issue.description[:80],
            description=issue.description,
            property=visit.property,
            assigned_role=StaffProfile.Role.MAINTENANCE,
            source=Ticket.Source.ONSITE,
        )
        for media in issue.media.all():
            ticket.attachments.create(file=media.file)
        issue.created_ticket = ticket
        issue.save(update_fields=['created_ticket'])
