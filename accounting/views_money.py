"""Money screens: the bank reconciliation, bills and checks, recurring bills, journal entries and reports."""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib import messages
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from core.models import Contact, Unit

from . import bankrec, charges, checkprint, invoice_inbox, ledger, owners, payables
from . import standard_chart as sc
from .models import (ZERO, Account, Bill, BankAccount, BankLine, Budget, BudgetLine, JournalEntry, Payment, ReconMatch, Reconciliation, RecurringBill, VendorTerms)
from .web import admin_only, association_or_404, books_or_redirect, default_month, month_context, parse_date, parse_money, parse_month


def _go(name, *args, **query):
    url = reverse(f'accounting:{name}', args=args)
    return url + ('?' + '&'.join(f'{k}={v}' for k, v in query.items()) if query else '')


def _books(request, pk):
    prop = association_or_404(pk)
    return prop, books_or_redirect(request, prop)


# ---------------------------------------------------------------- bank reconciliation

@admin_only
def bank(request, pk, bank_pk, month):
    prop, redirect_to = _books(request, pk)
    if redirect_to:
        return redirect_to
    acct = get_object_or_404(BankAccount, pk=bank_pk, association=prop)
    first = parse_month(month, default_month())
    here = _go('bank', prop.pk, acct.pk, f'{first:%Y-%m}')
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'upload':
                f = request.FILES.get('file')
                if f is None:
                    raise bankrec.StatementError('Choose the statement file.')
                data = f.read()
                parsed, how = bankrec.read_statement(f.name, data)
                typed = (request.POST.get('end_balance') or '').strip()
                f.seek(0)
                st = bankrec.import_statement(acct, parsed, file=f, read_by=how, end_balance=parse_money(typed) if typed else None)
                first = ledger.month_start(st.period_end)
                made = bankrec.auto_match(acct, first)
                messages.success(request, f'Read {st.lines.count()} lines from the statement and matched {made} group{"s" if made != 1 else ""}.')
                here = _go('bank', prop.pk, acct.pk, f'{first:%Y-%m}')
            elif action == 'auto':
                messages.success(request, f'Matched {bankrec.auto_match(acct, first)} more.')
            elif action == 'accept':
                bankrec.accept_timing(acct, first, request.POST['item'], request.user, request.POST.get('note') or '')
            elif action == 'unaccept':
                bankrec.unaccept(acct, first, request.POST['item'])
            elif action == 'unmatch':
                bankrec.unmatch(get_object_or_404(ReconMatch, pk=request.POST.get('match'), bank_account=acct))
                messages.success(request, 'Unmatched.')
            elif action == 'match_manual':
                bankrec.match_manual(acct, first, [int(i) for i in request.POST.getlist('bank_line')], request.POST.getlist('item'))
                messages.success(request, 'Matched.')
            elif action == 'add_to_books':
                line = get_object_or_404(BankLine, pk=request.POST.get('bank_line'), statement__bank_account=acct)
                account = get_object_or_404(Account, pk=request.POST.get('account'), association=prop)
                vendor = payables.vendor_for_name(request.POST.get('vendor')) if (request.POST.get('vendor') or '').strip() else None
                bankrec.add_to_books(line, account, request.user, vendor=vendor, memo=(request.POST.get('memo') or '').strip())
                messages.success(request, 'Added to the books and matched.')
            elif action == 'owner_payment':
                line = get_object_or_404(BankLine, pk=request.POST.get('bank_line'), statement__bank_account=acct)
                unit = get_object_or_404(Unit, pk=request.POST.get('unit'), property=prop)
                owner = get_object_or_404(Contact, pk=request.POST.get('owner')) if request.POST.get('owner') else owners.billed_owner(unit)
                bankrec.owner_payment_from_line(line, unit, owner, request.user)
                messages.success(request, f'Recorded as a payment from {owner.name}.')
            elif action == 'close':
                bankrec.close_reconciliation(acct, first, request.user)
                messages.success(request, 'The reconciliation is closed.')
            elif action == 'reopen':
                bankrec.reopen_reconciliation(acct, first)
                messages.success(request, 'The reconciliation is open again.')
        except (bankrec.StatementError, ValueError, ledger.LedgerError, charges.ChargeError) as e:
            messages.error(request, str(e))
        return redirect(here)
    s = bankrec.summary(acct, first)
    s['rows'] = sorted(s['rows'], key=lambda r: r['item'].date)
    other_banks = prop.bank_accounts.filter(is_active=True)
    return render(request, 'accounting/bank.html', {'assoc': prop, 'bank': acct, 's': s, 'other_banks': other_banks, 'accounts': prop.accounts.filter(is_active=True).order_by('code'), 'units': owners.active_units(prop),
                                                    'acc_back': _go('close', prop.pk, f'{first:%Y-%m}'), 'acc_back_label': f'{prop.name} close', **month_context(first)})


