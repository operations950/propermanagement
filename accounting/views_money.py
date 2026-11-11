"""Money screens: the bank reconciliation, bills and checks, recurring bills, journal entries and reports."""
import json
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.contrib import messages
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from core.models import Contact, Unit

from . import bankrec, charges, checkprint, invoice_inbox, ledger, owners, payables, reporting, scheduled
from . import standard_chart as sc
from .models import (ZERO, Account, Bill, BankAccount, BankLine, Budget, BudgetLine, Charge, ChargeRule, JournalEntry, JournalRule, Payment, Receipt, ReconMatch, Reconciliation, RecurringBill, VendorTerms)
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
                report = {}
                made = bankrec.auto_match(acct, first, report)
                read = report.get('recurring', 0)
                messages.success(request, f'Read {st.lines.count()} lines from the statement and matched {made} group{"s" if made != 1 else ""}'
                                 + (f', {read} of them recurring bills the statement paid.' if read else '.'))
                here = _go('bank', prop.pk, acct.pk, f'{first:%Y-%m}')
            elif action == 'auto':
                report = {}
                messages.success(request, f'Matched {bankrec.auto_match(acct, first, report)} more.' + (f' {report["recurring"]} were recurring bills the statement paid.' if report.get('recurring') else ''))
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
            elif action == 'clear_selected':
                account = get_object_or_404(Account, pk=request.POST.get('account'), association=prop) if request.POST.get('account') else None
                day = parse_date(request.POST.get('date')) if (request.POST.get('date') or '').strip() else None
                if (request.POST.get('date') or '').strip() and day is None:
                    raise bankrec.StatementError('Type the date as month/day/year.')
                made = bankrec.clear_selected(acct, first, [int(i) for i in request.POST.getlist('bank_line')], request.POST.getlist('item'), (request.POST.get('note') or '').strip()[:200], request.user,
                                              account=account, day=day, memo=(request.POST.get('memo') or '').strip())
                messages.success(request, 'Cleared.' if made.how == 'cleared by hand' else 'The difference is posted and everything you ticked is cleared.')
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
    deposits_open = [b for b in s['unmatched_bank'] if b.amount > 0]
    other_banks = prop.bank_accounts.filter(is_active=True)
    return render(request, 'accounting/bank.html', {'assoc': prop, 'bank': acct, 's': s, 'deposits_url': _go('deposits', prop.pk, acct.pk, f'{first:%Y-%m}'), 'dep_n': len(deposits_open), 'dep_total': sum((b.amount for b in deposits_open), ZERO), 'other_banks': other_banks, 'accounts': prop.accounts.filter(is_active=True).order_by('code'), 'units': owners.active_units(prop),
                                                    'acc_back': _go('close', prop.pk, f'{first:%Y-%m}'), 'acc_back_label': f'{prop.name} close', **month_context(first)})


# ---------------------------------------------------------------- sorting the deposits

def _payload(request):
    try:
        data = json.loads(request.POST.get('payload') or '')
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise bankrec.StatementError('Something went wrong with the form. Refresh the page and try again.')
    return data


def _owner_rows(prop, rows):
    units = {u.pk: u for u in Unit.objects.filter(property=prop)}
    out = []
    for r in rows or []:
        try:
            unit = units.get(int(r.get('unit')))
        except (TypeError, ValueError, AttributeError):
            unit = None
        if unit is None:
            raise bankrec.StatementError('One of those units is not part of this association.')
        out.append({'unit': unit, 'owner': owners.billed_owner(unit), 'amount': parse_money(str(r.get('amount') or '')), 'ref': str(r.get('ref') or '')})
    return out


def _other_rows(prop, rows):
    accounts = {a.pk: a for a in Account.objects.filter(association=prop)}
    out = []
    for r in rows or []:
        try:
            account = accounts.get(int(r.get('account')))
        except (TypeError, ValueError, AttributeError):
            account = None
        if account is None:
            raise bankrec.StatementError('Choose an account for each item that is not an owner payment.')
        out.append({'account': account, 'amount': parse_money(str(r.get('amount') or '')), 'memo': str(r.get('memo') or '')})
    return out


