"""Cart-state logic for the par-level supply reorder system — the one
piece of this redesign that has to be exactly right (see the build brief's
"Phase 3 — cart state" section, which this implements verbatim). Kept
separate from views.py since both the cleaner's par-check page (which
reveals history after each tap) and the staff cart page consume it.

For each active PropertySupply, let R be its latest SupplyReading and O be
its latest SENT SupplyOrderLine (i.e. the line's order has sent_at set).
Cancellation of an unsent order draft isn't a concept here — "sent" is the
only thing that matters, since an order that was never sent never went
anywhere and can't have suppressed or triggered anything.

    | R                                    | O                          | State           |
    |---------------------------------------|----------------------------|-----------------|
    | none, or older than STALE_DAYS         | (n/a)                      | unknown         |
    | HIGH                                   | (n/a)                      | stocked         |
    | MID/LOW                                | sent_at > R.read_at        | in_flight       |
    | MID/LOW                                | none, or sent_at < R.read_at | in_cart (+ flagged if O is older than DELIVERY_EXPECTED_DAYS) |

Deliberately no AI, no free text, no priority tier — see the brief's "Do
not" list."""
from django.conf import settings
from django.db.models import Max, OuterRef, Q, Subquery
from django.utils import timezone

from .models import PropertySupply, SupplyOrderLine, SupplyReading

UNKNOWN = 'unknown'
STOCKED = 'stocked'
IN_FLIGHT = 'in_flight'
IN_CART = 'in_cart'
IN_CART_FLAGGED = 'in_cart_flagged'


def _annotate_latest_reading_and_order_line(property_supply_qs):
    """Attaches latest_reading_id/latest_sent_line_id to each PropertySupply
    via a correlated subquery — one extra query total to resolve the ids
    (not one per row), portable across SQLite (local dev) and Postgres
    (production), unlike Postgres-only DISTINCT ON. Callers still need to
    batch-fetch the actual rows those ids point to; see cart_state_for."""
    latest_reading = (
        SupplyReading.objects.filter(property_supply=OuterRef('pk')).order_by('-read_at')
    )
    latest_sent_line = (
        SupplyOrderLine.objects.filter(property_supply=OuterRef('pk'), order__sent_at__isnull=False)
        .order_by('-order__sent_at')
    )
    return property_supply_qs.annotate(
        latest_reading_id=Subquery(latest_reading.values('id')[:1]),
        latest_sent_line_id=Subquery(latest_sent_line.values('id')[:1]),
    )


def _resolve_status(reading, order_line, now):
    """Pure decision-table logic, given an already-resolved latest reading
    and latest sent order line (both may be None) — split out from the
    batch query plumbing so it's independently testable."""
    stale_cutoff = now - timezone.timedelta(days=settings.SUPPLY_READING_STALE_DAYS)

    if reading is None or reading.read_at < stale_cutoff:
        return {'state': UNKNOWN, 'reading': reading, 'order_line': order_line, 'flagged': False, 'delivered': False}

    order = order_line.order if order_line else None

    if reading.level == SupplyReading.Level.HIGH:
        delivered = bool(order and order.sent_at and order.sent_at < reading.read_at)
        return {'state': STOCKED, 'reading': reading, 'order_line': order_line, 'flagged': False, 'delivered': delivered}

    # MID or LOW.
    if order and order.sent_at and order.sent_at > reading.read_at:
        return {'state': IN_FLIGHT, 'reading': reading, 'order_line': order_line, 'flagged': False, 'delivered': False}

    flagged = False
    if order and order.sent_at and order.sent_at < reading.read_at:
        expected_cutoff = now - timezone.timedelta(days=settings.SUPPLY_DELIVERY_EXPECTED_DAYS)
        flagged = order.sent_at < expected_cutoff
    state = IN_CART_FLAGGED if flagged else IN_CART
    return {'state': state, 'reading': reading, 'order_line': order_line, 'flagged': flagged, 'delivered': False}


