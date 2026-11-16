"""The commercial screens: the leases of a property, one tenant's page, and the tenant invoices. The books themselves (bank, bills, close, reports) are the accounting screens."""
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from accounting import charges, ledger
from accounting.web import acc_up, admin_only, parse_date, parse_month, parse_money
from core.models import Property

from . import invoices, rent
from .forms import LeaseForm
from .models import Lease, LeaseDocument, RentStep, SeasonalRent, TenantInvoice


def commercial_or_404(pk):
    prop = get_object_or_404(Property, pk=pk, property_type=Property.Type.COMMERCIAL)
    ledger.profile_for(prop)
    return prop


def _lease_or_404(prop, lease_pk):
    return get_object_or_404(Lease.objects.select_related('unit', 'tenant', 'property').prefetch_related('steps', 'seasons', 'documents'), pk=lease_pk, property=prop)


def _money_or(text, default=None):
    try:
        return parse_money(text)
    except ValueError:
        return default


# ---------------------------------------------------------------- the leases of a property

@admin_only
def leases(request, pk):
    prop = commercial_or_404(pk)
    today = timezone.localdate()
    rows = list(Lease.objects.filter(property=prop).select_related('unit', 'tenant').prefetch_related('steps', 'seasons'))
    accounts = {a['unit'].pk: a['balance'] for a in charges.accounts(prop) if a.get('current')}
    soon = today + timedelta(days=90)
    current, upcoming, ended = [], [], []
    for l in rows:
        row = {'lease': l, 'rent': rent.rent_for_month(l, today)['monthly'] if rent.is_billable(l, today) else l.base_rent, 'balance': accounts.get(l.unit_id, Decimal('0.00')),
               'ends_soon': bool(l.end_date and today <= l.end_date <= soon and l.status != Lease.Status.ENDED), 'expired': bool(l.end_date and l.end_date < today and l.status != Lease.Status.ENDED)}
        if l.status == Lease.Status.ENDED or (l.end_date and l.end_date < today and l.status != Lease.Status.PENDING and not rent.is_billable(l, today)):
            ended.append(row)
        elif l.start_date and l.start_date > today:
            upcoming.append(row)
        else:
            current.append(row)
    leased = {l.unit_id for l in rows if l.status != Lease.Status.ENDED and (not l.end_date or l.end_date >= today)}
    vacant = [u for u in prop.units.filter(is_active=True).order_by('label') if u.pk not in leased]
    return render(request, 'commercial/leases.html', {'assoc': prop, 'current': current, 'upcoming': upcoming, 'ended': ended, 'vacant': vacant, 'ending_soon': [r for r in current if r['ends_soon']],
                                                      'books_on': ledger.profile_for(prop).we_do_financials, 'today': today, **acc_up(request, prop, ['accounting:hub', 'accounting:close', 'accounting:units', 'ws_financials'], reverse('property_detail', args=[prop.pk]), prop.name)})


@admin_only
def lease_form(request, pk, lease_pk=None):
    prop = commercial_or_404(pk)
    lease = _lease_or_404(prop, lease_pk) if lease_pk else None
    initial = {}
    if lease is None:
        initial = {'start_date': timezone.localdate().replace(day=1), 'unit': request.GET.get('unit') or None}
    form = LeaseForm(request.POST or None, instance=lease, prop=prop, initial=initial)
    if request.method == 'POST' and form.is_valid():
        saved = form.save()
        rent.ensure_ownership(saved)
        messages.success(request, f'{saved.tenant.name} saved in unit {saved.unit.label}.')
        return redirect('commercial:lease', pk=prop.pk, lease_pk=saved.pk)
    tenants = Property.objects.none()
    from core.models import Contact
    names = list(Contact.objects.filter(properties=prop).values_list('name', flat=True).distinct()[:200])
    return render(request, 'commercial/lease_form.html', {'assoc': prop, 'form': form, 'lease': lease, 'tenant_names': names,
                                                          **acc_up(request, prop, ([('commercial:lease', lease.tenant.name)] if lease is not None else []) + ['commercial:leases'], reverse('commercial:leases', args=[prop.pk]) if lease is None else reverse('commercial:lease', args=[prop.pk, lease.pk]), 'Leases' if lease is None else f'{lease.tenant.name}')})