def _deposit_people(prop):
    """Every unit with its billed owner, what is owed now, its usual monthly charge and what the owner has paid before: what the sorting screen suggests from."""
    owed, monthly = defaultdict(lambda: ZERO), {}
    for c in Charge.objects.filter(association=prop, is_void=False).prefetch_related('applications'):
        owed[(c.unit_id, c.owner_id)] += c.balance
        if c.kind == Charge.Kind.ASSESSMENT and (c.unit_id not in monthly or c.due_date >= monthly[c.unit_id][0]):
            monthly[c.unit_id] = (c.due_date, c.amount)
    earlier = defaultdict(list)
    for r in Receipt.objects.filter(association=prop, is_void=False).order_by('-date', '-id'):
        if len(earlier[r.unit_id]) < 3 and r.amount not in earlier[r.unit_id]:
            earlier[r.unit_id].append(r.amount)
    people = []
    for u in owners.active_units(prop):
        o = owners.billed_owner(u)
        people.append({'id': u.pk, 'label': u.label, 'owner': o.name if o else '', 'owes': str(owed.get((u.pk, o.pk), ZERO)) if o else '0', 'monthly': str(monthly[u.pk][1]) if u.pk in monthly else '0',
                       'earlier': [str(a) for a in earlier.get(u.pk, [])]})
    return people


def _deposit_accounts(prop):
    """The whole chart, for the account search, and the few that are most often right (the reserve cash, interest, miscellaneous income, suspense) to show first."""
    rows = list(Account.objects.filter(association=prop, is_active=True).order_by('code'))
    def common(a):
        n = a.name.lower()
        return a.code in (sc.INTEREST_INCOME, sc.INCOME_MISC) or 'suspense' in n or 'uncategorized' in n or ('reserve' in n and 'cash' in n)
    order = {'reserve': 0, 'interest': 1, 'miscellaneous': 2}
    suggest = sorted((a for a in rows if common(a)), key=lambda a: next((v for k, v in order.items() if k in a.name.lower()), 3))
    return [[a.pk, a.code, a.name, a.kind] for a in rows], [a.pk for a in suggest]


