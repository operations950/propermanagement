"""The month-end lease renewals screen: the leases that end (or change rent rate) soon, each with a checkbox to say it is okay."""
from datetime import datetime

from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from accounting import ledger
from core.permissions import has_financial as _is_admin

from . import renewals


@login_required
@user_passes_test(_is_admin)
def renewals_page(request):
    today = timezone.localdate()
    try:
        month = datetime.strptime(request.GET.get('month') or request.POST.get('month') or '', '%Y-%m').date()
    except ValueError:
        month = ledger.previous_month(today)
    month = ledger.month_start(month)
    here = reverse('commercial:renewals') + f'?month={month:%Y-%m}'
    if request.method == 'POST':
        if request.POST.get('action') == 'confirm_all':
            for r in renewals.rows(month):
                renewals.set_confirmed(r['kind'], r['pk'], r['what'], True, request.user)
            messages.success(request, 'All confirmed.')
        else:
            try:
                renewals.set_confirmed(request.POST.get('kind'), int(request.POST.get('pk') or 0), request.POST.get('what'), request.POST.get('ok') == '1', request.user)
            except (ValueError, LookupError, renewals.Lease.DoesNotExist, renewals.LongTermLease.DoesNotExist):
                messages.error(request, 'That lease could not be found.')
        return redirect(here)
    items = renewals.rows(month)
    status, detail = renewals.summary(month)
    return render(request, 'commercial/renewals.html', {'rows': items, 'month': month, 'previous': ledger.previous_month(month), 'next': ledger.next_month(month), 'status': status, 'detail': detail,
                                                       'window': renewals.WINDOW_DAYS})
