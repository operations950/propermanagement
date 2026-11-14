"""The central Payables page: drop invoices, refine a review card, create the payable; approve for the board; see what is ready to pay and what was paid."""
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import ledger, payables, payables_hub as hub
from core.models import Contact

from .models import BankAccount, Bill, PayableUpload, SeenEmail
from .web import admin_only, parse_date

TABS = ('review', 'board', 'ready', 'checks', 'envelopes', 'paid')


def _ajax(request):
    return request.headers.get('X-Requested-With') == 'XMLHttpRequest'


def _card_html(request, upload):
    return render_to_string('accounting/_payables_card.html', {'c': hub.build_card(upload), 'properties': hub.picker_rows()}, request=request)


@admin_only
def payables_home(request):
    tab = request.POST.get('tab') or request.GET.get('tab') or 'review'
    tab = tab if tab in TABS else 'review'
    here = reverse('accounting:payables_home') + f'?tab={tab}'
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            bill = get_object_or_404(Bill, pk=request.POST.get('bill')) if request.POST.get('bill') else None
            if action == 'board_ok':
                bank = BankAccount.objects.filter(pk=request.POST.get('bank')).first() if request.POST.get('bank') else None
                hub.board_approve(bill, request.user, request.POST.get('by'), parse_date(request.POST.get('on')) or timezone.localdate(), bank)
                messages.success(request, 'The board\'s approval is recorded: it is ready to pay.')
            elif action == 'set_bank':
                hub.set_bank(bill, get_object_or_404(BankAccount, pk=request.POST.get('bank')))
                messages.success(request, 'The bank account is set.')
            elif action == 'void':
                payables.void_bill(bill, request.user)
                messages.success(request, 'The payable is void.')
        except (payables.PayableError, ledger.LedgerError) as e:
            messages.error(request, str(e))
        return redirect(here)
    if tab == 'review':
        hub.adopt_inbox_drafts()
    counts = hub.tab_counts()
    ctx = {'tab': tab, 'counts': counts, 'acc_back': reverse('ws_financials'), 'acc_back_label': 'Financials', 'today': timezone.localdate(), 'properties': hub.picker_rows()}
    if tab == 'review':
        ctx['cards'] = [hub.build_card(u) for u in PayableUpload.objects.filter(status=PayableUpload.Status.PENDING).select_related('bill')]
        ctx['vendors'] = list(Contact.objects.filter(contact_type=Contact.ContactType.VENDOR).order_by('name').values_list('name', flat=True)[:2000])
        ctx['needs_home'] = list(SeenEmail.objects.filter(needs_home=True).order_by('-created_at')[:10])
    elif tab == 'board':
        ctx['waiting'] = hub.waiting_for_board()
        for b in ctx['waiting']:
            b.bank_options = hub.banks_for(b.association, b.reserve)
    elif tab == 'ready':
        ctx['groups'] = hub.ready_groups()
    elif tab == 'paid':
        ctx['paid'] = hub.recently_paid()
    return render(request, 'accounting/payables_home.html', ctx)


@admin_only
@require_POST
def payables_upload(request):
    """One or several dropped files. The page sends them one at a time and gets each card back as HTML; a plain form post sends them all and comes back to the page."""
    files = request.FILES.getlist('files') or request.FILES.getlist('file')
    if not files:
        if _ajax(request):
            return JsonResponse({'ok': False, 'error': 'No file was sent.'}, status=400)
        messages.error(request, 'Choose an invoice to add.')
        return redirect(reverse('accounting:payables_home'))
    made, errors = [], []
    for f in files:
        try:
            made.append(hub.ingest(f, request.user))
        except hub.IntakeError as e:
            errors.append(str(e))
    if _ajax(request):
        if not made:
            return JsonResponse({'ok': False, 'error': ' '.join(errors)}, status=400)
        return JsonResponse({'ok': True, 'cards': [{'id': u.pk, 'html': _card_html(request, u)} for u in made], 'errors': errors})
    for e in errors:
        messages.error(request, e)
    if made:
        messages.success(request, f'{len(made)} invoice{"s" if len(made) != 1 else ""} read. Check each one and press Create.')
    return redirect(reverse('accounting:payables_home'))


@admin_only
@require_POST
def payables_card(request, pk):
    """One review card: refresh it after a change, make a vendor from it, create the payable, or discard it."""
    upload = get_object_or_404(PayableUpload, pk=pk, status=PayableUpload.Status.PENDING)
    action = request.POST.get('action') or 'refresh'
    note = ''
    try:
        hub.save_choices(upload, request.POST)
        if action == 'create_vendor':
            card = hub.build_card(upload)
            vendor = hub.create_vendor(upload, card['prop'], is_1099=bool(request.POST.get('is_1099')))
            note = f'{vendor.name} is now a vendor.'
        elif action == 'create':
            bill = hub.create_payable(upload, request.user)
            waiting = bill.status == Bill.Status.AWAITING_BOARD
            msg = (f'{bill.payee}, {bill.amount:,.2f}: sent to the board, waiting for approval.' if waiting else
                   f'{bill.payee}, {bill.amount:,.2f}: management approved and ready to pay from {bill.bank_account.label if bill.bank_account else "its bank account"}.')
            if _ajax(request):
                return JsonResponse({'ok': True, 'done': True, 'message': msg, 'waiting': waiting, 'html': render_to_string('accounting/_payables_done.html', {'bill': bill, 'waiting': waiting}, request=request)})
            messages.success(request, msg)
            return redirect(reverse('accounting:payables_home'))
        elif action == 'discard':
            hub.discard(upload)
            if _ajax(request):
                return JsonResponse({'ok': True, 'done': True, 'message': 'Discarded.', 'html': ''})
            messages.success(request, 'The invoice was discarded.')
            return redirect(reverse('accounting:payables_home'))
    except (payables.PayableError, ledger.LedgerError) as e:
        if _ajax(request):
            return JsonResponse({'ok': False, 'error': str(e), 'html': _card_html(request, upload)})
        messages.error(request, str(e))
        return redirect(reverse('accounting:payables_home'))
    if _ajax(request):
        return JsonResponse({'ok': True, 'html': _card_html(request, upload), 'note': note})
    if note:
        messages.success(request, note)
    return redirect(reverse('accounting:payables_home'))
