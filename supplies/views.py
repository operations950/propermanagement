from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from core.models import Property

from . import services as supply_services
from .models import SupplyItem, SupplyOrder, SupplyOrderLine


def _row_key(row):
    """Stable per-row key for form field names (qty_<key>) — a resolved
    row has no PropertySupply.pk to hang off anymore, so this is built
    from the (unit, supply_item) pair it actually resolved from."""
    return f'{row["unit"].pk if row["unit"] else 0}_{row["supply_item"].pk}'


@login_required
def cart(request):
    """Today's cart — reviewed and flushed daily, not a request queue (see
    the build brief). Consolidates the VIEW across every property, but a
    Walmart cart URL is always generated per property in send_order below
    — a Walmart order carries one delivery address, so combining
    properties into a single cart isn't an option. One call to
    cart_state_for_portfolio, not one per property, keeps the query count
    flat regardless of portfolio size — see that function's own
    docstring."""
    all_rows = supply_services.cart_state_for_portfolio()

    by_property = {}
    for row in all_rows:
        if row['state'] not in (supply_services.IN_CART, supply_services.IN_CART_FLAGGED):
            continue
        by_property.setdefault(row['property'], []).append(row)

    properties = []
    for prop, rows in sorted(by_property.items(), key=lambda kv: kv[0].name):
        rows.sort(key=lambda r: ((r['unit'].label if r['unit'] else ''), r['supply_item'].name))
        for r in rows:
            r['row_key'] = _row_key(r)
        missing_id_rows = [r for r in rows if not r['supply_item'].walmart_item_id]
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
    rows = supply_services.cart_state_for_property(prop)
    cart_rows = [r for r in rows if r['state'] in (supply_services.IN_CART, supply_services.IN_CART_FLAGGED)]

    if not cart_rows:
        messages.info(request, f'{prop.name}: nothing to order right now.')
        return redirect('supplies:cart')

    order = SupplyOrder.objects.create(property=prop, created_by=request.user)
    cart_pairs = []
    missing_id_names = []
    for row in cart_rows:
        item = row['supply_item']
        raw_qty = request.POST.get(f'qty_{_row_key(row)}', '').strip()
        quantity = int(raw_qty) if raw_qty.isdigit() and int(raw_qty) > 0 else row['reorder_quantity']
        SupplyOrderLine.objects.create(
            order=order, unit=row['unit'], supply_item=item, quantity=quantity,
            triggered_by_reading=row['reading'],
        )
        if item.walmart_item_id:
            cart_pairs.append((item.walmart_item_id, quantity))
        else:
            missing_id_names.append(item.name)

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
    lines = order.lines.select_related('unit', 'supply_item')
    cart_urls = order.cart_url.split('\n') if order.cart_url else []
    return render(request, 'supplies/order_detail.html', {'order': order, 'lines': lines, 'cart_urls': cart_urls})


@login_required
def catalog(request):
    """Staff-facing catalog management — add/edit/deactivate SupplyItem
    rows, including which items are on the portfolio-wide standard list,
    without going through Django admin. No hard delete: an item may still
    be referenced by PropertySupplyOverride/SupplyReading/SupplyOrderLine
    history — is_active is the only way an item goes away, same as
    Property/Unit/everything else in this app that's ever been stocked or
    ordered. Checking "standard" here is the ENTIRE mechanism for putting
    an item on every property/unit automatically — see resolve_supplies;
    there's nothing left to "push" or "clone" per property anymore."""
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'add_item':
            name = request.POST.get('name', '').strip()
            if not name:
                messages.error(request, 'Enter a name for the item.')
            else:
                raw_qty = request.POST.get('standard_reorder_quantity', '').strip()
                SupplyItem.objects.create(
                    name=name,
                    unit_label=request.POST.get('unit_label', '').strip(),
                    walmart_item_id=request.POST.get('walmart_item_id', '').strip(),
                    is_standard=request.POST.get('is_standard') == 'on',
                    standard_reorder_quantity=int(raw_qty) if raw_qty.isdigit() and int(raw_qty) > 0 else 1,
                )
                messages.success(request, f'Added "{name}" to the catalog.')
        elif action == 'update_item':
            item = get_object_or_404(SupplyItem, pk=request.POST.get('item_id'))
            name = request.POST.get('name', '').strip()
            if name:
                item.name = name
            item.unit_label = request.POST.get('unit_label', '').strip()
            item.walmart_item_id = request.POST.get('walmart_item_id', '').strip()
            item.is_standard = request.POST.get('is_standard') == 'on'
            raw_qty = request.POST.get('standard_reorder_quantity', '').strip()
            item.standard_reorder_quantity = int(raw_qty) if raw_qty.isdigit() and int(raw_qty) > 0 else 1
            item.is_active = request.POST.get('is_active') == 'on'
            item.save()
            messages.success(request, 'Item updated.')
        return redirect('supplies:catalog')

    return render(request, 'supplies/catalog.html', {'items': SupplyItem.objects.all()})


@login_required
def blind_spots(request):
    """Coverage gaps the cart page can't see — a short cart looks
    identical whether every property/unit is stocked or nobody's visited
    (see supply_services.blind_spots's own docstring)."""
    spots = supply_services.blind_spots()
    return render(request, 'supplies/blind_spots.html', spots)


@login_required
@require_http_methods(['POST'])
def undo_order(request, pk):
    """Clears sent_at rather than deleting the order/lines — the order
    stays as a record of what was attempted, and cart_state_for_property/
    portfolio only ever look at SENT order lines, so an undone order
    simply stops suppressing or explaining anything; the items it covered
    fall straight back into today's cart on the next view."""
    order = get_object_or_404(SupplyOrder, pk=pk)
    order.sent_at = None
    order.save(update_fields=['sent_at'])
    messages.success(request, f'Undone — {order.property.name}\'s order is no longer marked sent.')
    return redirect('supplies:cart')