@admin_only
def deposits(request, pk, bank_pk, month):
    prop, redirect_to = _books(request, pk)
    if redirect_to:
        return redirect_to
    acct = get_object_or_404(BankAccount, pk=bank_pk, association=prop)
    first = parse_month(month, default_month())
    here = _go('deposits', prop.pk, acct.pk, f'{first:%Y-%m}')
    stmt = bankrec.statement_for(acct, first)
    if request.method == 'POST':
        action = request.POST.get('action')
        target = here
        try:
            if action == 'sort':
                data = _payload(request)
                line = get_object_or_404(BankLine, pk=data.get('bank_line'), statement=stmt) if stmt else None
                if line is None:
                    raise bankrec.StatementError('Upload the statement first.')
                bankrec.sort_deposit(line, _owner_rows(prop, data.get('owners')), _other_rows(prop, data.get('others')), [str(k) for k in (data.get('items') or [])], request.user)
                messages.success(request, f'Sorted the ${line.amount:,.2f} deposit of {line.date:%b} {line.date.day}.')
                nxt = stmt.lines.filter(amount__gt=0, match__isnull=True).order_by('date', 'id').filter(date__gte=line.date).first() or stmt.lines.filter(amount__gt=0, match__isnull=True).order_by('date', 'id').first()
                target = here + (f'?open={nxt.pk}' if nxt else '')
            elif action == 'ahead':
                data = _payload(request)
                day = parse_date(request.POST.get('date'))
                if day is None:
                    raise bankrec.StatementError('Choose the date the payments were received.')
                method = request.POST.get('method') if request.POST.get('method') in dict(Receipt.Method.choices) else Receipt.Method.CHECK
                made = bankrec.record_owner_payments(acct, day, _owner_rows(prop, data.get('owners')), method, request.user)
                later = f' They are dated after {ledger.month_end(first):%B} {ledger.month_end(first).day}, so they wait for the next statement.' if day > ledger.month_end(first) else ' They wait on the deposit screen until the bank deposit arrives.'
                messages.success(request, f'Recorded {len(made)} payment{"s" if len(made) != 1 else ""}.' + later)
        except (bankrec.StatementError, ValueError, ledger.LedgerError, charges.ChargeError) as e:
            messages.error(request, str(e))
        return redirect(target)
    lines = list(stmt.lines.filter(amount__gt=0).select_related('match').order_by('date', 'id')) if stmt else []
    refs = {r.entry_id: r for r in Receipt.objects.filter(association=prop, entry__isnull=False, bank_account=acct)}
    candidates = []
    for it in bankrec.deposit_candidates(acct, first) if stmt else []:
        rec = refs.get(it.obj.entry_id) if it.kind == 'line' else None
        candidates.append({'key': it.key, 'date': f'{it.date:%b} {it.date.day}', 'text': (it.text or '').strip(), 'amount': str(it.amount), 'ref': rec.reference if rec else ''})
    deposits_ = [{'id': b.pk, 'date': f'{b.date:%b} {b.date.day}', 'text': b.description, 'amount': str(b.amount), 'done': b.match_id is not None, 'how': b.match.how if b.match_id else ''} for b in lines]
    accounts, suggest = _deposit_accounts(prop)
    open_id = request.GET.get('open')
    if not (open_id and open_id.isdigit() and any(d['id'] == int(open_id) for d in deposits_)):
        open_id = next((d['id'] for d in deposits_ if not d['done']), None)
    return render(request, 'accounting/deposits.html', {
        'assoc': prop, 'bank': acct, 'stmt': stmt, 'month': first, 'deposits': deposits_, 'open_id': int(open_id) if open_id else 0, 'units': _deposit_people(prop) if lines else [], 'accounts': accounts, 'suggest': suggest,
        'candidates': candidates, 'open_n': sum(1 for d in deposits_ if not d['done']), 'acc_back': _go('bank', prop.pk, acct.pk, f'{first:%Y-%m}'), 'acc_back_label': 'Bank reconciliation',
        'today': min(timezone.localdate(), ledger.month_end(first)), 'methods': [(v, l) for v, l in Receipt.Method.choices if v in ('check', 'online', 'ach', 'card', 'cash', 'other')]})


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
                                                     'owed': payables.aging(prop, timezone.localdate()), 'box': box, 'inbox_ready': invoice_inbox.is_configured(), 'needs_home': invoice_inbox.SeenEmail.objects.filter(needs_home=True).order_by('-created_at')[:10],
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
                              pay_method=request.POST.get('pay_method') if request.POST.get('pay_method') in ('check', 'ach') else 'check', statement_match=(request.POST.get('statement_match') or '').strip()[:80],
                              post_on_month_end=bool(request.POST.get('post_on_month_end')))
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
            elif action == 'charges_due':
                done = charges.post_due(prop, timezone.localdate(), request.user)
                n = sum(c for _, _, c in done)
                messages.success(request, f'Posted {n} charge{"s" if n != 1 else ""}.' if n else 'Nothing was due.')
                return redirect(here + '?tab=charges')
            elif action in ('assess_late', 'assess_interest'):
                made = charges.assess_late_fees(prop, timezone.localdate(), request.user) if action == 'assess_late' else charges.assess_interest(prop, timezone.localdate(), request.user)
                what = 'late fee' if action == 'assess_late' else 'interest charge'
                messages.success(request, f'Charged {len(made)} {what}{"s" if len(made) != 1 else ""}: ${sum((c.amount for c in made), ZERO):,.2f}.' if made else f'No {what}s were owed.')
                return redirect(here + '?tab=charges')
            elif action in ('jr_add', 'jr_save'):
                day = int(request.POST.get('day_of_month') or 0)
                if not 1 <= day <= 31:
                    raise ValueError('Give the day of the month the transfer happens (1 to 31; 31 means the last day).')
                if action == 'jr_add':
                    JournalRule.objects.create(association=prop, kind='reserve', name='Reserve contribution', day_of_month=day)
                else:
                    JournalRule.objects.filter(pk=request.POST.get('rule'), association=prop).update(day_of_month=day)
                messages.success(request, 'Saved.')
                return redirect(here + '?tab=entries')
            elif action in ('jr_stop', 'jr_resume'):
                JournalRule.objects.filter(pk=request.POST.get('rule'), association=prop).update(is_active=(action == 'jr_resume'))
                return redirect(here + '?tab=entries')
            elif action == 'jr_post':
                rule = get_object_or_404(JournalRule, pk=request.POST.get('rule'), association=prop)
                month_first = parse_month(request.POST.get('month'), ledger.month_start(timezone.localdate()))
                entry = scheduled.post_contribution(prop, rule, month_first, request.user)
                messages.success(request, f'Posted J-{entry.number}: ${sum(l.credit for l in entry.lines.all()):,.2f} to the reserve funds, dated {entry.date:%b} {entry.date.day}.')
                return redirect(here + '?tab=entries')
        except (ValueError, payables.PayableError, ledger.LedgerError, charges.ChargeError) as e:
            messages.error(request, str(e))
            if action and action.startswith('jr_'):
                return redirect(here + '?tab=entries')
        return redirect(here)
    tab = request.GET.get('tab') if request.GET.get('tab') in ('bills', 'charges', 'entries') else 'bills'
    today = timezone.localdate()
    month = parse_month(request.GET.get('month'), ledger.month_start(today))
    rows = list(RecurringBill.objects.filter(association=prop).select_related('vendor', 'account').order_by('-is_active', 'description'))
    state = {r['rec'].pk: r for r in payables.recurring_status(prop, month)}
    for r in rows:
        st = state.get(r.pk)
        r.this_month = None if not r.is_active or st is None or not st['due'] else ('posted' if st['posted'] else ('waiting' if not st['fixed'] else 'missing'))
    edit = next((r for r in rows if str(r.pk) == request.GET.get('edit')), None)
    rules = list(ChargeRule.objects.filter(association=prop).order_by('is_special', 'name'))
    for rule in rules:
        rule.st = charges.rule_status(rule, today)
    late_rows = [{'unit': c.unit.label, 'owner': c.owner.name, 'what': f'Late fee on {c.description}', 'amount': fee} for c, fee, due in charges._late_fee_rows(prop, today)]
    interest_rows = [{'unit': c.unit.label, 'owner': c.owner.name, 'what': f'Interest on {c.description}, {mf:%B %Y}', 'amount': amt} for c, mf, me, amt in charges._interest_rows(prop, today)]
    jrules = list(JournalRule.objects.filter(association=prop).order_by('name'))
    for jr in jrules:
        jr.st = scheduled.status(prop, jr, month, today)
    needs = [r for r in rows if r.this_month in ('waiting', 'missing')]
    counts = {'bills': len(needs), 'charges': sum(1 for r in rules if r.is_active and r.st['pending']) + (1 if late_rows or interest_rows else 0), 'entries': sum(1 for jr in jrules if jr.is_active and jr.st['state'] in ('due', 'no_budget'))}
    return render(request, 'accounting/recurring.html', {'assoc': prop, 'rows': rows, 'edit': edit, 'vendors': _vendor_names(prop), 'expense_accounts': prop.accounts.filter(kind='expense', is_active=True).order_by('code'),
                                                         'frequencies': RecurringBill.Frequency.choices, 'profile': ledger.profile_for(prop), 'today': today, 'tab': tab, 'rules': rules, 'jrules': jrules, 'counts': counts,
                                                         'late_rows': late_rows, 'interest_rows': interest_rows, 'late_total': sum((r['amount'] for r in late_rows), ZERO), 'interest_total': sum((r['amount'] for r in interest_rows), ZERO),
                                                         'needs': needs, 'acc_back': _go('hub', prop.pk), 'acc_back_label': 'Accounting', **month_context(month)})


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
                transfer = ledger.account(prop, sc.RESERVE_TRANSFER)
                day = parse_date(request.POST.get('date')) or timezone.localdate()
                if request.POST.get('from_bank') and request.POST.get('to_bank'):
                    # any bank account to any other: only a move between an operating and a reserve account touches a reserve fund
                    src = get_object_or_404(BankAccount, pk=request.POST.get('from_bank'), association=prop)
                    dst = get_object_or_404(BankAccount, pk=request.POST.get('to_bank'), association=prop)
                    if src.pk == dst.pk:
                        raise ValueError('Choose two different bank accounts.')
                    lines = [{'account': dst.account, 'debit': amount}, {'account': src.account, 'credit': amount}]
                    memo = f'Transfer from {src.name} to {dst.name}'
                    if src.kind != dst.kind:
                        fund = get_object_or_404(Account, pk=request.POST.get('fund'), association=prop)
                        to_reserves = dst.kind == 'reserve'
                        lines += [{'account': transfer if to_reserves else fund, 'debit': amount}, {'account': fund if to_reserves else transfer, 'credit': amount}]
                        memo += f', {fund.name}'
                else:
                    to_reserves = request.POST.get('direction') != 'to_operating'
                    fund = get_object_or_404(Account, pk=request.POST.get('fund'), association=prop)
                    op = ledger.account(prop, sc.CASH_OPERATING)
                    res = ledger.account(prop, sc.CASH_RESERVE)
                    cash_in, cash_out = (res, op) if to_reserves else (op, res)
                    lines = [{'account': cash_in, 'debit': amount}, {'account': cash_out, 'credit': amount},
                             {'account': transfer if to_reserves else fund, 'debit': amount}, {'account': fund if to_reserves else transfer, 'credit': amount}]
                    memo = ('Transfer to reserves' if to_reserves else 'Transfer from reserves') + f', {fund.name}'
                e = ledger.post_entry(prop, day, lines, memo=memo, source=JournalEntry.Source.TRANSFER, user=request.user)
                messages.success(request, f'Transferred ${amount:,.2f} (J-{e.number}).')
        except (ValueError, ledger.LedgerError) as e:
            messages.error(request, str(e))
        return redirect(here)
    month = parse_month(request.GET.get('month'), ledger.month_start(timezone.localdate()))
    entries = JournalEntry.objects.filter(association=prop, date__gte=month, date__lte=ledger.month_end(month)).prefetch_related('lines__account').order_by('-date', '-number')
    return render(request, 'accounting/journal.html', {'assoc': prop, 'entries': entries, 'accounts': prop.accounts.filter(is_active=True).order_by('code'), 'reserve_funds': prop.accounts.filter(kind='equity', code__gte='3811', code__lte='3899').order_by('code'),
                                                       'banks': prop.bank_accounts.filter(is_active=True), 'today': timezone.localdate(), 'range': range(1, 7), 'acc_back': _go('hub', prop.pk), 'acc_back_label': 'Accounting', **month_context(month)})


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


