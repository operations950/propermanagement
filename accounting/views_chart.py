"""The chart of accounts for one association: see every account, add one, rename one, turn one off. Accounts are never deleted (the books keep their history), and an account's number and
type are fixed once it exists, so nothing already posted changes meaning."""
import re

from django.contrib import messages
from django.db.models import Count
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from . import ledger
from . import standard_chart as sc
from .models import Account
from .web import admin_only, association_or_404, books_or_redirect

KIND_ORDER = [Account.Kind.ASSET, Account.Kind.LIABILITY, Account.Kind.EQUITY, Account.Kind.INCOME, Account.Kind.EXPENSE]
FIRST_DIGIT = {'1': 'asset', '2': 'liability', '3': 'equity', '4': 'income', '5': 'expense', '6': 'expense', '7': 'expense', '8': 'expense', '9': 'expense'}
SYSTEM_CODES = {v for n, v in vars(sc).items() if n.isupper() and isinstance(v, str) and v.isdigit()}       # the accounts the program posts to itself


def _rows(prop):
    used = dict(Account.objects.filter(association=prop).annotate(n=Count('lines')).values_list('pk', 'n'))
    balances = ledger.balances_as_of(prop, timezone.localdate())
    out = []
    for kind in KIND_ORDER:
        accts = [a for a in Account.objects.filter(association=prop, kind=kind).order_by('code')]
        out.append({'kind': kind, 'label': Account.Kind(kind).label, 'accounts': [
            {'a': a, 'entries': used.get(a.pk, 0), 'balance': (balances.get(a.pk) or 0) * (1 if a.normal_debit else -1), 'system': a.code in SYSTEM_CODES} for a in accts]})
    return out


@admin_only
def chart(request, pk):
    prop = association_or_404(pk)
    redirect_to = books_or_redirect(request, prop)
    if redirect_to:
        return redirect_to
    here = reverse('accounting:chart', args=[pk])
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'add':
                code = (request.POST.get('code') or '').strip()
                name = ' '.join((request.POST.get('name') or '').split())
                kind = request.POST.get('kind')
                if not re.fullmatch(r'\d{4}', code):
                    raise ValueError('The number is four digits, like 6921. Yardi\'s numbers are used so an old report and a new one line up.')
                if not name:
                    raise ValueError('Give the account a name.')
                if len(name) > 120:
                    raise ValueError('That name is too long.')
                if kind not in Account.Kind.values:
                    raise ValueError('Pick what kind of account it is.')
                if Account.objects.filter(association=prop, code=code).exists():
                    raise ValueError(f'{code} is already an account here ({Account.objects.get(association=prop, code=code).name}). Pick another number.')
                Account.objects.create(association=prop, code=code, name=name, kind=kind)
                note = ''
                if FIRST_DIGIT.get(code[0]) != kind:
                    note = f' The number starts with {code[0]}, which is usually {FIRST_DIGIT.get(code[0], "another kind of account")}; it was added as {Account.Kind(kind).label.lower()} as you chose.'
                messages.success(request, f'{code} {name} is added.{note}')
            elif action == 'rename':
                acct = Account.objects.get(association=prop, pk=request.POST.get('account'))
                name = ' '.join((request.POST.get('name') or '').split())
                if not name or len(name) > 120:
                    raise ValueError('Give the account a name (up to 120 letters).')
                acct.name = name
                acct.save(update_fields=['name'])
                messages.success(request, f'{acct.code} is now called {name}.')
            elif action == 'toggle':
                acct = Account.objects.get(association=prop, pk=request.POST.get('account'))
                if acct.is_active:
                    if acct.code in SYSTEM_CODES:
                        raise ValueError(f'{acct.code} {acct.name} is one the program posts to itself, so it stays on.')
                    if ledger.balances_as_of(prop, timezone.localdate()).get(acct.pk):
                        raise ValueError(f'{acct.code} {acct.name} still has a balance. Move it first, then turn the account off.')
                acct.is_active = not acct.is_active
                acct.save(update_fields=['is_active'])
                messages.success(request, f'{acct.code} {acct.name} is {"on" if acct.is_active else "off"}.' + ('' if acct.is_active else ' It no longer shows in pickers; its history stays.'))
        except Account.DoesNotExist:
            messages.error(request, 'That account is gone. Reload the page.')
        except ValueError as e:
            messages.error(request, str(e))
        return redirect(here)
    return render(request, 'accounting/chart.html', {'assoc': prop, 'groups': _rows(prop), 'kinds': Account.Kind.choices, 'acc_back': reverse('accounting:hub', args=[pk]), 'acc_back_label': 'Accounting'})
