"""Checklist resolution and the Visit lifecycle. See ONSITE_DESIGN.md's "The
checklist model" section for the full rationale — a property's checklist is
never stored; it's computed from the standard reservoir plus that
property's overrides/additions, and only materialized (copied) once, at
Visit creation, into VisitChecklistItem rows."""
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from ..models import (
    PropertyChecklistItem,
    PropertyChecklistOverride,
    PropertyChecklistReview,
    StandardChecklistItem,
    Visit,
    VisitChecklistItem,
    VisitType,
)

# The slug seed_checklist_templates uses for the deep-clean addon bundle —
# looked up by slug (not hardcoded id) wherever the addon's items need
# resolving, so renaming the VisitType's display name never breaks this.
DEEP_CLEAN_ADDON_SLUG = 'deep-clean'


class VisitSubmitBlocked(ValidationError):
    """Same ValidationError submit_visit always raised — still catchable as
    one anywhere that only cares about the message — but also carries the
    actual item ids so the caller (onsite/views.py) can highlight exactly
    which checklist rows are blocking submission, instead of just showing
    the message text. missing_item_ids / missing_photo_item_ids can overlap
    (an item can be both unmarked AND missing its photo)."""
    def __init__(self, message, missing_item_ids, missing_photo_item_ids):
        super().__init__(message)
        self.missing_item_ids = missing_item_ids
        self.missing_photo_item_ids = missing_photo_item_ids


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

    # Property-specific additions always sort after every standard item —
    # their own 'order' field starts fresh at 0 independently, and now that
    # sorting is by 'order' alone (see the note above), an unoffset 0 would
    # jump a property-specific item to the very front of a 40-item standard
    # list instead of appending after it.
    property_item_offset = max((r['order'] for r in resolved), default=-1) + 1
    for item in PropertyChecklistItem.objects.filter(property=property, visit_type=visit_type, is_active=True):
        resolved.append({
            'source': VisitChecklistItem.Source.PROPERTY,
            'standard_item': None,
            'section': '',
            'order': property_item_offset + item.order,
            'text': item.text,
            'mandatory': item.mandatory,
            'requires_photo': item.requires_photo,
            'requires_note': False,
            'is_new_unreviewed': False,
        })

    # Sorted by 'order' alone, NOT (section, order) — see
    # StandardChecklistItem.Meta's comment for why: the seed data is
    # written as one deliberate room-by-room sequence (Kitchen, then
    # Bathrooms, then Bedrooms, ...), and each section's items are already
    # contiguous within it, so sorting by section name would alphabetize
    # the sections and scramble that intended flow.
    resolved.sort(key=lambda r: r['order'])
    return resolved


def mark_checklist_reviewed(property, visit_type):
    PropertyChecklistReview.objects.update_or_create(
        property=property, visit_type=visit_type, defaults={'reviewed_at': timezone.now()},
    )


def _addon_visit_type(slug=DEEP_CLEAN_ADDON_SLUG):
    return VisitType.objects.filter(slug=slug, is_addon=True).first()


def _deep_clean_checklist_items(visit, property, order_offset):
    """Resolves the deep-clean addon bundle's items for this property and
    returns unsaved VisitChecklistItem instances for them, all grouped
    under one flat 'Deep Clean Extras' section regardless of whatever
    section the addon's own StandardChecklistItems are tagged with — this
    is one extra bucket layered onto a normal turnover, not a second
    room-by-room breakdown, so it doesn't need its own sub-sections.

    order_offset MUST push every one of these past every item already on
    (or about to be added to) the visit — resolve_checklist(addon_type)
    numbers its own rows from 0, the exact same range the turnover items
    already occupy, so without the offset the two groups' order values
    overlap and VisitChecklistItem's plain 'order' sort (see its Meta's
    comment) interleaves them into alternating sections instead of one
    clean block at the end — caught by an actual screenshot during this
    feature's build, not by review."""
    addon_type = _addon_visit_type()
    if addon_type is None:
        return []
    resolved = resolve_checklist(property, addon_type)
    return [
        VisitChecklistItem(
            visit=visit,
            source=VisitChecklistItem.Source.DEEP_CLEAN,
            section='Deep Clean Extras',
            order=order_offset + row['order'],
            text=row['text'],
            mandatory=row['mandatory'],
            requires_photo=row['requires_photo'],
            requires_note=row['requires_note'],
            is_new_unreviewed=row['is_new_unreviewed'],
        )
        for row in resolved
    ]


@transaction.atomic
def create_visit(property, visit_type, is_deep_clean=False, **visit_kwargs):
    """Creates a Visit and snapshots the currently-resolved checklist into
    VisitChecklistItem rows — the one place copying is correct, per the
    design doc, since a submitted visit's record must never change under it
    even as the standard list keeps evolving. is_deep_clean additionally
    layers the deep-clean addon bundle's items on top (see
    _deep_clean_checklist_items) — most callers won't know this at creation
    time (a booking import has no way to know a given turnover should also
    be a deep clean); see set_deep_clean for turning it on afterward."""
    visit = Visit.objects.create(property=property, visit_type=visit_type, is_deep_clean=is_deep_clean, **visit_kwargs)
    resolved = resolve_checklist(property, visit_type)
    items = [
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
    ]
    if is_deep_clean:
        offset = max((i.order for i in items), default=-1) + 1
        items += _deep_clean_checklist_items(visit, property, offset)
    VisitChecklistItem.objects.bulk_create(items)
    # Deferred to after commit — this is a network call, and shouldn't hold
    # the transaction (or block the caller) if Google is slow/unreachable.
    transaction.on_commit(lambda: _push_to_calendar(visit))
    return visit