@admin_only
def lease_detail(request, pk, lease_pk):
    prop = commercial_or_404(pk)
    lease = _lease_or_404(prop, lease_pk)
    here = reverse('commercial:lease', args=[prop.pk, lease.pk])
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'add_step':
                day, amount = parse_date(request.POST.get('effective_date')), parse_money(request.POST.get('monthly_rent'))
                if day is None:
                    raise ValueError('Give the date the new rent starts.')
                RentStep.objects.update_or_create(lease=lease, effective_date=day, defaults={'monthly_rent': amount, 'note': (request.POST.get('note') or '').strip()[:200]})
                messages.success(request, f'Rent steps to ${amount:,.2f} from {day:%b} {day.day}, {day.year}.')
            elif action == 'delete_step':
                RentStep.objects.filter(pk=request.POST.get('step'), lease=lease).delete()
                messages.success(request, 'The rent step is removed.')
            elif action == 'generate_steps':
                day = parse_date(request.POST.get('first_effective'))
                years = int(request.POST.get('years') or 0)
                percent = request.POST.get('percent', '').strip()
                amount = request.POST.get('amount', '').strip()
                if day is None or years < 1 or years > 30:
                    raise ValueError('Give the first increase date and how many years (1 to 30).')
                try:
                    made = rent.generate_steps(lease, day, years, percent=Decimal(percent) if percent else None, amount=Decimal(amount.replace('$', '').replace(',', '')) if amount else None)
                except InvalidOperation:
                    raise ValueError('The percent or amount is not a number.')
                messages.success(request, f'{len(made)} yearly rent step{"s" if len(made) != 1 else ""} made.')
            elif action == 'add_season':
                start, end = int(request.POST.get('start_month') or 0), int(request.POST.get('end_month') or 0)
                if not (1 <= start <= 12 and 1 <= end <= 12):
                    raise ValueError('Choose the first and last month of the season.')
                SeasonalRent.objects.create(lease=lease, name=(request.POST.get('name') or '').strip()[:80], start_month=start, end_month=end, monthly_rent=parse_money(request.POST.get('monthly_rent')))
                messages.success(request, 'Seasonal rent added.')
            elif action == 'delete_season':
                SeasonalRent.objects.filter(pk=request.POST.get('season'), lease=lease).delete()
                messages.success(request, 'The seasonal rent is removed.')
            elif action == 'upload_doc':
                f = request.FILES.get('file')
                if f is None:
                    raise ValueError('Choose the file to keep.')
                LeaseDocument.objects.create(lease=lease, file=f, kind=request.POST.get('kind') if request.POST.get('kind') in dict(LeaseDocument.Kind.choices) else 'lease',
                                             name=(request.POST.get('name') or f.name)[:200], uploaded_by=request.user)
                messages.success(request, 'The document is on file.')
            elif action == 'delete_doc':
                LeaseDocument.objects.filter(pk=request.POST.get('doc'), lease=lease).delete()
                messages.success(request, 'The document is removed.')
            elif action == 'end_lease':
                lease.end_date = parse_date(request.POST.get('end_date')) or timezone.localdate()
                lease.status = Lease.Status.ENDED
                lease.save(update_fields=['end_date', 'status'])
                rent.ensure_ownership(lease)
                messages.success(request, f'The lease ends {lease.end_date:%b} {lease.end_date.day}, {lease.end_date.year}.')
        except (ValueError, ArithmeticError) as e:
            messages.error(request, str(e))
        return redirect(here)
    today = timezone.localdate()
    accounts = [a for a in charges.accounts(prop) if a['unit'].pk == lease.unit_id]
    return render(request, 'commercial/lease_detail.html', {
        'assoc': prop, 'lease': lease, 'unit': lease.unit, 'schedule': rent.schedule(lease, today.replace(day=1), 12), 'steps': list(lease.steps.all()), 'seasons': list(lease.seasons.all()),
        'documents': list(lease.documents.all()), 'rows': list(reversed(charges.ledger_rows(lease.unit))), 'balance': sum((a['balance'] for a in accounts), Decimal('0.00')),
        'invoices': list(lease.invoices.order_by('-month')[:12]), 'months': [(i, n) for i, n in enumerate(['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'], 1)],
        'kinds': LeaseDocument.Kind.choices, 'today': today, 'books_on': ledger.profile_for(prop).we_do_financials, **acc_up(request, prop, ['commercial:leases', 'commercial:invoices', 'commercial:cam', 'accounting:units', 'accounting:report_aging', 'accounting:charges', 'accounting:hub', 'accounting:close'], reverse('commercial:leases', args=[prop.pk]), 'Leases')})


