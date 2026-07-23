from urllib.parse import quote_plus

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from core.models import Property, properties_by_type

from .models import SupplyOrderBatch, SupplyRequest


@login_required
def digest(request):
    pending = SupplyRequest.objects.filter(status=SupplyRequest.Status.PENDING).select_related('property')
    by_property = {}
    unassigned = []
    for req in pending:
        if req.property_id:
            by_property.setdefault(req.property, []).append(req)
        else:
            unassigned.append(req)

    if request.method == 'POST':
        property_id = request.POST.get('property_id')
        request_ids = request.POST.getlist('request_ids')
        if property_id and request_ids:
            prop = get_object_or_404(Property, pk=property_id)
            batch, _ = SupplyOrderBatch.objects.get_or_create(property=prop, date=timezone.localdate())
            SupplyRequest.objects.filter(pk__in=request_ids, property=prop).update(
                status=SupplyRequest.Status.ORDERED, order_batch=batch,
            )
            messages.success(request, f'Built an order list for {prop.name} with {len(request_ids)} item(s).')
            return redirect('supplies:batch_detail', pk=batch.pk)

    return render(request, 'supplies/digest.html', {
        'by_property': by_property, 'unassigned': unassigned, 'properties_by_type': properties_by_type(),
    })


@login_required
def supply_request_set_property(request, pk):
    req = get_object_or_404(SupplyRequest, pk=pk)
    if request.method == 'POST':
        property_id = request.POST.get('property_id')
        if property_id:
            req.property_id = property_id
            req.save(update_fields=['property'])
            messages.success(request, f'Assigned to {req.property.name}.')
    return redirect('supplies:digest')


@login_required
def batch_detail(request, pk):
    batch = get_object_or_404(SupplyOrderBatch, pk=pk)
    items = batch.requests.all()
    amazon_links = [
        (r, f'https://www.amazon.com/s?k={quote_plus(r.item_guess or r.raw_text[:60])}') for r in items
    ]
    export_text = '\n'.join(f'- {r.item_guess or r.raw_text} ({r.quantity_guess or "qty ?"})' for r in items)
    return render(request, 'supplies/batch_detail.html', {
        'batch': batch, 'amazon_links': amazon_links, 'export_text': export_text,
    })