def cart_state_for(property_supply_qs, now=None):
    """Batched version — the one to use for anything rendering more than a
    single item (the cleaner's whole par-check list, the cart page across
    every property). Returns a list of dicts, one per PropertySupply in
    property_supply_qs, each with: property_supply, state, reading,
    order_line, flagged, delivered. Fixed query count (3 total) regardless
    of how many properties/items are in property_supply_qs — see
    _annotate_latest_reading_and_order_line's docstring."""
    now = now or timezone.now()
    supplies = list(
        _annotate_latest_reading_and_order_line(property_supply_qs)
        .select_related('property', 'unit', 'supply_item')
    )

    reading_ids = {s.latest_reading_id for s in supplies if s.latest_reading_id}
    line_ids = {s.latest_sent_line_id for s in supplies if s.latest_sent_line_id}
    readings_by_id = {r.id: r for r in SupplyReading.objects.filter(id__in=reading_ids)}
    lines_by_id = {
        l.id: l for l in SupplyOrderLine.objects.filter(id__in=line_ids).select_related('order')
    }

    results = []
    for ps in supplies:
        reading = readings_by_id.get(ps.latest_reading_id)
        order_line = lines_by_id.get(ps.latest_sent_line_id)
        status = _resolve_status(reading, order_line, now)
        results.append({'property_supply': ps, **status})
    return results


def cart_state_for_property(property, now=None):
    """Convenience wrapper for the single-property case (the cleaner's par
    check, one property at a time)."""
    return cart_state_for(
        PropertySupply.objects.filter(property=property, is_active=True).order_by('display_order'), now=now,
    )


def supply_check_context(visit):
    """Feeds visit_public.html's Supplies card. For every active
    PropertySupply the cleaner should be checking on THIS visit — at
    visit.property, scoped to visit.unit when the visit has one: that
    unit's own rows PLUS every property-wide (unit=None) row, since a
    multi-unit building can mix both ("each unit tracks its own kitchen
    supplies" alongside "one shared laundry detergent for the building").
    A visit with no unit (single-unit property) sees only the property-
    wide rows — identical to how every property behaved before per-unit
    tracking existed. Whether THIS visit already recorded a reading for
    each row (readings are never updated, so once answered it's locked —
    no re-tap), and only once answered, the history to reveal — the
    reading from before this one, and the order/flag info from
    cart_state_for_property. History is deliberately withheld pre-tap (see
    the build brief): showing the previous reading first would anchor the
    cleaner into just re-tapping it instead of forming an independent
    observation, and the whole cart-state loop depends on readings being
    independent."""
    supplies = list(
        PropertySupply.objects.filter(property=visit.property, is_active=True)
        .filter(Q(unit=visit.unit_id) | Q(unit__isnull=True))
        .select_related('supply_item', 'unit').order_by('unit__label', 'display_order')
    )
    state_by_ps_id = {row['property_supply'].pk: row for row in cart_state_for_property(visit.property)}
    this_visit_readings = {
        r.property_supply_id: r
        for r in SupplyReading.objects.filter(visit=visit, property_supply__in=supplies)
    }

    rows = []
    for ps in supplies:
        answered = this_visit_readings.get(ps.pk)
        row = {'property_supply': ps, 'answered': answered}
        if answered:
            row['previous_reading'] = (
                SupplyReading.objects.filter(property_supply=ps)
                .exclude(pk=answered.pk).order_by('-read_at').first()
            )
            state = state_by_ps_id.get(ps.pk, {})
            row['order_line'] = state.get('order_line')
            row['flagged'] = state.get('flagged', False)
        rows.append(row)
    return rows


def record_reading(visit, property_supply, level):
    """Creates the one reading this visit gets for this item — a no-op
    (returns None) if this visit already answered it, since a reading is
    never updated and the DB's own uniq_reading_per_visit_per_item
    constraint would reject a second insert anyway; checking first avoids
    surfacing that as a 500."""
    if SupplyReading.objects.filter(visit=visit, property_supply=property_supply).exists():
        return None
    return SupplyReading.objects.create(
        property_supply=property_supply, visit=visit, level=level,
        read_by_staff=visit.assigned_staff, read_by_contact=visit.assigned_contact,
    )


