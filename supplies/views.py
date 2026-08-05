from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from core.models import Property

from . import services as supply_services
from .models import PropertySupply, SupplyOrder, SupplyOrderLine


@login_required
def cart(request):
    """Today's cart — reviewed and flushed daily, not a request queue (see
    the build brief). Consolidates the VIEW across every property with an
    active supply catalog, but a Walmart cart URL is always generated per
    property in send_order below — a Walmart order carries one delivery
    address, so combining properties into a single cart isn't an option.
    One call to cart_state_for across every active PropertySupply, not one
    per property, keeps the query count flat regardless of portfolio size
    — see that function's own docstring."""
    all_rows = supply_services.cart_state_for(PropertySupply.objects.filter(is_active=True))

    by_property = {}
    for row in all_rows:
        if row['state'] not in (supply_services.IN_CART, supply_services.IN_CART_FLAGGED):
            continue
        prop = row['property_supply'].property
        by_property.setdefault(prop, []).append(row)

    properties = []
    for prop, rows in sorted(by_property.items(), key=lambda kv: kv[0].name):
        rows.sort(key=lambda r: r['property_supply'].display_order)
        missing_id_rows = [r for r in rows if not r['property_supply'].supply_item.walmart_item_id]
        properties.append({
            'property': prop,
            'rows': rows,
            'missing_id_rows': missing_id_rows,
            'orderable_count': len(rows) - len(missing_id_rows),
        })

    return render(request, 'supplies/cart.html', {'properties': properties})


@login_required
@require_http_methods(['POST'])
def send_order(request, property_id):
    """Builds and 'sends' one property's cart — 'sends' means generating
    the Walmart link(s) and marking sent_at, not actually placing an order
    ourselves (there's no API/affiliate account for that — see the build
    brief). Marked optimistically the moment staff click through: clicking
    doesn't prove a purchase happened, but the reading loop self-corrects
    regardless — if it never actually completed, the next reading is still
    below par and the item comes back, flagged. Items missing a Walmart id
    are excluded from the generated URL (one bad/blank id would take down
    the WHOLE cart link, confirmed empirically) but still get an order
    line and a visible warning — a catalog problem to go fix, not a
    silent drop."""
    prop = get_object_or_404(Property, pk=property_id)
    rows = supply_services.cart_state_for(PropertySupply.objects.filter(property=prop, is_active=True))
    cart_rows = [r for r in rows if r['state'] in (supply_services.IN_CART, supply_services.IN_CART_FLAGGED)]

    if not cart_rows:
        messages.info(request, f'{prop.name}: nothing to order right now.')
        return redirect('supplies:cart')

    order = SupplyOrder.objects.create(property=prop, created_by=request.user)
    cart_pairs = []
    missing_id_names = []
    for row in cart_rows:
        ps = row['property_supply']
        raw_qty = request.POST.get(f'qty_{ps.pk}', '').strip()
        quantity = int(raw_qty) if raw_qty.isdigit() and int(raw_qty) > 0 else ps.reorder_quantity
        SupplyOrderLine.objects.create(
            order=order, property_supply=ps, quantity=quantity, triggered_by_reading=row['reading'],
        )
        if ps.supply_item.walmart_item_id:
            cart_pairs.append((ps.supply_item.walmart_item_id, quantity))
        else:
            missing_id_names.append(ps.supply_item.name)

    if not cart_pairs:
        order.delete()
        messages.error(
            request,
            f'{prop.name}: every item in the cart is missing a Walmart item id — nothing to send. '
            'Fix the catalog first.',
        )
        return redirect('supplies:cart')

    order.cart_url = '\n'.join(supply_services.build_walmart_cart_urls(cart_pairs))
    order.sent_at = timezone.now()
    order.save(update_fields=['cart_url', 'sent_at'])

    if missing_id_names:
        messages.warning(
            request,
            f'{prop.name}: sent, but skipped {len(missing_id_names)} item(s) with no Walmart id: '
            f'{", ".join(missing_id_names)}.',
        )
    else:
        messages.success(request, f'{prop.name}: order sent.')
    return redirect('supplies:order_detail', pk=order.pk)


@login_required
def order_detail(request, pk):
    order = get_object_or_404(
        SupplyOrder.objects.select_related('property', 'created_by'), pk=pk,
    )
    lines = order.lines.select_related('property_supply__supply_item')
    cart_urls = order.cart_url.split('\n') if order.cart_url else []
    return render(request, 'supplies/order_detail.html', {'order': order, 'lines': lines, 'cart_urls': cart_urls})


@login_required
def blind_spots(request):
    """Coverage gaps the cart page can't see — a short cart looks
    identical whether every property is stocked or nobody's visited (see
    supply_services.blind_spots's own docstring)."""
    spots = supply_services.blind_spots()
    return render(request, 'supplies/blind_spots.html', spots)


@login_required
@require_http_methods(['POST'])
def clone_kit(request, property_id):
    """One-click "stock this property from the standard kit" action off
    the blind spots page — see clone_kit_onto_property's docstring for why
    this is the adoption-risk mitigation, not a new catalog concept."""
    prop = get_object_or_404(Property, pk=property_id)
    created = supply_services.clone_kit_onto_property(prop)
    if created:
        messages.success(
            request,
            f'{prop.name}: added {created} item(s) from the standard kit. '
            'Set reorder quantities and Walmart ids from Admin before relying on it.',
        )
    else:
        messages.info(request, f'{prop.name}: already has every catalog item — nothing to add.')
    return redirect('supplies:blind_spots')


@login_required
@require_http_methods(['POST'])
def undo_order(request, pk):
    """Clears sent_at rather than deleting the order/lines — the order
    stays as a record of what was attempted, and cart_state_for only ever
    looks at SENT order lines, so an undone order simply stops suppressing
    or explaining anything; the items it covered fall straight back into
    today's cart on the next view."""
    order = get_object_or_404(SupplyOrder, pk=pk)
    order.sent_at = None
    order.save(update_fields=['sent_at'])
    messages.success(request, f'Undone — {order.property.name}\'s order is no longer marked sent.')
    return redirect('supplies:cart')
