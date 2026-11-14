"""The Print checks and Print envelopes screens of the Payables module: every association at once. The work is in check_runs.py."""
from django.contrib import messages
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from . import check_runs as cr
from . import ledger, payables
from .models import Payment
from .web import admin_only, association_or_404, parse_date


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
                messages.success(request, f'Check {payment.number} to {payment.payee} is void. Its payables are ready to pay again.')
        except (payables.PayableError, ledger.LedgerError) as e:
            messages.error(request, str(e))
        return redirect('accounting:payables_print_checks')
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


@admin_only
def print_envelopes(request):
    """The checks printed lately, with the envelope each one goes in. Tick the ones to print."""
    runs = cr.recent_runs()
    only = request.GET.get('run')
    for r in runs:
        r['rows'] = cr.envelope_rows([p for p in r['payments'] if p.status != Payment.Status.VOID])
        for row in r['rows']:
            row['edit_url'] = reverse('contact_edit', args=[row['vendor'].pk]) if row['vendor'] is not None else ''
        r['ticked'] = (r['key'] == only) if only else (r is runs[0])
        r['missing'] = sum(1 for row in r['rows'] if not row['lines'])
    return render(request, 'accounting/envelopes.html', {'runs': runs, 'return_lines': cr.return_lines()})


@admin_only
def envelopes_pdf(request):
    """One #10 envelope per ticked check, in check order (?id=<payment>, repeated). ?test=1 makes a single sample to try the printer."""
    ids = _ints(request.GET.getlist('id'))
    payments = list(Payment.objects.filter(pk__in=ids, method=Payment.Method.CHECK).exclude(status=Payment.Status.VOID).select_related('association', 'vendor'))
    order = {pk: i for i, pk in enumerate(ids)}
    payments.sort(key=lambda p: order[p.pk])
    rows = cr.envelope_rows(payments)
    test = bool(request.GET.get('test'))
    if not test and not any(r['lines'] for r in rows):
        messages.error(request, 'None of the ticked checks has a vendor address to print.')
        return redirect('accounting:payables_print_envelopes')
    return _pdf(cr.envelopes_pdf(rows, test), 'Envelopes - test.pdf' if test else 'Envelopes.pdf')