# Confirmed empirically against the real site (see the build brief's
# instruction to verify before building around it): this exact
# comma-separated id_qty format adds every listed item to a guest cart,
# no login/API key needed. Critically, ONE invalid id poisons the WHOLE
# request — Walmart shows "Invalid item or quantity" and bounces to the
# homepage instead of adding the valid items and skipping the bad one.
# That's why every caller MUST filter out items with no walmart_item_id
# before this function ever sees them — see send_order in views.py.
WALMART_ADD_TO_CART_URL = 'https://www.walmart.com/sc/cart/addToCart'
# Comfortably under every browser/proxy/server URL-length limit anyone
# still enforces (IE's old 2083 is the tightest realistic floor).
WALMART_CART_URL_MAX_LENGTH = 1800


def clone_kit_onto_property(property, unit=None, default_reorder_quantity=1):
    """The one-click side of "someone has to enter ~20 items ... this
    never gets set up if manual per property" (the build brief's own
    framing of the adoption risk). SupplyItem is already a single shared
    catalog across every property (see its docstring — identity resolved
    once, not per property), so "the reusable item set" doesn't need a
    separate model: this just bulk-creates a PropertySupply for every
    active SupplyItem this property (or, when unit is given, this specific
    unit) doesn't already stock, in catalog order. Existing PropertySupply
    rows are left untouched — safe to run again after adding more items to
    the catalog later, or after a property/unit already has a partial/
    adjusted list. unit=None clones onto the property-wide list exactly as
    before this parameter existed; pass a Unit to seed that unit's own
    list instead — the two are independent (existing_item_ids is scoped
    to the same unit=None/unit as what's being created, so cloning onto
    Unit B doesn't skip an item just because Unit A already has it)."""
    from .models import SupplyItem

    existing = PropertySupply.objects.filter(property=property, unit=unit)
    existing_item_ids = set(existing.values_list('supply_item_id', flat=True))
    next_order = (existing.aggregate(m=Max('display_order'))['m'] or -1) + 1

    to_create = [
        PropertySupply(
            property=property, unit=unit, supply_item=item, reorder_quantity=default_reorder_quantity,
            display_order=next_order + i,
        )
        for i, item in enumerate(SupplyItem.objects.filter(is_active=True).exclude(id__in=existing_item_ids))
    ]
    PropertySupply.objects.bulk_create(to_create)
    return len(to_create)


def push_item_to_adopted_properties(item, default_reorder_quantity=1):
    """The other direction from clone_kit_onto_property: instead of one
    property picking up every item, one new item flows out to every
    property that's already "adopted" the standard kit — defined as
    having at least one active PropertySupply row already, the same
    signal blind_spots uses to distinguish a real property from one
    that's never been set up. A property with zero supplies is left
    alone (that's still what "Clone standard kit" on the blind spots page
    is for) — this only ever adds to a list that already exists, never
    starts one. Idempotent: skips any property that already stocks this
    item, so calling it again (e.g. from a later manual "push to every
    property" retry) is always safe.

    Deliberately still property-wide only (unit=None) even now that
    per-unit PropertySupply rows exist — "adopted" here means "has ANY
    active row," so a multi-unit property with only per-unit rows still
    counts as adopted, and the pushed item lands at the property level
    alongside them rather than being guessed onto one specific unit. A
    unit that specifically needs the new item still gets it added there
    manually, the same one-time way its other unit-specific items were."""
    from core.models import Property

    adopted_property_ids = (
        PropertySupply.objects.filter(is_active=True).exclude(supply_item=item)
        .values_list('property_id', flat=True).distinct()
    )
    already_has_it = set(
        PropertySupply.objects.filter(supply_item=item).values_list('property_id', flat=True)
    )
    target_ids = set(adopted_property_ids) - already_has_it

    to_create = []
    for prop in Property.objects.filter(pk__in=target_ids, is_active=True):
        next_order = (
            PropertySupply.objects.filter(property=prop).aggregate(m=Max('display_order'))['m'] or -1
        ) + 1
        to_create.append(PropertySupply(
            property=prop, supply_item=item, reorder_quantity=default_reorder_quantity, display_order=next_order,
        ))
    PropertySupply.objects.bulk_create(to_create)
    return len(to_create)