# ---------------------------------------------------------------- bills

def _vendor_names(prop):
    names = set(Contact.objects.filter(contact_type=Contact.ContactType.VENDOR).values_list('name', flat=True)[:600])
    names |= set(Bill.objects.filter(association=prop).exclude(vendor__isnull=True).values_list('vendor__name', flat=True))
    return sorted(names, key=str.lower)


@admin_only
def bills(request, pk):
    prop, redirect_to = _books(request, pk)
    if redirect_to:
        return redirect_to
    here = _go('bills', prop.pk)
    profile = ledger.profile_for(prop)
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'new':
                vendor_name = ' '.join((request.POST.get('vendor') or '').split())
                if not vendor_name:
                    raise payables.PayableError('Say who the bill is from.')
                vendor = payables.vendor_for_name(vendor_name)
                account = get_object_or_404(Account, pk=request.POST.get('account'), association=prop)
                amount = parse_money(request.POST.get('amount'))
                kind = request.POST.get('kind') if request.POST.get('kind') in ('routine', 'exception', 'reserve') else 'exception'
                reserve = bool(request.POST.get('reserve')) or kind == 'reserve'
                bill = payables.create_bill(prop, parse_date(request.POST.get('date')) or timezone.localdate(), [(account, amount, (request.POST.get('memo') or '').strip())], vendor=vendor,
                                            number=(request.POST.get('number') or '').strip(), due=parse_date(request.POST.get('due')), kind=Bill.Kind.RESERVE if reserve else kind, reserve=reserve,
                                            source=Bill.Source.UPLOAD if request.FILES.get('file') else Bill.Source.MANUAL, file=request.FILES.get('file'), user=request.user, memo=(request.POST.get('memo') or '').strip())
                messages.success(request, 'The bill was added' + ('.' if bill.status == Bill.Status.APPROVED else ' and is waiting to be reviewed.'))
            elif action == 'approve':
                bill = get_object_or_404(Bill, pk=request.POST.get('bill'), association=prop)
                before = bill.status
                payables.approve_bill(bill, request.user)
                messages.success(request, 'Approved: it is ready to pay.' if bill.status == Bill.Status.APPROVED else 'This one needs the board. Record their approval when you have it.')
            elif action == 'board':
                bill = get_object_or_404(Bill, pk=request.POST.get('bill'), association=prop)
                by = (request.POST.get('by') or '').strip()
                if not by:
                    raise payables.PayableError('Say who on the board approved it.')
                payables.approve_bill(bill, request.user, board_by=by, board_on=parse_date(request.POST.get('on')))
                messages.success(request, 'The board\'s approval is recorded: it is ready to pay.')
            elif action == 'void':
                payables.void_bill(get_object_or_404(Bill, pk=request.POST.get('bill'), association=prop), request.user)
                messages.success(request, 'The bill is void.')
            elif action == 'inbox_run':
                made, note = invoice_inbox.run(request.user)
                messages.success(request, note)
            elif action == 'inbox_save':
                box = invoice_inbox.InvoiceInbox.get()
                box.enabled, box.label = bool(request.POST.get('enabled')), (request.POST.get('label') or 'Invoices').strip()[:80]
                box.save(update_fields=['enabled', 'label'])
                messages.success(request, 'Saved.')
            elif action == 'inbox_assign':
                seen = get_object_or_404(invoice_inbox.SeenEmail, pk=request.POST.get('seen'))
                msg = invoice_inbox.fetch_one(seen.message_id)
                made = invoice_inbox.process_message(msg, association=prop, user=request.user)
                messages.success(request, f'{made} draft bill{"s" if made != 1 else ""} made.' if made else 'No bill could be made from that email.')
        except (payables.PayableError, ledger.LedgerError, ValueError) as e:
            messages.error(request, str(e))
        except Exception:
            import logging
            logging.getLogger(__name__).exception('Bill action failed')
            messages.error(request, 'That did not work. Try again.')
        return redirect(here)
    qs = Bill.objects.filter(association=prop).select_related('vendor').prefetch_related('payments', 'lines__account')
    review = [b for b in qs.filter(status__in=[Bill.Status.DRAFT, Bill.Status.AWAITING_BOARD]).order_by('date')]
    for b in review:
        b.need = payables.requirement(prop, b)
    box = invoice_inbox.InvoiceInbox.get()
    return render(request, 'accounting/bills.html', {'assoc': prop, 'profile': profile, 'review': review, 'ready': payables.bills_to_pay(prop), 'paid': list(qs.filter(status=Bill.Status.PAID).order_by('-date')[:15]),
                                                     'expense_accounts': prop.accounts.filter(kind='expense', is_active=True).order_by('code'), 'vendors': _vendor_names(prop), 'today': timezone.localdate(),
                                                     'box': box, 'inbox_ready': invoice_inbox.is_configured(), 'needs_home': invoice_inbox.SeenEmail.objects.filter(needs_home=True).order_by('-created_at')[:10],
                                                     'acc_back': _go('hub', prop.pk), 'acc_back_label': 'Accounting'})


