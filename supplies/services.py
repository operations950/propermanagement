"""Standard-list resolution + cart-state logic for the par-level supply
reorder system.

Resolution (resolve_supplies / _resolve_supplies_bulk) mirrors
onsite.services.checklist.resolve_checklist's own shape: a portfolio-wide
standard list (SupplyItem.is_standard) that every property/unit follows
automatically, minus anything a PropertySupplyOverride hides, with
quantity overridden where set, plus any non-standard "extra" items added
specifically at one property/unit. Nothing is materialized/stored per
property — this is computed fresh every call. See core/models.py's
PropertyChecklistOverride and this app's own PropertySupplyOverride for
the two systems solving the identical shape of problem.

Cart-state (the rest of this module — see the build brief's "Phase 3 —
cart state" section, which this implements verbatim) is unchanged in
spirit from before: for each resolved (property, unit, supply_item), let
R be its latest SupplyReading and O be its latest SENT SupplyOrderLine.
Cancellation of an unsent order draft isn't a concept here — "sent" is
the only thing that matters, since an order that was never sent never
went anywhere and can't have suppressed or triggered anything.

    | R                                    | O                          | State           |
    |---------------------------------------|----------------------------|-----------------|
    | none, or older than STALE_DAYS         | (n/a)                      | unknown         |
    | HIGH                                   | (n/a)                      | stocked         |
    | MID/LOW                                | sent_at > R.read_at        | in_flight       |
    | MID/LOW                                | none, or sent_at < R.read_at | in_cart (+ flagged if O is older than DELIVERY_EXPECTED_DAYS) |

Deliberately no AI, no free text, no priority tier — see the brief's "Do
not" list."""
from django.conf import settings
from django.utils import timezone
from django.db.models import Max

from .models import PropertySupplyOverride, SupplyItem, SupplyOrderLine, SupplyReading

UNKNOWN = 'unknown'
STOCKED = 'stocked'
IN_FLIGHT = 'in_flight'
IN_CART = 'in_cart'
IN_CART_FLAGGED = 'in_cart_flagged'


def _fetch_standard_items():
    return list(SupplyItem.objects.filter(is_standard=True, is_active=True).order_by('standard_display_order', 'name'))


def _fetch_overrides_by_property_unit(property_ids):
    """{(property_id, unit_id): {supply_item_id: PropertySupplyOverride}}
    — unit_id is None for a property-wide override. One query regardless
    of how many properties are being resolved."""
    by_key = {}
    for o in (
        PropertySupplyOverride.objects.filter(property_id__in=property_ids, is_active=True)
        .select_related('supply_item')
    ):
        by_key.setdefault((o.property_id, o.unit_id), {})[o.supply_item_id] = o
    return by_key


def _resolve_for_property_unit(property, unit, standard_items, overrides_by_property_unit):
    """The one place standard-list-plus-overrides logic lives — both
    resolve_supplies (single property/unit) and _resolve_supplies_bulk
    (portfolio-wide) call this against pre-fetched data, never re-querying
    per property/unit. A unit-specific override wins over a property-wide
    one for the same item when both exist."""
    unit_id = unit.pk if unit else None
    unit_overrides = overrides_by_property_unit.get((property.pk, unit_id), {})
    property_wide_overrides = overrides_by_property_unit.get((property.pk, None), {}) if unit_id else {}

    resolved = []
    seen_item_ids = set()
    for item in standard_items:
        override = unit_overrides.get(item.pk) or property_wide_overrides.get(item.pk)
        if override and override.is_hidden:
            continue
        qty = override.reorder_quantity if (override and override.reorder_quantity) else item.standard_reorder_quantity
        resolved.append({'property': property, 'unit': unit, 'supply_item': item, 'reorder_quantity': qty, 'override': override})
        seen_item_ids.add(item.pk)

    # Extras — an override for an item that ISN'T on the standard list at
    # all (or wasn't matched above for any other reason), specific to
    # this property/unit. Unit-specific wins over property-wide for the
    # same item, same as the standard-item loop above.
    combined_overrides = {**property_wide_overrides, **unit_overrides}
    extras = [
        (item_id, o) for item_id, o in combined_overrides.items()
        if item_id not in seen_item_ids and not o.is_hidden
    ]
    for item_id, override in sorted(extras, key=lambda pair: (pair[1].display_order, pair[1].supply_item.name)):
        resolved.append({
            'property': property, 'unit': unit, 'supply_item': override.supply_item,
            'reorder_quantity': override.reorder_quantity or 1, 'override': override,
        })
    return resolved


