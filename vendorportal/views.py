from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from tickets.models import Ticket

from .forms import VendorCompleteForm, VendorPhotoUploadForm
from .models import AccessAttempt


def _client_ip(request):
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '0.0.0.0')


CLOSED_STATUSES = {Ticket.Status.COMPLETED, Ticket.Status.VERIFIED, Ticket.Status.CANCELLED}


@require_http_methods(['GET', 'POST'])
def vendor_ticket_view(request, token):
    if AccessAttempt.is_rate_limited(_client_ip(request)):
        return HttpResponse('Too many requests. Please try again later.', status=429)

    ticket = get_object_or_404(Ticket, completion_token=token)
    if not ticket.is_completion_token_valid():
        return render(request, 'vendorportal/expired.html', status=410)

    complete_form = VendorCompleteForm()
    upload_form = VendorPhotoUploadForm()
    is_closed = ticket.status in CLOSED_STATUSES

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'upload_photo':
            upload_form = VendorPhotoUploadForm(request.POST, request.FILES)
            if upload_form.is_valid():
                attachment = upload_form.save(commit=False)
                attachment.ticket = ticket
                attachment.uploaded_by_contact = ticket.assigned_contact
                attachment.save()
                return redirect('vendorportal:ticket', token=token)

        elif action == 'mark_complete' and not is_closed:
            complete_form = VendorCompleteForm(request.POST)
            if complete_form.is_valid():
                ticket.status = Ticket.Status.COMPLETED
                ticket.completed_at = timezone.now()
                notes = complete_form.cleaned_data['resolution_notes']
                if notes:
                    ticket.resolution_notes = notes
                ticket.save()
                return redirect('vendorportal:ticket', token=token)

    return render(request, 'vendorportal/ticket_detail.html', {
        'ticket': ticket,
        'is_closed': is_closed,
        'complete_form': complete_form,
        'upload_form': upload_form,
        'attachments': ticket.attachments.all().order_by('-created_at'),
    })