def flagged_orders():
    """Feeds the Owner Dashboard's on-site panel (see the build brief:
    "Items in the flagged state ... belong on the on-site panel — that's
    a delivery failure, not a supply request", and explicitly never a
    Ticket — different lifecycle, batch -> order -> confirm-by-reading,
    not assign -> complete)."""
    rows = cart_state_for(PropertySupply.objects.filter(is_active=True))
    return [r for r in rows if r['state'] == IN_CART_FLAGGED]


def blind_spots(now=None):
    """A short cart looks identical whether every property is stocked or
    nobody visited — same failure shape as a dead booking feed (see the
    build brief). Two kinds of blind spot, both real coverage gaps rather
    than "nothing to order": a property with no supply catalog at all
    (nobody's set it up yet), and a property WITH a catalog whose most
    recent reading across every item is stale or missing entirely."""
    from core.models import Property
    from onsite.models import Visit

    now = now or timezone.now()
    stale_cutoff = now - timezone.timedelta(days=settings.SUPPLY_READING_STALE_DAYS)

    str_properties = list(
        Property.objects.filter(property_type=Property.Type.SHORT_TERM_RENTAL, is_active=True),
    )
    catalog_property_ids = set(
        PropertySupply.objects.filter(is_active=True, property__in=str_properties)
        .values_list('property_id', flat=True).distinct(),
    )
    no_catalog = [p for p in str_properties if p.pk not in catalog_property_ids]

    latest_by_property = {
        row['property_supply__property_id']: row['latest']
        for row in (
            SupplyReading.objects.filter(property_supply__property_id__in=catalog_property_ids)
            .values('property_supply__property_id').annotate(latest=Max('read_at'))
        )
    }

    stale = []
    for p in str_properties:
        if p.pk not in catalog_property_ids:
            continue
        latest = latest_by_property.get(p.pk)
        if latest is not None and latest >= stale_cutoff:
            continue
        turnovers_since = Visit.objects.filter(property=p, visit_type__slug='turnover')
        if latest:
            turnovers_since = turnovers_since.filter(scheduled_date__gte=latest.date())
        stale.append({
            'property': p, 'latest_reading_at': latest, 'turnovers_since': turnovers_since.count(),
        })

    return {'no_catalog': no_catalog, 'stale': stale}


def build_walmart_cart_urls(item_id_quantity_pairs):
    """item_id_quantity_pairs: [(walmart_item_id, quantity), ...] — every
    id must already be real/non-blank; this function trusts its input
    rather than re-validating, since "which items are catalog problems" is
    a decision the caller has to surface to a human anyway. Returns a list
    of one or more URLs, chunked so none exceeds WALMART_CART_URL_MAX_LENGTH."""
    pairs = [f'{item_id}_{qty}' for item_id, qty in item_id_quantity_pairs]
    urls = []
    current = []
    for pair in pairs:
        candidate_url = f'{WALMART_ADD_TO_CART_URL}?items=' + ','.join(current + [pair])
        if len(candidate_url) > WALMART_CART_URL_MAX_LENGTH and current:
            urls.append(f'{WALMART_ADD_TO_CART_URL}?items=' + ','.join(current))
            current = [pair]
        else:
            current.append(pair)
    if current:
        urls.append(f'{WALMART_ADD_TO_CART_URL}?items=' + ','.join(current))
    return urls