def resolve_supplies(property, unit=None):
    """Live-resolved supply list for ONE property/unit. Returns a list of
    dicts: property, unit, supply_item, reorder_quantity, override (the
    PropertySupplyOverride responsible for a non-default quantity/extra,
    or None for a plain standard item at its default quantity)."""
    standard_items = _fetch_standard_items()
    overrides_by_property_unit = _fetch_overrides_by_property_unit([property.pk])
    return _resolve_for_property_unit(property, unit, standard_items, overrides_by_property_unit)


def _resolve_supplies_bulk(properties):
    """Same resolution as resolve_supplies, across every active unit of
    every given property (or just the property itself, for a single-unit
    property), in a FIXED number of queries (2) regardless of portfolio
    size. `properties` should already have .units prefetched by the
    caller (Property.objects...prefetch_related('units')) — without that,
    this still works, just with one extra query per property."""
    properties = list(properties)
    standard_items = _fetch_standard_items()
    overrides_by_property_unit = _fetch_overrides_by_property_unit([p.pk for p in properties])

    rows = []
    for property in properties:
        units = [u for u in property.units.all() if u.is_active]
        for unit in (units or [None]):
            rows.extend(_resolve_for_property_unit(property, unit, standard_items, overrides_by_property_unit))
    return rows


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


def _attach_cart_state(rows, now=None):
    """Attaches state/reading/order_line/flagged/delivered to each
    resolved row (from resolve_supplies/_resolve_supplies_bulk) — the
    latest SupplyReading and latest SENT SupplyOrderLine matching that
    row's exact (property, unit, supply_item), batched into two queries
    total regardless of how many rows are being resolved."""
    now = now or timezone.now()
    if not rows:
        return []

    property_ids = {r['property'].pk for r in rows}
    item_ids = {r['supply_item'].pk for r in rows}

    # Ordered so the FIRST row seen per (property, unit, item) group in
    # the loop below is that group's most recent — portable across
    # SQLite/Postgres, unlike Postgres-only DISTINCT ON.
    readings_by_key = {}
    for reading in (
        SupplyReading.objects.filter(property_id__in=property_ids, supply_item_id__in=item_ids)
        .order_by('property_id', 'unit_id', 'supply_item_id', '-read_at')
    ):
        key = (reading.property_id, reading.unit_id, reading.supply_item_id)
        readings_by_key.setdefault(key, reading)

    lines_by_key = {}
    for line in (
        SupplyOrderLine.objects.filter(
            order__property_id__in=property_ids, supply_item_id__in=item_ids, order__sent_at__isnull=False,
        )
        .select_related('order').order_by('order__property_id', 'unit_id', 'supply_item_id', '-order__sent_at')
    ):
        key = (line.order.property_id, line.unit_id, line.supply_item_id)
        lines_by_key.setdefault(key, line)

    results = []
    for row in rows:
        key = (row['property'].pk, row['unit'].pk if row['unit'] else None, row['supply_item'].pk)
        reading = readings_by_key.get(key)
        order_line = lines_by_key.get(key)
        status = _resolve_status(reading, order_line, now)
        results.append({**row, **status})
    return results


def cart_state_for_property(property, now=None):
    """Every unit's (or, single-unit, the property's own) resolved supply
    list for ONE property, with cart state attached — the cleaner's par
    check / the property's own cart, one property at a time."""
    units = list(property.units.filter(is_active=True))
    standard_items = _fetch_standard_items()
    overrides_by_property_unit = _fetch_overrides_by_property_unit([property.pk])
    rows = []
    for unit in (units or [None]):
        rows.extend(_resolve_for_property_unit(property, unit, standard_items, overrides_by_property_unit))
    return _attach_cart_state(rows, now)


def cart_state_for_portfolio(now=None):
    """Every active property's full resolved supply list (every unit),
    with cart state attached — the staff cart page across the whole
    portfolio, and flagged_orders below."""
    from core.models import Property

    properties = list(Property.objects.filter(is_active=True).prefetch_related('units'))
    rows = _resolve_supplies_bulk(properties)
    return _attach_cart_state(rows, now)


