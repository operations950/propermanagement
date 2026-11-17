"""Long-term rentals' leases: the tenant, the term, the rent and its increases, the uploaded lease read for its rent schedule. These properties are not run through our books; the monthly
rent in force feeds the month-end rent check (core/close_ltr_tax.py)."""
from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from accounting.documents import DocumentError
from accounting.models import AssociationDocument
from accounting.web import admin_only, parse_date, parse_money
from core.models import Contact, Property

from . import leasereader
from .models import LeaseProposal, LongTermLease, LongTermRentStep


def _ltr_or_404(pk):
    return get_object_or_404(Property, pk=pk, property_type=Property.Type.LONG_TERM_RENTAL)


def _tenant(name, email='', phone=''):
    name = ' '.join((name or '').split())
    if not name:
        raise ValueError('Give the tenant\'s name.')
    person = Contact.objects.filter(name__iexact=name, contact_type=Contact.ContactType.TENANT).first() or Contact.objects.filter(name__iexact=name).first()
    if person is None:
        person = Contact.objects.create(name=name, contact_type=Contact.ContactType.TENANT, source=Contact.Source.MANUAL)
    changed = []
    if email and not person.email:
        person.email = email.strip()
        changed.append('email')
    if phone and not person.phone:
        person.phone = phone.strip()
        changed.append('phone')
    if changed:
        person.save(update_fields=changed)
    return person


@admin_only
def ltr_leases(request, pk):
    prop = _ltr_or_404(pk)
    if request.method == 'POST':
        try:
            tenant = _tenant(request.POST.get('tenant_name'), request.POST.get('tenant_email', ''), request.POST.get('tenant_phone', ''))
            lease = LongTermLease.objects.create(property=prop, tenant=tenant, start_date=parse_date(request.POST.get('start_date')), end_date=parse_date(request.POST.get('end_date')),
                                                 monthly_rent=parse_money(request.POST.get('monthly_rent') or '0'), rate_expires_on=parse_date(request.POST.get('rate_expires_on')))
            tenant.properties.add(prop)
            messages.success(request, f'{tenant.name} added.')
            return redirect('commercial:ltr_lease', pk=prop.pk, lease_pk=lease.pk)
        except ValueError as e:
            messages.error(request, str(e))
        return redirect('commercial:ltr_leases', pk=prop.pk)
    leases = list(LongTermLease.objects.filter(property=prop).select_related('tenant'))
    return render(request, 'commercial/ltr_leases.html', {'prop': prop, 'leases': leases, 'today': timezone.localdate()})


@admin_only
def ltr_lease(request, pk, lease_pk):
    prop = _ltr_or_404(pk)
    lease = get_object_or_404(LongTermLease.objects.select_related('tenant'), pk=lease_pk, property=prop)
    here = reverse('commercial:ltr_lease', args=[prop.pk, lease.pk])
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'save':
                lease.status = request.POST.get('status') if request.POST.get('status') in LongTermLease.Status.values else lease.status
                lease.start_date, lease.end_date = parse_date(request.POST.get('start_date')), parse_date(request.POST.get('end_date'))
                lease.monthly_rent = parse_money(request.POST.get('monthly_rent') or '0')
                lease.security_deposit = parse_money(request.POST.get('security_deposit') or '0')
                lease.rate_expires_on = parse_date(request.POST.get('rate_expires_on'))
                lease.renewal_notes, lease.notes = (request.POST.get('renewal_notes') or '').strip(), (request.POST.get('notes') or '').strip()
                lease.save()
                messages.success(request, 'Saved.')
            elif action == 'add_step':
                day, amount = parse_date(request.POST.get('effective_date')), parse_money(request.POST.get('monthly_rent'))
                if day is None:
                    raise ValueError('Give the date the new rent starts.')
                LongTermRentStep.objects.update_or_create(lease=lease, effective_date=day, defaults={'monthly_rent': amount, 'note': (request.POST.get('note') or '').strip()[:200]})
                messages.success(request, f'Rent steps to ${amount:,.2f} from {day:%b} {day.day}, {day.year}.')
            elif action == 'delete_step':
                LongTermRentStep.objects.filter(pk=request.POST.get('step'), lease=lease).delete()
            elif action == 'upload_doc':
                f = request.FILES.get('file')
                if f is None:
                    raise ValueError('Choose the file to keep.')
                kind = 'amendment' if request.POST.get('kind') == 'amendment' else 'lease'
                leasereader.attach_lease_document(prop, f, request.user, (request.POST.get('name') or '').strip(), kind=kind)
                messages.success(request, 'The document is on file and is being read. In a few minutes, press "Read the lease" to see its rent schedule.')
            elif action == 'read_lease':
                doc = get_object_or_404(AssociationDocument, pk=request.POST.get('doc'), property=prop)
                leasereader.read_lease(doc, lease, request.user)
                messages.success(request, 'The lease is read. Look over what it found below and apply what is right.')
            elif action == 'apply_proposal':
                proposal = get_object_or_404(LeaseProposal, pk=request.POST.get('proposal'), ltr_lease=lease, status=LeaseProposal.Status.PROPOSED)
                made = leasereader.apply_proposal(proposal, request.user, step_dates=request.POST.getlist('step'), set_term=bool(request.POST.get('set_term')))
                messages.success(request, f'Applied: {made} rent increase{"s" if made != 1 else ""} now on the lease.')
            elif action == 'dismiss_proposal':
                leasereader.dismiss_proposal(get_object_or_404(LeaseProposal, pk=request.POST.get('proposal'), ltr_lease=lease), request.user)
        except (ValueError, ArithmeticError, DocumentError) as e:
            messages.error(request, str(e))
        return redirect(here)
    today = timezone.localdate()
    month, schedule = today.replace(day=1), []
    for _ in range(12):
        schedule.append({'month': month, 'rent': lease.rent_for_month(month) if lease.covers(month) else Decimal('0.00')})
        month = (month.replace(day=28) + timedelta(days=5)).replace(day=1)
    docs = list(AssociationDocument.objects.filter(property=prop, kind__in=['lease', 'amendment']).order_by('-uploaded_at'))
    return render(request, 'commercial/ltr_lease.html', {'prop': prop, 'lease': lease, 'steps': list(lease.steps.all()), 'schedule': schedule, 'docs': docs, 'statuses': LongTermLease.Status.choices,
                                                         'proposal': lease.proposals.filter(status=LeaseProposal.Status.PROPOSED).first(), 'today': today})