# ---------------------------------------------------------------- tenant invoices

@admin_only
def invoice_list(request, pk):
    prop = commercial_or_404(pk)
    today = timezone.localdate()
    month = parse_month(request.GET.get('month') or request.POST.get('month'), today.replace(day=1) if today.day <= 20 else ledger.next_month(today))
    cfg = rent.settings_for(prop)
    here = reverse('commercial:invoices', args=[prop.pk]) + f'?month={month:%Y-%m}'
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'settings':
                days = int(request.POST.get('days') or 0)
                if not 0 <= days <= 45:
                    raise ValueError('Post the rent between 0 and 45 days ahead.')
                cfg.rent_post_days_ahead = days
                cfg.auto_send_invoices = bool(request.POST.get('auto_send'))
                cfg.payment_instructions = (request.POST.get('instructions') or '').strip()
                cfg.save()
                messages.success(request, 'Saved.')
            elif action == 'post_rent':
                made = failed = 0
                for lease in rent.leases_for(prop, month):
                    try:
                        got = rent.post_lease_month(lease, month, user=request.user)
                    except ledger.LedgerError as e:
                        failed += 1
                        messages.error(request, str(e))
                        break
                    made += len(got)
                    if got or rent.is_posted(lease, month):
                        invoices.ensure_invoice(lease, month)
                messages.success(request, f'Posted {made} charge{"s" if made != 1 else ""} for {month:%B %Y}.' if made else f'Nothing more to post for {month:%B %Y}.')
            elif action in ('send', 'resend'):
                inv = get_object_or_404(TenantInvoice, pk=request.POST.get('invoice'), property=prop)
                invoices.send_invoice(inv, user=request.user, again=(action == 'resend' or bool(request.POST.get('again'))))
                messages.success(request, f'Invoice {inv.number} sent to {", ".join(inv.sent_to)}.')
            elif action == 'send_all':
                sent, problems = 0, []
                for inv in TenantInvoice.objects.filter(property=prop, month=month, status=TenantInvoice.Status.READY):
                    try:
                        invoices.send_invoice(inv, user=request.user)
                        sent += 1
                    except invoices.InvoiceError as e:
                        problems.append(f'{inv.tenant_name}: {e}')
                if sent:
                    messages.success(request, f'Sent {sent} invoice{"s" if sent != 1 else ""}.')
                for p in problems:
                    messages.error(request, p)
                if not sent and not problems:
                    messages.info(request, 'Every invoice for the month was already sent.')
            elif action == 'refresh':
                invoices.refresh_invoice(get_object_or_404(TenantInvoice, pk=request.POST.get('invoice'), property=prop))
                messages.success(request, 'The invoice is taken again from the books.')
            elif action == 'make':
                lease = get_object_or_404(Lease, pk=request.POST.get('lease'), property=prop)
                inv, new = invoices.ensure_invoice(lease, month)
                messages.success(request, f'Invoice {inv.number} is ready.' if new else 'That invoice is already made.')
        except (invoices.InvoiceError, ValueError) as e:
            messages.error(request, str(e))
        return redirect(here)
    invs = list(TenantInvoice.objects.filter(property=prop, month=month).select_related('lease'))
    have = {i.lease_id for i in invs}
    waiting = []
    for lease in rent.leases_for(prop, month):
        if lease.pk in have:
            continue
        due = rent.rent_for_month(lease, month)
        if due['rent'] > 0 or due['cam'] > 0:
            waiting.append({'lease': lease, 'posted': rent.is_posted(lease, month), 'rent': due['rent'], 'cam': due['cam']})
    return render(request, 'commercial/invoices.html', {'assoc': prop, 'month': month, 'previous': ledger.previous_month(month), 'next': ledger.next_month(month), 'invoices': invs, 'waiting': waiting, 'cfg': cfg,
                                                        'ready': sum(1 for i in invs if i.status == i.Status.READY), 'delivery': invoices.board_send.delivery_available(),
                                                        'post_on': month - timedelta(days=cfg.rent_post_days_ahead), **acc_up(request, prop, ['commercial:leases', 'accounting:hub', 'accounting:close'], reverse('commercial:leases', args=[prop.pk]), 'Leases')})