def supply_check_context(visit):
    """Feeds visit_public.html's Supplies card — this visit's own
    resolved supply list (visit.property/visit.unit), whether THIS visit
    already recorded a reading for each item (readings are never
    updated, so once answered it's locked — no re-tap), and only once
    answered, the history to reveal: the reading from before this one,
    and the order/flag info from cart_state_for_property. History is
    deliberately withheld pre-tap (see the build brief): showing the
    previous reading first would anchor the cleaner into just re-tapping
    it instead of forming an independent observation, and the whole
    cart-state loop depends on readings being independent."""
    resolved = resolve_supplies(visit.property, visit.unit)
    item_ids = [r['supply_item'].pk for r in resolved]

    this_visit_readings = {
        r.supply_item_id: r
        for r in SupplyReading.objects.filter(visit=visit, supply_item_id__in=item_ids)
    }
    this_unit_id = visit.unit_id
    state_by_item_id = {
        row['supply_item'].pk: row for row in cart_state_for_property(visit.property)
        if (row['unit'].pk if row['unit'] else None) == this_unit_id
    }

    rows = []
    for r in resolved:
        item = r['supply_item']
        answered = this_visit_readings.get(item.pk)
        row = {'supply_item': item, 'reorder_quantity': r['reorder_quantity'], 'answered': answered}
        if answered:
            row['previous_reading'] = (
                SupplyReading.objects.filter(property=visit.property, unit=visit.unit, supply_item=item)
                .exclude(pk=answered.pk).order_by('-read_at').first()
            )
            state = state_by_item_id.get(item.pk, {})
            row['order_line'] = state.get('order_line')
            row['flagged'] = state.get('flagged', False)
        rows.append(row)
    return rows


def record_reading(visit, supply_item, level):
    """Creates the one reading this visit gets for this item — a no-op
    (returns None) if this visit already answered it, since a reading is
    never updated and the DB's own uniq_reading_per_visit_per_item
    constraint would reject a second insert anyway; checking first avoids
    surfacing that as a 500."""
    if SupplyReading.objects.filter(visit=visit, property=visit.property, unit=visit.unit, supply_item=supply_item).exists():
        return None
    return SupplyReading.objects.create(
        property=visit.property, unit=visit.unit, supply_item=supply_item, visit=visit, level=level,
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


def flagged_orders():
    """Feeds the Owner Dashboard's on-site panel (see the build brief:
    "Items in the flagged state ... belong on the on-site panel — that's
    a delivery failure, not a supply request", and explicitly never a
    Ticket — different lifecycle, batch -> order -> confirm-by-reading,
    not assign -> complete)."""
    rows = cart_state_for_portfolio()
    return [r for r in rows if r['state'] == IN_CART_FLAGGED]


def blind_spots(now=None):
    """A short cart looks identical whether every property/unit is fully
    stocked or nobody's actually checked it in a while — same failure
    shape as a dead booking feed. Flags any STR property/unit whose most
    recent reading (across every item it currently resolves to) is stale
    or missing entirely.

    There's no more separate "hasn't been set up yet" bucket — every
    active property now always resolves at least the standard list (see
    resolve_supplies), so "never been set up" isn't a real state anymore.
    A property/unit with literally zero readings ever just falls straight
    into this same stale bucket via latest=None, same as one that WAS set
    up but hasn't been checked recently."""
    from core.models import Property
    from onsite.models import Visit

    now = now or timezone.now()
    stale_cutoff = now - timezone.timedelta(days=settings.SUPPLY_READING_STALE_DAYS)

    str_properties = list(
        Property.objects.filter(property_type=Property.Type.SHORT_TERM_RENTAL, is_active=True)
        .prefetch_related('units'),
    )
    property_ids = [p.pk for p in str_properties]

    latest_by_property_unit = {
        (row['property_id'], row['unit_id']): row['latest']
        for row in (
            SupplyReading.objects.filter(property_id__in=property_ids)
            .values('property_id', 'unit_id').annotate(latest=Max('read_at'))
        )
    }

    stale = []
    for p in str_properties:
        units = [u for u in p.units.all() if u.is_active]
        for unit in (units or [None]):
            latest = latest_by_property_unit.get((p.pk, unit.pk if unit else None))
            if latest is not None and latest >= stale_cutoff:
                continue
            turnovers_since = Visit.objects.filter(property=p, unit=unit, visit_type__slug='turnover')
            if latest:
                turnovers_since = turnovers_since.filter(scheduled_date__gte=latest.date())
            stale.append({
                'property': p, 'unit': unit, 'latest_reading_at': latest, 'turnovers_since': turnovers_since.count(),
            })

    return {'stale': stale}


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