def _end(request, prop):
    return parse_date(request.GET.get('end')) or ledger.month_end(default_month())


@admin_only
def report_bs(request, pk):
    prop, redirect_to = _books(request, pk)
    if redirect_to:
        return redirect_to
    end, basis = _end(request, prop), _basis(request)
    view = request.GET.get('view') if request.GET.get('view') in ('compare', 'year', 'months') else 'compare'
    st = reporting.balance_sheet(prop, end, basis, view)
    return render(request, 'accounting/report_bs.html', {'assoc': prop, 'st': st, 'basis': basis, 'end': end, 'view': view, 'acc_back': _go('reports', prop.pk), 'acc_back_label': 'Reports',
                                                         'views': [('compare', 'vs last month'), ('year', 'vs last year'), ('months', 'Month by month')]})


@admin_only
def report_pl(request, pk):
    prop, redirect_to = _books(request, pk)
    if redirect_to:
        return redirect_to
    end, basis = _end(request, prop), _basis(request)
    view = request.GET.get('view') if request.GET.get('view') in ('compare', 'months') else 'compare'
    st = reporting.profit_and_loss(prop, end, basis, view)
    return render(request, 'accounting/report_pl.html', {'assoc': prop, 'st': st, 'basis': basis, 'end': end, 'view': view, 'acc_back': _go('reports', prop.pk), 'acc_back_label': 'Reports',
                                                         'views': [('compare', 'This month, last month, year to date'), ('months', 'Month by month')]})