@admin_only
def bill_detail(request, pk, bill_pk):
    prop, redirect_to = _books(request, pk)
    if redirect_to:
        return redirect_to
    bill = get_object_or_404(Bill, pk=bill_pk, association=prop)
    return render(request, 'accounting/bill_detail.html', {'assoc': prop, 'bill': bill, 'need': payables.requirement(prop, bill), 'profile': ledger.profile_for(prop), 'acc_back': _go('bills', prop.pk), 'acc_back_label': 'Bills'})


@admin_only
def pay(request, pk):
    """The check run: choose approved bills, pay them, print the checks."""
    prop, redirect_to = _books(request, pk)
    if redirect_to:
        return redirect_to
    ready = payables.bills_to_pay(prop)
    if request.method == 'POST':
        try:
            ids = [int(i) for i in request.POST.getlist('bill')]
            chosen = [b for b in ready if b.pk in ids]
            if not chosen:
                raise payables.PayableError('Choose the bills to pay.')
            bank_acct = get_object_or_404(BankAccount, pk=request.POST.get('bank'), association=prop)
            method = request.POST.get('method') if request.POST.get('method') in ('check', 'ach') else 'check'
            day = parse_date(request.POST.get('date')) or timezone.localdate()
            by_vendor = {}
            for b in chosen:
                by_vendor.setdefault(b.vendor_id or -b.pk, []).append(b)
            made = []
            for group in by_vendor.values():
                made.append(payables.pay_bills(prop, bank_acct, [(b, b.balance) for b in group], day, method=method, user=request.user))
            messages.success(request, f'{len(made)} payment{"s" if len(made) != 1 else ""} recorded.')
            if method == 'check':
                return redirect(_go('check_print', prop.pk) + '?ids=' + ','.join(str(p.pk) for p in made))
            return redirect(_go('checks', prop.pk))
        except (payables.PayableError, ledger.LedgerError) as e:
            messages.error(request, str(e))
        return redirect(_go('pay', prop.pk))
    return render(request, 'accounting/pay.html', {'assoc': prop, 'ready': ready, 'banks': prop.bank_accounts.filter(is_active=True), 'today': timezone.localdate(), 'acc_back': _go('bills', prop.pk), 'acc_back_label': 'Bills'})


@admin_only
def checks(request, pk):
    prop, redirect_to = _books(request, pk)
    if redirect_to:
        return redirect_to
    if request.method == 'POST':
        try:
            payables.void_payment(get_object_or_404(Payment, pk=request.POST.get('payment'), association=prop), request.user)
            messages.success(request, 'The payment is void and its bills are ready to pay again.')
        except (payables.PayableError, ledger.LedgerError) as e:
            messages.error(request, str(e))
        return redirect(_go('checks', prop.pk))
    pays = Payment.objects.filter(association=prop).select_related('vendor', 'bank_account').order_by('-date', '-id')[:200]
    return render(request, 'accounting/checks.html', {'assoc': prop, 'payments': pays, 'profile': ledger.profile_for(prop), 'acc_back': _go('bills', prop.pk), 'acc_back_label': 'Bills'})


