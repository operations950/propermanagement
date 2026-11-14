"""The Print checks and Print envelopes screens of the Payables module: every association at once. The work is in check_runs.py."""
from django.contrib import messages
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from . import check_runs as cr
from . import ledger, payables
from .models import Bill, Payment
from .web import admin_only, association_or_404, parse_date

CHECKS_URL, ENVELOPES_URL = 'accounting:payables_print_checks', 'accounting:payables_print_envelopes'


def _pdf(data, name):
    response = HttpResponse(data, content_type='application/pdf')
    response['Content-Disposition'] = f'inline; filename="{name}"'
    return response


def _ints(values):
    return [int(v) for v in values if str(v).isdigit()]


@admin_only
def print_checks(request):
    """Every approved payable that is ready, by bank account, as the checks that would be written. Print makes the payments and hands back one PDF with every check in order."""
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'print':
                ids = set(_ints(request.POST.getlist('bill')))
                day = parse_date(request.POST.get('date'), timezone.localdate())
                made = cr.print_run(ids, day, request.user)
                if not made:
                    raise payables.PayableError('Tick at least one payable to print.')
                return _pdf(cr.checks_pdf(made), f'Checks {day:%Y-%m-%d}.pdf')
            if action == 'void':
                payment = get_object_or_404(Payment, pk=request.POST.get('payment'), method=Payment.Method.CHECK)
                payables.void_payment(payment, request.user)
                Payment.objects.filter(pk=payment.pk, envelope_state='waiting').update(envelope_state='')            # a voided check needs no envelope
                messages.success(request, f'Check {payment.number} to {payment.payee} is void. Its payables are ready to pay again.')
            elif action == 'void_bill':
                bill = get_object_or_404(Bill, pk=request.POST.get('bill'))
                payables.void_bill(bill, request.user)
                messages.success(request, f'The payable to {bill.payee} is void.')
            elif action == 'mark_paid':
                bill = get_object_or_404(Bill, pk=request.POST.get('bill'), status=Bill.Status.APPROVED)
                day = parse_date(request.POST.get('date'), timezone.localdate())
                cr.mark_paid(bill, day, request.user)
                messages.success(request, f'{bill.payee}, {bill.balance or bill.amount:,.2f}: recorded as paid by the bank on {day:%b} {day.day}.')
        except (payables.PayableError, ledger.LedgerError) as e:
            messages.error(request, str(e))
        return redirect(CHECKS_URL)
    result = cr.plan()
    return render(request, 'accounting/check_run.html', {**result, 'today': timezone.localdate(), 'runs': cr.recent_runs(), 'Void': Payment.Status.VOID})


@admin_only
def checks_pdf(request):
    """The checks as a PDF again (a reprint: nothing is posted), or one made-up test check on an association's layout (?test=<association>)."""
    if request.GET.get('test'):
        prop = association_or_404(request.GET['test'])
        return _pdf(cr.sample_checks_pdf(prop, guides=bool(request.GET.get('guides'))), f'Test check - {prop.name}.pdf')
    if request.GET.get('payment'):
        payments = list(Payment.objects.filter(pk=request.GET['payment'], method=Payment.Method.CHECK).exclude(status=Payment.Status.VOID).select_related('association', 'vendor'))
        name = 'Check reprint.pdf'
    elif request.GET.get('run'):
        payments = cr.run_payments(request.GET['run'])
        name = 'Checks reprint.pdf'
    else:
        raise Http404
    if not payments:
        raise Http404
    return _pdf(cr.checks_pdf(payments), name)


def _envelope_pdf_for(ids):
    payments = list(Payment.objects.filter(pk__in=ids, method=Payment.Method.CHECK).exclude(status=Payment.Status.VOID).select_related('association', 'vendor'))
    order = {pk: i for i, pk in enumerate(ids)}
    payments.sort(key=lambda p: order[p.pk])
    return payments, cr.envelope_rows(payments)


@admin_only
def print_envelopes(request):
    """The envelopes still to deal with, from every check run: print the ticked ones, skip the ones that are not mailed. Printed and skipped ones leave the list."""
    if request.method == 'POST':
        action = request.POST.get('action')
        ids = _ints(request.POST.getlist('id'))
        if action == 'print':
            payments, rows = _envelope_pdf_for(ids)
            if not any(r['lines'] for r in rows):
                messages.error(request, 'None of the ticked checks has a vendor address to print.')
                return redirect(ENVELOPES_URL)
            cr.set_envelope_state([r['payment'].pk for r in rows if r['lines']], 'printed')
            return _pdf(cr.envelopes_pdf([r for r in rows if r['lines']]), 'Envelopes.pdf')
        if not ids:
            messages.error(request, 'Tick at least one envelope first.')
        elif action in ('printed', 'skipped', 'waiting'):
            n = cr.set_envelope_state(ids, action)
            messages.success(request, {'printed': f'{n} marked as printed.', 'skipped': f'{n} skipped.', 'waiting': f'{n} back on the list.'}[action])
        return redirect(ENVELOPES_URL)
    rows = cr.envelope_rows(cr.waiting_envelopes())
    for row in rows:
        row['edit_url'] = reverse('contact_edit', args=[row['vendor'].pk]) if row['vendor'] is not None else ''
        row['ticked'] = bool(row['lines']) and not row['own']
    done = cr.envelope_rows(cr.finished_envelopes())
    return render(request, 'accounting/envelopes.html', {'rows': rows, 'done': done, 'return_lines': cr.return_lines(), 'n_missing': sum(1 for r in rows if not r['lines'])})


@admin_only
def envelopes_pdf(request):
    """Envelopes as a PDF again, or ?test=1 for a single sample to try the printer: nothing is marked printed here (printing from the list does that)."""
    ids = _ints(request.GET.getlist('id'))
    payments, rows = _envelope_pdf_for(ids)
    test = bool(request.GET.get('test'))
    if not test and not any(r['lines'] for r in rows):
        messages.error(request, 'None of the ticked checks has a vendor address to print.')
        return redirect(ENVELOPES_URL)
    return _pdf(cr.envelopes_pdf(rows, test), 'Envelopes - test.pdf' if test else 'Envelopes.pdf')