@admin_only
def report_bva(request, pk):
    prop, redirect_to = _books(request, pk)
    if redirect_to:
        return redirect_to
    end, basis = _end(request, prop), _basis(request)
    st = reporting.budget_vs_actual(prop, end, basis)
    return render(request, 'accounting/report_bva.html', {'assoc': prop, 'st': st, 'basis': basis, 'end': end, 'acc_back': _go('reports', prop.pk), 'acc_back_label': 'Reports'})


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
    totals = charges.totals(charges.accounts(prop))
    bal = ledger.balances_as_of(prop, end, JournalEntry.Basis.ACCRUAL)
    gl_net = bal.get(ledger.account(prop, sc.AR).pk, ZERO) + bal.get(ledger.account(prop, sc.PREPAID_FEES).pk, ZERO)   # receivables less paid-ahead, as the books hold them
    gap = gl_net - totals['net']
    return render(request, 'accounting/report_aging.html', {'assoc': prop, 'ag': charges.aging(prop, end), 'end': end, 'totals': totals, 'gl_net': gl_net, 'gap': gap,
                                                            'acc_back': _go('reports', prop.pk), 'acc_back_label': 'Reports'})


@admin_only
def report_payables(request, pk):
    prop, redirect_to = _books(request, pk)
    if redirect_to:
        return redirect_to
    end = parse_date(request.GET.get('end')) or timezone.localdate()
    return render(request, 'accounting/report_payables.html', {'assoc': prop, 'ag': payables.aging(prop, end), 'end': end, 'acc_back': _go('reports', prop.pk), 'acc_back_label': 'Reports'})


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