def set_deep_clean(visit, enabled):
    """Turns the deep-clean addon on/off for an existing visit — the
    realistic path, since a booking import can't know at creation time that
    a given turnover should also be a deep clean; staff decide that
    afterward (see visit_detail). Only allowed before the visit starts,
    mirroring this module's existing 'checklist is frozen except one-off
    additions before it starts' rule (see VisitChecklistItem's docstring) —
    changing it mid-clean would rewrite the list out from under whoever's
    already working it."""
    if visit.started_at is not None:
        raise ValidationError("Can't change deep-clean status after the visit has started.")
    if enabled == visit.is_deep_clean:
        return
    if enabled:
        existing_max = visit.checklist_items.aggregate(m=Max('order'))['m']
        offset = (existing_max if existing_max is not None else -1) + 1
        items = _deep_clean_checklist_items(visit, visit.property, offset)
        if not items:
            raise ValidationError('No deep-clean checklist bundle is configured yet — add items to it first.')
        VisitChecklistItem.objects.bulk_create(items)
    else:
        visit.checklist_items.filter(source=VisitChecklistItem.Source.DEEP_CLEAN).delete()
    visit.is_deep_clean = enabled
    visit.save(update_fields=['is_deep_clean'])


def _push_to_calendar(visit):
    from ..google_calendar_push import push_visit
    push_visit(visit)


def submit_visit(visit):
    """Server-side gate: a visit can't be submitted until every mandatory
    checklist item is either completed or has a skip_reason, and every
    requires_photo item has at least one VisitMedia. Raises
    VisitSubmitBlocked (a ValidationError) carrying both a human message and
    the actual blocking item ids, so the caller can highlight exactly which
    checklist rows to look at rather than just showing text."""
    items = list(visit.checklist_items.all())
    missing_items = [
        item for item in items
        if item.mandatory and not item.is_completed and not item.skip_reason
    ]
    media_item_ids = set(visit.media.exclude(checklist_item__isnull=True).values_list('checklist_item_id', flat=True))
    missing_photo_items = [
        item for item in items
        # A skip_reason already exempts an item from the mandatory-completion
        # check above; requires_photo needs the same exemption for the same
        # reason — a cleaner who skipped an item (with a reason) shouldn't
        # then be blocked from submitting because they never took a photo
        # of something they didn't do.
        if item.requires_photo and not item.skip_reason and item.id not in media_item_ids
    ]
    if missing_items or missing_photo_items:
        problems = []
        if missing_items:
            problems.append(
                f"{len(missing_items)} item(s) not completed or skipped: "
                f"{', '.join(i.text for i in missing_items[:5])}"
            )
        if missing_photo_items:
            problems.append(
                f"{len(missing_photo_items)} item(s) missing a required photo: "
                f"{', '.join(i.text for i in missing_photo_items[:5])}"
            )
        raise VisitSubmitBlocked(
            ' — '.join(problems),
            missing_item_ids=[i.id for i in missing_items],
            missing_photo_item_ids=[i.id for i in missing_photo_items],
        )

    visit.status = Visit.Status.SUBMITTED
    visit.submitted_at = timezone.now()
    visit.save(update_fields=['status', 'submitted_at'])

    create_issue_tickets(visit)
    return visit


def create_issue_tickets(visit, created_by=None):
    """Each VisitIssue becomes a real Ticket — the bridge into the
    existing ticket system (see ONSITE_DESIGN.md). Only issues that don't
    already have one are converted, so calling this more than once (a
    resubmit, or a staff-side status override landing on Submitted/
    Verified after the cleaner's own submit already ran it — see
    onsite/views.py::visit_detail's set_status action) can't double-create
    tickets. Public (not the old leading-underscore _create_issue_tickets)
    because visit_detail's manual status override needs to call this too:
    that path sets visit.status directly rather than going through
    submit_visit() above, and issue->ticket conversion was silently
    getting skipped whenever staff pushed a visit to Submitted/Verified by
    hand instead of the cleaner tapping Submit on their own link.

    created_by stays None from the cleaner's own token-link submit
    (anonymous, no Django auth session at all) and is only ever passed
    from visit_detail's manual override, where a real logged-in staff
    user made the call."""
    from tickets.models import StaffProfile, Ticket

    for issue in visit.issues.filter(created_ticket__isnull=True):
        ticket = Ticket.objects.create(
            title=issue.description[:80],
            description=issue.description,
            property=visit.property,
            assigned_role=StaffProfile.Role.MAINTENANCE,
            source=Ticket.Source.ONSITE,
            created_by=created_by,
        )
        for media in issue.media.all():
            # visible_to_vendor=True: a photo of the reported issue itself
            # (e.g. what's actually broken) is exactly what a vendor needs
            # to see on the completion link to do the job — unlike the
            # Documents card, this isn't an internal-only attachment.
            ticket.attachments.create(file=media.file, visible_to_vendor=True)
        issue.created_ticket = ticket
        issue.save(update_fields=['created_ticket'])
