"""The chart of accounts. There is one for the whole company: every association uses it, so an account added, renamed, renumbered or turned off here is the same in all of them
(each association still holds its own copy of the accounts, because the ledger posts to those, but they are never edited separately). Accounts are never deleted. A number or kind can
change only while nothing has been posted to the account in any association."""
import re

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Sum
from django.shortcuts import redirect, render
from django.urls import reverse

from core.models import Property

from . import ledger
from . import standard_chart as sc
from .models import Account, JournalLine, MasterAccount
from .web import admin_only

KIND_ORDER = [Account.Kind.ASSET, Account.Kind.LIABILITY, Account.Kind.EQUITY, Account.Kind.INCOME, Account.Kind.EXPENSE]
FIRST_DIGIT = {'1': 'asset', '2': 'liability', '3': 'equity', '4': 'income', '5': 'expense', '6': 'expense', '7': 'expense', '8': 'expense', '9': 'expense'}
SYSTEM_CODES = sc.SYSTEM_CODES       # the accounts the program posts to itself
RESERVE_NOTE = f'Reserve funds are equity accounts numbered {sc.RESERVE_FIRST}-{sc.RESERVE_LAST}; they show as reserve lines in the budget.'


def _clean_code(code):
    code = (code or '').strip()
    if not re.fullmatch(r'\d{4}', code):
        raise ValueError("The number is four digits, like 6921. Yardi's numbers are used so an old report and a new one line up.")
    return code


def _clean_name(name):
    name = ' '.join((name or '').split())
    if not name:
        raise ValueError('Give the account a name.')
    if len(name) > 120:
        raise ValueError('That name is too long.')
    return name


def _clean_kind(kind):
    if kind not in Account.Kind.values:
        raise ValueError('Pick what kind of account it is.')
    return kind


def _mismatch_note(code, kind):
    if FIRST_DIGIT.get(code[0]) == kind:
        return ''
    return f' The number starts with {code[0]}, which is usually {FIRST_DIGIT.get(code[0], "another kind of account")}; it is {Account.Kind(kind).label.lower()} as you chose.'


def _books():
    """The associations that have books (accounts)."""
    ids = set(Account.objects.values_list('association_id', flat=True))
    return list(Property.objects.filter(pk__in=ids).order_by('name'))


def _master_rows():
    stats = {}
    for a in Account.objects.annotate(n=Count('lines')).values('code', 'n'):
        s = stats.setdefault(a['code'], {'assocs': 0, 'entries': 0})
        s['assocs'] += 1
        s['entries'] += 1 if a['n'] else 0
    out = []
    for kind in KIND_ORDER:
        rows = []
        for m in MasterAccount.objects.filter(kind=kind).order_by('code'):
            s = stats.get(m.code, {'assocs': 0, 'entries': 0})
            rows.append({'m': m, 'assocs': s['assocs'], 'entries': s['entries'], 'system': m.code in SYSTEM_CODES, 'editable': m.code not in SYSTEM_CODES and not s['entries']})
        out.append({'kind': kind, 'label': Account.Kind(kind).label, 'rows': rows})
    return out


def _has_balance(code):
    """True if any association has money sitting on this account."""
    for r in JournalLine.objects.filter(account__code=code).values('account').annotate(d=Sum('debit'), c=Sum('credit')):
        if r['d'] != r['c']:
            return True
    return False


@admin_only
def chart(request, pk=None):
    """The old per-association address: there is one chart now."""
    return redirect('accounting:master_chart')


@admin_only
def master(request):
    here = reverse('accounting:master_chart')
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'add':
                code, name, kind = _clean_code(request.POST.get('code')), _clean_name(request.POST.get('name')), _clean_kind(request.POST.get('kind'))
                if MasterAccount.objects.filter(code=code).exists():
                    raise ValueError(f'{code} is already on the chart ({MasterAccount.objects.get(code=code).name}). Pick another number.')
                with transaction.atomic():
                    MasterAccount.objects.create(code=code, name=name, kind=kind)
                    ledger.sync_all_charts()
                messages.success(request, f'{code} {name} is added to every association.{_mismatch_note(code, kind)}')
            elif action == 'rename':
                m = MasterAccount.objects.get(pk=request.POST.get('account'))
                m.name = _clean_name(request.POST.get('name'))
                with transaction.atomic():
                    m.save(update_fields=['name'])
                    Account.objects.filter(code=m.code).update(name=m.name)
                messages.success(request, f'{m.code} is now called {m.name} in every association.')
            elif action == 'edit':               # the number or the kind, only while nothing is posted to it anywhere
                m = MasterAccount.objects.get(pk=request.POST.get('account'))
                if m.code in SYSTEM_CODES:
                    raise ValueError(f'{m.code} {m.name} is one the program posts to itself, so its number stays.')
                if Account.objects.filter(code=m.code, lines__isnull=False).exists():
                    raise ValueError(f'{m.code} {m.name} has entries in an association, so its number and kind are fixed. You can still rename it.')
                code = _clean_code(request.POST.get('code')) if 'code' in request.POST else m.code
                kind = _clean_kind(request.POST.get('kind')) if 'kind' in request.POST else m.kind
                if code != m.code and (MasterAccount.objects.filter(code=code).exists() or Account.objects.filter(code=code).exists()):
                    raise ValueError(f'{code} is already an account ({MasterAccount.objects.filter(code=code).first() or Account.objects.filter(code=code).first()}). Pick another number.')
                old = m.code
                with transaction.atomic():
                    Account.objects.filter(code=old).update(code=code, kind=kind)
                    m.code, m.kind = code, kind
                    m.save(update_fields=['code', 'kind'])
                messages.success(request, f'{old} {m.name} is now {code} ({Account.Kind(kind).label.lower()}) in every association.{_mismatch_note(code, kind)}')
            elif action == 'toggle':
                m = MasterAccount.objects.get(pk=request.POST.get('account'))
                if m.is_active:
                    if m.code in SYSTEM_CODES:
                        raise ValueError(f'{m.code} {m.name} is one the program posts to itself, so it stays on.')
                    if _has_balance(m.code):
                        raise ValueError(f'{m.code} {m.name} still has a balance in an association. Move it first, then turn the account off.')
                m.is_active = not m.is_active
                with transaction.atomic():
                    m.save(update_fields=['is_active'])
                    Account.objects.filter(code=m.code).update(is_active=m.is_active)
                messages.success(request, f'{m.code} {m.name} is {"on" if m.is_active else "off"} in every association.' + ('' if m.is_active else ' It no longer shows in pickers; its history stays.'))
        except MasterAccount.DoesNotExist:
            messages.error(request, 'That account is gone. Reload the page.')
        except ValueError as e:
            messages.error(request, str(e))
        return redirect(here)
    ledger.sync_all_charts()
    return render(request, 'accounting/master_chart.html', {'groups': _master_rows(), 'kinds': Account.Kind.choices, 'books': len(_books()), 'reserve_note': RESERVE_NOTE,
                                                            'acc_back': reverse('ws_financials'), 'acc_back_label': 'Financials'})