@admin_only
def check_print(request, pk):
    """The checks, laid out on the blank stock, ready for the browser's print dialog. ?sample=1 prints one made-up check for lining the stock up."""
    prop, redirect_to = _books(request, pk)
    if redirect_to:
        return redirect_to
    profile = ledger.profile_for(prop)
    layout = checkprint.layout_for(profile)
    sample = bool(request.GET.get('sample'))
    items = []
    if sample:
        items = [checkprint.sample_check()]
    else:
        ids = [int(i) for i in (request.GET.get('ids') or '').split(',') if i.strip().isdigit()]
        pays = list(Payment.objects.filter(association=prop, pk__in=ids, method='check').exclude(status='void').select_related('vendor'))
        items = [dict(checkprint.check_fields(p), payment=p) for p in pays]
        if request.method == 'POST' and request.POST.get('action') == 'printed':
            Payment.objects.filter(pk__in=[i['payment'].pk for i in items]).update(printed_at=timezone.now())
            messages.success(request, 'Marked as printed.')
            return redirect(_go('checks', prop.pk))
    backdrop = profile.check_stock.url if (sample and profile.check_stock and profile.check_stock.name.lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.gif'))) else ''
    return render(request, 'accounting/check_print.html', {'assoc': prop, 'items': items, 'pos': checkprint.positions(layout), 'layout': layout, 'sample': sample, 'backdrop': backdrop})


@admin_only
def check_setup(request, pk):
    prop, redirect_to = _books(request, pk)
    if redirect_to:
        return redirect_to
    profile = ledger.profile_for(prop)
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'upload':
                f = request.FILES.get('file')
                if f is None:
                    raise checkprint.CheckPrintError('Choose a picture or PDF of the blank check.')
                data = f.read()
                f.seek(0)
                try:
                    layout = checkprint.learn_layout(data, f.name)
                finally:
                    pass
                profile.check_stock.save(f.name, f, save=False)
                profile.check_layout = layout
                profile.save()
                messages.success(request, 'The layout was read from your check stock. Print a test on plain paper held over a check to see if it lines up.')
            elif action == 'nudge':
                profile.check_layout = checkprint.nudge(profile.check_layout or dict(checkprint.DEFAULT_LAYOUT), float(request.POST.get('dx') or 0), float(request.POST.get('dy') or 0))
                profile.save(update_fields=['check_layout'])
            elif action == 'reset':
                profile.check_layout = {}
                profile.save(update_fields=['check_layout'])
                messages.success(request, 'Back to the standard layout.')
            elif action == 'next_number':
                profile.next_check_number = max(1, int(request.POST.get('number') or 1))
                profile.save(update_fields=['next_check_number'])
                messages.success(request, 'The next check number is saved.')
        except checkprint.CheckPrintError as e:
            if request.FILES.get('file'):
                # keep the stock on file even if reading it failed, so it can be read later
                profile.check_stock.save(request.FILES['file'].name, request.FILES['file'], save=True)
            messages.error(request, str(e))
        except ValueError:
            messages.error(request, 'That is not a number.')
        return redirect(_go('check_setup', prop.pk))
    layout = checkprint.layout_for(profile)
    return render(request, 'accounting/check_setup.html', {'assoc': prop, 'profile': profile, 'layout': layout, 'acc_back': _go('checks', prop.pk), 'acc_back_label': 'Checks'})


# ---------------------------------------------------------------- recurring bills

@admin_only
def recurring(request, pk):
    prop, redirect_to = _books(request, pk)
    if redirect_to:
        return redirect_to
    here = _go('recurring', prop.pk)
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'save':
                vendor = payables.vendor_for_name(request.POST.get('vendor'))
                if vendor is None:
                    raise ValueError('Say who the bill is from.')
                account = get_object_or_404(Account, pk=request.POST.get('account'), association=prop)
                amount = parse_money(request.POST.get('amount')) if (request.POST.get('amount') or '').strip() else None
                nxt = parse_date(request.POST.get('next_date'))
                if nxt is None:
                    raise ValueError('Give the date of the next bill.')
                fields = dict(vendor=vendor, description=(request.POST.get('description') or vendor.name).strip()[:200], account=account, amount=amount, next_date=nxt,
                              frequency=request.POST.get('frequency') if request.POST.get('frequency') in dict(RecurringBill.Frequency.choices) else 'monthly',
                              pay_method=request.POST.get('pay_method') if request.POST.get('pay_method') in ('check', 'ach') else 'check', statement_match=(request.POST.get('statement_match') or '').strip()[:80])
                rid = request.POST.get('rec')
                if rid:
                    RecurringBill.objects.filter(pk=rid, association=prop).update(**fields)
                else:
                    RecurringBill.objects.create(association=prop, **fields)
                VendorTerms.objects.update_or_create(association=prop, vendor=vendor, defaults={'default_account': account, 'pay_method': fields['pay_method']})
                messages.success(request, 'Saved.')
            elif action == 'stop':
                RecurringBill.objects.filter(pk=request.POST.get('rec'), association=prop).update(is_active=False)
            elif action == 'resume':
                RecurringBill.objects.filter(pk=request.POST.get('rec'), association=prop).update(is_active=True)
            elif action == 'post_due':
                made = payables.post_recurring(prop, timezone.localdate(), request.user)
                messages.success(request, f'Posted {len(made)} bill{"s" if len(made) != 1 else ""}.' if made else 'Nothing was due.')
        except (ValueError, payables.PayableError, ledger.LedgerError) as e:
            messages.error(request, str(e))
        return redirect(here)
    rows = list(RecurringBill.objects.filter(association=prop).select_related('vendor', 'account').order_by('-is_active', 'description'))
    edit = next((r for r in rows if str(r.pk) == request.GET.get('edit')), None)
    return render(request, 'accounting/recurring.html', {'assoc': prop, 'rows': rows, 'edit': edit, 'vendors': _vendor_names(prop), 'expense_accounts': prop.accounts.filter(kind='expense', is_active=True).order_by('code'),
                                                         'frequencies': RecurringBill.Frequency.choices, 'profile': ledger.profile_for(prop), 'today': timezone.localdate(), 'acc_back': _go('hub', prop.pk), 'acc_back_label': 'Accounting'})


# ---------------------------------------------------------------- journal

@admin_only
def journal(request, pk):
    prop, redirect_to = _books(request, pk)
    if redirect_to:
        return redirect_to
    here = _go('journal', prop.pk)
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'entry':
                lines = []
                for i in range(1, 9):
                    code = (request.POST.get(f'account_{i}') or '').strip()
                    if not code:
                        continue
                    acct = get_object_or_404(Account, pk=code, association=prop)
                    debit = parse_money(request.POST.get(f'debit_{i}')) if (request.POST.get(f'debit_{i}') or '').strip() else ZERO
                    credit = parse_money(request.POST.get(f'credit_{i}')) if (request.POST.get(f'credit_{i}') or '').strip() else ZERO
                    lines.append({'account': acct, 'debit': debit, 'credit': credit, 'memo': (request.POST.get(f'memo_{i}') or '').strip()})
                basis = request.POST.get('basis') if request.POST.get('basis') in ('both', 'accrual', 'cash') else 'both'
                e = ledger.post_entry(prop, parse_date(request.POST.get('date')) or timezone.localdate(), lines, memo=(request.POST.get('memo') or '').strip(), basis=basis, user=request.user)
                messages.success(request, f'Posted as J-{e.number}.')
            elif action == 'reverse':
                e = get_object_or_404(JournalEntry, pk=request.POST.get('entry'), association=prop)
                r = ledger.reverse_entry(e, parse_date(request.POST.get('date')) or timezone.localdate(), request.user)
                messages.success(request, f'J-{e.number} was reversed by J-{r.number}.')
            elif action == 'transfer':
                amount = parse_money(request.POST.get('amount'))
                if amount <= 0:
                    raise ValueError('The amount must be above zero.')
                to_reserves = request.POST.get('direction') != 'to_operating'
                fund = get_object_or_404(Account, pk=request.POST.get('fund'), association=prop)
                op = ledger.account(prop, sc.CASH_OPERATING)
                res = ledger.account(prop, sc.CASH_RESERVE)
                transfer = ledger.account(prop, sc.RESERVE_TRANSFER)
                day = parse_date(request.POST.get('date')) or timezone.localdate()
                cash_in, cash_out = (res, op) if to_reserves else (op, res)
                lines = [{'account': cash_in, 'debit': amount}, {'account': cash_out, 'credit': amount},
                         {'account': transfer if to_reserves else fund, 'debit': amount}, {'account': fund if to_reserves else transfer, 'credit': amount}]
                e = ledger.post_entry(prop, day, lines, memo=('Transfer to reserves' if to_reserves else 'Transfer from reserves') + f', {fund.name}', source=JournalEntry.Source.TRANSFER, user=request.user)
                messages.success(request, f'Transferred ${amount:,.2f} (J-{e.number}).')
        except (ValueError, ledger.LedgerError) as e:
            messages.error(request, str(e))
        return redirect(here)
    month = parse_month(request.GET.get('month'), ledger.month_start(timezone.localdate()))
    entries = JournalEntry.objects.filter(association=prop, date__gte=month, date__lte=ledger.month_end(month)).prefetch_related('lines__account').order_by('-date', '-number')
    return render(request, 'accounting/journal.html', {'assoc': prop, 'entries': entries, 'accounts': prop.accounts.filter(is_active=True).order_by('code'), 'reserve_funds': prop.accounts.filter(kind='equity', code__gte='3811', code__lte='3899').order_by('code'),
                                                       'today': timezone.localdate(), 'range': range(1, 7), 'acc_back': _go('hub', prop.pk), 'acc_back_label': 'Accounting', **month_context(month)})


# ---------------------------------------------------------------- reports

def _basis(request):
    return 'cash' if request.GET.get('basis') == 'cash' else 'accrual'


def _range(request, prop):
    end = parse_date(request.GET.get('end')) or ledger.month_end(default_month())
    start = parse_date(request.GET.get('start')) or ledger.profile_for(prop).fiscal_year_start(end)
    return start, end


@admin_only
def reports(request, pk):
    prop, redirect_to = _books(request, pk)
    if redirect_to:
        return redirect_to
    month = default_month()
    return render(request, 'accounting/reports.html', {'assoc': prop, 'month': month, 'banks': prop.bank_accounts.filter(is_active=True), 'acc_back': _go('hub', prop.pk), 'acc_back_label': 'Accounting', 'today': timezone.localdate()})


@admin_only
def report_tb(request, pk):
    prop, redirect_to = _books(request, pk)
    if redirect_to:
        return redirect_to
    start, end = _range(request, prop)
    basis = _basis(request)
    tb = ledger.trial_balance(prop, start, end, basis)
    return render(request, 'accounting/report_tb.html', {'assoc': prop, 'tb': tb, 'basis': basis, 'start': start, 'end': end, 'acc_back': _go('reports', prop.pk), 'acc_back_label': 'Reports'})


@admin_only
def report_bs(request, pk):
    prop, redirect_to = _books(request, pk)
    if redirect_to:
        return redirect_to
    start, end = _range(request, prop)
    basis = _basis(request)
    bs = ledger.balance_sheet(prop, end, basis)
    order = ['Cash', 'Receivables', 'Other assets', 'Liabilities', 'Equity', 'Reserve funds']
    return render(request, 'accounting/report_bs.html', {'assoc': prop, 'bs': bs, 'basis': basis, 'end': end, 'start': start, 'order': order, 'acc_back': _go('reports', prop.pk), 'acc_back_label': 'Reports'})


@admin_only
def report_is(request, pk):
    prop, redirect_to = _books(request, pk)
    if redirect_to:
        return redirect_to
    start, end = _range(request, prop)
    basis = _basis(request)
    inc = ledger.income_statement(prop, start, end, basis)
    budget = Budget.objects.filter(association=prop, year=ledger.profile_for(prop).fiscal_year_start(end).year).first()
    months = (end.year - start.year) * 12 + end.month - start.month + 1
    amounts = {l.account_id: l.annual_amount for l in budget.lines.all()} if budget else {}
    for r in inc['income'] + inc['expense']:
        annual = amounts.get(r['account'].pk)
        r['budget'] = (annual * months / 12).quantize(Decimal('0.01')) if annual is not None else None
        r['variance'] = (r['amount'] - r['budget']) if r['budget'] is not None else None
    return render(request, 'accounting/report_is.html', {'assoc': prop, 'inc': inc, 'basis': basis, 'start': start, 'end': end, 'has_budget': bool(amounts), 'acc_back': _go('reports', prop.pk), 'acc_back_label': 'Reports'})


@admin_only
def report_gl(request, pk):
    prop, redirect_to = _books(request, pk)
    if redirect_to:
        return redirect_to
    start, end = _range(request, prop)
    basis = _basis(request)
    acct = prop.accounts.filter(pk=request.GET.get('account')).first() or prop.accounts.filter(code=sc.CASH_OPERATING).first()
    gl = ledger.general_ledger(prop, acct, start, end, basis) if acct else None
    return render(request, 'accounting/report_gl.html', {'assoc': prop, 'gl': gl, 'acct': acct, 'accounts': prop.accounts.order_by('code'), 'basis': basis, 'start': start, 'end': end, 'acc_back': _go('reports', prop.pk), 'acc_back_label': 'Reports'})


@admin_only
def report_aging(request, pk):
    prop, redirect_to = _books(request, pk)
    if redirect_to:
        return redirect_to
    end = parse_date(request.GET.get('end')) or timezone.localdate()
    return render(request, 'accounting/report_aging.html', {'assoc': prop, 'ag': charges.aging(prop, end), 'end': end, 'totals': charges.totals(charges.accounts(prop)), 'acc_back': _go('reports', prop.pk), 'acc_back_label': 'Reports'})


@admin_only
def report_checks(request, pk):
    prop, redirect_to = _books(request, pk)
    if redirect_to:
        return redirect_to
    start, end = _range(request, prop)
    pays = Payment.objects.filter(association=prop, date__gte=start, date__lte=end).select_related('vendor', 'bank_account').order_by('date', 'number')
    return render(request, 'accounting/report_checks.html', {'assoc': prop, 'payments': pays, 'start': start, 'end': end, 'total': sum((p.amount for p in pays if p.status != 'void'), ZERO), 'acc_back': _go('reports', prop.pk), 'acc_back_label': 'Reports'})


@admin_only
def report_rec(request, pk, bank_pk, month):
    """The bank reconciliation report: what a board packet carries. A closed month shows what was frozen when it closed."""
    prop, redirect_to = _books(request, pk)
    if redirect_to:
        return redirect_to
    acct = get_object_or_404(BankAccount, pk=bank_pk, association=prop)
    first = parse_month(month, default_month())
    s = bankrec.summary(acct, first)
    rec = Reconciliation.objects.filter(bank_account=acct, month=first, status='closed').first()
    cleared = []
    if s['statement']:
        cleared = list(s['statement'].lines.filter(match__isnull=False).order_by('kind', 'date'))
    return render(request, 'accounting/report_rec.html', {'assoc': prop, 'bank': acct, 's': s, 'rec': rec, 'cleared': cleared, 'acc_back': _go('reports', prop.pk), 'acc_back_label': 'Reports', **month_context(first)})


@admin_only
def budget(request, pk):
    prop, redirect_to = _books(request, pk)
    if redirect_to:
        return redirect_to
    profile = ledger.profile_for(prop)
    year = int(request.GET.get('year') or profile.fiscal_year_start(timezone.localdate()).year)
    bud, _ = Budget.objects.get_or_create(association=prop, year=year)
    if request.method == 'POST':
        saved = 0
        for acct in prop.accounts.filter(kind__in=['income', 'expense']):
            raw = (request.POST.get(f'amt_{acct.pk}') or '').strip()
            if raw == '':
                BudgetLine.objects.filter(budget=bud, account=acct).delete()
                continue
            try:
                BudgetLine.objects.update_or_create(budget=bud, account=acct, defaults={'annual_amount': parse_money(raw)})
                saved += 1
            except ValueError:
                messages.error(request, f'"{raw}" is not an amount ({acct.name}).')
                return redirect(_go('budget', prop.pk) + f'?year={year}')
        messages.success(request, f'Budget saved: {saved} account{"s" if saved != 1 else ""}.')
        return redirect(_go('budget', prop.pk) + f'?year={year}')
    have = {l.account_id: l.annual_amount for l in bud.lines.all()}
    used = set(ledger.lines(prop).values_list('account_id', flat=True))
    rows = [{'account': a, 'amount': have.get(a.pk)} for a in prop.accounts.filter(kind__in=['income', 'expense']).order_by('code') if a.pk in used or a.pk in have]
    return render(request, 'accounting/budget.html', {'assoc': prop, 'rows': rows, 'year': year, 'others': prop.accounts.filter(kind__in=['income', 'expense']).exclude(pk__in=[r['account'].pk for r in rows]).order_by('code'),
                                                      'acc_back': _go('reports', prop.pk), 'acc_back_label': 'Reports'})