@admin_only
def invoice_pdf(request, pk, inv_pk):
    prop = commercial_or_404(pk)
    inv = get_object_or_404(TenantInvoice, pk=inv_pk, property=prop)
    resp = HttpResponse(invoices.pdf_bytes(inv), content_type='application/pdf')
    resp['Content-Disposition'] = f'inline; filename="{invoices.filename(inv)}"'
    return resp


def property_card(prop):
    """What the property page shows an administrator for a commercial building: whether the books are on, the leases, what is owed and what is ending."""
    profile = ledger.profile_for(prop)
    today = timezone.localdate()
    leases_ = list(Lease.objects.filter(property=prop).select_related('unit', 'tenant').prefetch_related('steps', 'seasons'))
    active = [l for l in leases_ if rent.is_billable(l, today)]
    owed = sum((a['balance'] for a in charges.accounts(prop) if a['balance'] > 0 and a.get('current')), Decimal('0.00'))
    soon = [l for l in active if l.end_date and l.end_date <= today + timedelta(days=90)]
    return {'books_on': profile.we_do_financials, 'leases': len(active), 'units': prop.units.filter(is_active=True).count(), 'owed': owed, 'ending_soon': soon,
            'monthly_rent': sum((rent.rent_for_month(l, today)['monthly'] for l in active), Decimal('0.00'))}


@admin_only
def owners(request, pk):
    """Who receives the property's financial package each month: the property's owner or owners (kept as board terms with the position 'Owner')."""
    from accounting.models import BoardTerm
    from core.models import Contact
    prop = commercial_or_404(pk)
    here = reverse('commercial:owners', args=[prop.pk])
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'add':
                name = ' '.join((request.POST.get('name') or '').split())
                email = (request.POST.get('email') or '').strip()
                if not name:
                    raise ValueError("Type the owner's name.")
                if email:
                    from django.core.exceptions import ValidationError
                    from django.core.validators import validate_email
                    try:
                        validate_email(email)
                    except ValidationError:
                        raise ValueError(f'"{email}" is not an email address.')
                person = Contact.objects.filter(name__iexact=name, properties=prop).first() or Contact.objects.filter(name__iexact=name, contact_type=Contact.ContactType.OWNER).first()
                if person is None:
                    person = Contact.objects.create(name=name, contact_type=Contact.ContactType.OWNER, source=Contact.Source.MANUAL)
                if email and person.email != email:
                    person.email = email
                    person.save(update_fields=['email'])
                person.properties.add(prop)
                if not BoardTerm.objects.filter(association=prop, person=person, end_date__isnull=True).exists():
                    BoardTerm.objects.create(association=prop, person=person, position=(request.POST.get('position') or 'Owner').strip()[:60] or 'Owner', start_date=timezone.localdate())
                messages.success(request, f'{person.name} will get the financials.')
            elif action == 'remove':
                BoardTerm.objects.filter(pk=request.POST.get('term'), association=prop).update(end_date=timezone.localdate())
                messages.success(request, 'Removed: they will not get the next package.')
        except ValueError as e:
            messages.error(request, str(e))
        return redirect(here)
    terms = list(BoardTerm.objects.filter(association=prop, end_date__isnull=True).select_related('person').order_by('person__name'))
    return render(request, 'commercial/owners.html', {'assoc': prop, 'terms': terms, **acc_up(request, prop, ['commercial:leases', 'accounting:hub', 'accounting:close'], reverse('commercial:leases', args=[prop.pk]), 'Leases')})
