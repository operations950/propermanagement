"""The association's contracts: the list, adding one by hand or from the file, and the official-records list."""
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from . import contracts, documents, ledger
from .models import Account, Contract
from .web import admin_only, association_or_404, books_or_redirect, parse_date, parse_money


def _fields(request, prop):
    """The contract's fields from the form."""
    name = (request.POST.get('name') or '').strip()
    if not name:
        raise ValueError('Say what the contract is for.')
    account = Account.objects.filter(pk=request.POST.get('expense_account'), association=prop).first() if request.POST.get('expense_account') else None
    amount = parse_money(request.POST.get('amount')) if (request.POST.get('amount') or '').strip() else None
    freq = request.POST.get('frequency') if request.POST.get('frequency') in Contract.Frequency.values else 'monthly'
    return dict(name=name[:200], vendor_name=(request.POST.get('vendor_name') or '').strip()[:200], description=(request.POST.get('description') or '').strip(),
                start_date=parse_date(request.POST.get('start_date')), end_date=parse_date(request.POST.get('end_date')), auto_renews=bool(request.POST.get('auto_renews')),
                renewal_months=int(request.POST.get('renewal_months') or 12), notice_days=int(request.POST.get('notice_days') or 0), amount=amount, frequency=freq, expense_account=account,
                board_approved_on=parse_date(request.POST.get('board_approved_on')), visible_to_owners=bool(request.POST.get('visible_to_owners')), notes=(request.POST.get('notes') or '').strip())


@admin_only
def contract_list(request, pk):
    prop = association_or_404(pk)
    redirect_to = books_or_redirect(request, prop)
    if redirect_to:
        return redirect_to
    here = reverse('accounting:contracts', args=[pk])
    today = timezone.localdate()
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'save':
                fields = _fields(request, prop)
                rid = request.POST.get('contract')
                if rid:
                    c = get_object_or_404(Contract, pk=rid, association=prop)
                    for k, v in fields.items():
                        setattr(c, k, v)
                    c.confirmed = True
                    c.save()
                else:
                    c = Contract.objects.create(association=prop, **fields)
                up = request.FILES.get('file')
                if up is not None:
                    c.document = documents.upload(prop, 'contract', up, request.user)
                    c.save(update_fields=['document'])
                if request.POST.get('make_bill') or c.recurring_bill_id:
                    nxt = parse_date(request.POST.get('next_bill_date'))
                    if not c.recurring_bill_id and nxt is None and request.POST.get('make_bill'):
                        raise ValueError('Saved, but give the date of the first bill to make its recurring bill.')
                    contracts.sync_bill(c, nxt)
                made = contracts.ensure_alerts(today)
                messages.success(request, 'Saved.' + (f' {made} alert{"s" if made != 1 else ""} on the compliance screen.' if made else ''))
            elif action == 'read':
                up = request.FILES.get('file')
                if up is None:
                    raise ValueError('Choose the contract file.')
                data = up.read()
                up.seek(0)
                found = contracts.read_contract(data, up.name)
                c = Contract.objects.create(association=prop, confirmed=False, **found)
                c.document = documents.upload(prop, 'contract', up, request.user)
                c.save(update_fields=['document'])
                messages.success(request, 'Read it. Check each field, then save.')
                return redirect(f'{here}?edit={c.pk}')
            elif action in ('stop', 'resume'):
                Contract.objects.filter(pk=request.POST.get('contract'), association=prop).update(is_active=(action == 'resume'))
        except (ValueError, InvalidOperation, documents.DocumentError, ledger.LedgerError) as e:
            messages.error(request, str(e))
        except Exception:
            import logging
            logging.getLogger(__name__).exception('Contract action failed')
            messages.error(request, 'That did not work. Try again.')
        return redirect(here)
    rows = contracts.upcoming(prop, today)
    edit = next((r['c'] for r in rows if str(r['c'].pk) == request.GET.get('edit')), None)
    vendor_text = (edit.vendor.name if edit and edit.vendor_id else (edit.vendor_name if edit else ''))
    return render(request, 'accounting/contracts.html', {'assoc': prop, 'rows': rows, 'edit': edit, 'vendor_text': vendor_text, 'frequencies': Contract.Frequency.choices, 'expense_accounts': prop.accounts.filter(kind='expense', is_active=True).order_by('code'),
                                                         'today': today, 'acc_back': reverse('accounting:hub', args=[pk]), 'acc_back_label': 'Accounting'})


@admin_only
def contract_print(request, pk):
    """The contracts as the official-records list an owner may ask to see."""
    prop = association_or_404(pk)
    rows = [r for r in contracts.upcoming(prop) if r['c'].is_active and r['c'].visible_to_owners]
    return render(request, 'accounting/contracts_print.html', {'assoc': prop, 'rows': rows, 'today': timezone.localdate(), 'acc_back': reverse('accounting:contracts', args=[pk]), 'acc_back_label': 'Contracts'})
