"""The chart of accounts: one association's (see every account, add one, rename one, renumber an unused one, turn one off) and the master chart every new association starts from.
Accounts are never deleted (the books keep their history). An account's number and kind can be changed only while nothing has been posted to it."""
import re

from django.contrib import messages
from django.db import transaction
from django.db.models import Count
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from core.models import Property

from . import ledger
from . import standard_chart as sc
from .models import Account, MasterAccount
from .web import admin_only, association_or_404, books_or_redirect

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


def _rows(prop):
    used = dict(Account.objects.filter(association=prop).annotate(n=Count('lines')).values_list('pk', 'n'))
    balances = ledger.balances_as_of(prop, timezone.localdate())
    out = []
    for kind in KIND_ORDER:
        accts = [a for a in Account.objects.filter(association=prop, kind=kind).order_by('code')]
        out.append({'kind': kind, 'label': Account.Kind(kind).label, 'accounts': [
            {'a': a, 'entries': used.get(a.pk, 0), 'balance': (balances.get(a.pk) or 0) * (1 if a.normal_debit else -1), 'system': a.code in SYSTEM_CODES,
             'editable': a.code not in SYSTEM_CODES and not used.get(a.pk, 0)} for a in accts]})
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
                code, name, kind = _clean_code(request.POST.get('code')), _clean_name(request.POST.get('name')), _clean_kind(request.POST.get('kind'))
                if Account.objects.filter(association=prop, code=code).exists():
                    raise ValueError(f'{code} is already an account here ({Account.objects.get(association=prop, code=code).name}). Pick another number.')
                with transaction.atomic():
                    Account.objects.create(association=prop, code=code, name=name, kind=kind)
                    also = ''
                    if request.POST.get('to_master'):
                        if MasterAccount.objects.filter(code=code).exists():
                            also = f' {code} is already on the master chart, so that was left alone.'
                        else:
                            MasterAccount.objects.create(code=code, name=name, kind=kind)
                            also = ' It is on the master chart too, so new associations start with it.'
                messages.success(request, f'{code} {name} is added.{_mismatch_note(code, kind)}{also}')
            elif action == 'rename':
                acct = Account.objects.get(association=prop, pk=request.POST.get('account'))
                acct.name = _clean_name(request.POST.get('name'))
                acct.save(update_fields=['name'])
                messages.success(request, f'{acct.code} is now called {acct.name}.')
            elif action == 'edit':               # the number or the kind, only while nothing has been posted to the account
                acct = Account.objects.get(association=prop, pk=request.POST.get('account'))
                if acct.code in SYSTEM_CODES:
                    raise ValueError(f'{acct.code} {acct.name} is one the program posts to itself, so its number stays.')
                if acct.lines.exists():
                    raise ValueError(f'{acct.code} {acct.name} has entries, so its number and kind are fixed. You can still rename it.')
                code = _clean_code(request.POST.get('code')) if 'code' in request.POST else acct.code
                kind = _clean_kind(request.POST.get('kind')) if 'kind' in request.POST else acct.kind
                if code != acct.code and Account.objects.filter(association=prop, code=code).exists():
                    raise ValueError(f'{code} is already an account here ({Account.objects.get(association=prop, code=code).name}). Pick another number.')
                old = acct.code
                acct.code, acct.kind = code, kind
                acct.save(update_fields=['code', 'kind'])
                messages.success(request, f'{old} {acct.name} is now {code} ({Account.Kind(kind).label.lower()}).{_mismatch_note(code, kind)}')
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
    return render(request, 'accounting/chart.html', {'assoc': prop, 'groups': _rows(prop), 'kinds': Account.Kind.choices, 'reserve_note': RESERVE_NOTE,
                                                     'acc_back': reverse('accounting:hub', args=[pk]), 'acc_back_label': 'Accounting'})


# ---------------------------------------------------------------- the master chart

def _books():
    """The associations that have books (accounts)."""
    ids = set(Account.objects.values_list('association_id', flat=True))
    return list(Property.objects.filter(pk__in=ids).order_by('name'))


def _master_rows():
    stats = {}
    for a in Account.objects.annotate(n=Count('lines')).values('code', 'name', 'n'):
        s = stats.setdefault(a['code'], {'assocs': 0, 'entries': 0, 'names': []})
        s['assocs'] += 1
        s['entries'] += 1 if a['n'] else 0
        s['names'].append(a['name'])
    out = []
    for kind in KIND_ORDER:
        rows = []
        for m in MasterAccount.objects.filter(kind=kind).order_by('code'):
            s = stats.get(m.code, {'assocs': 0, 'entries': 0, 'names': []})
            rows.append({'m': m, 'assocs': s['assocs'], 'entries': s['entries'], 'differs': sum(1 for n in s['names'] if n != m.name),
                         'system': m.code in SYSTEM_CODES, 'editable': m.code not in SYSTEM_CODES and not s['entries']})
        out.append({'kind': kind, 'label': Account.Kind(kind).label, 'rows': rows})
    return out


def _push(code, name, kind):
    """Give every association with books this account if it has none with that number. Returns (added, [associations that use the number for something else])."""
    added, clash = 0, []
    have = {a.association_id: a for a in Account.objects.filter(code=code)}
    for prop in _books():
        a = have.get(prop.pk)
        if a is None:
            Account.objects.create(association=prop, code=code, name=name, kind=kind)
            added += 1
        elif a.name != name:
            clash.append(f'{prop.name} (has it as {a.name})')
    return added, clash


@admin_only
def master(request):
    here = reverse('accounting:master_chart')
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'add':
                code, name, kind = _clean_code(request.POST.get('code')), _clean_name(request.POST.get('name')), _clean_kind(request.POST.get('kind'))
                if MasterAccount.objects.filter(code=code).exists():
                    raise ValueError(f'{code} is already on the master chart ({MasterAccount.objects.get(code=code).name}). Pick another number.')
                with transaction.atomic():
                    MasterAccount.objects.create(code=code, name=name, kind=kind)
                    note = ''
                    if request.POST.get('everywhere'):
                        added, clash = _push(code, name, kind)
                        note = f' Added to {added} association{"s" if added != 1 else ""}.'
                        if clash:
                            note += ' Not added where the number is already used for something else: ' + '; '.join(clash) + '.'
                messages.success(request, f'{code} {name} is on the master chart.{_mismatch_note(code, kind)}{note}')
            elif action == 'rename':
                m = MasterAccount.objects.get(pk=request.POST.get('account'))
                m.name = _clean_name(request.POST.get('name'))
                m.save(update_fields=['name'])
                messages.success(request, f'{m.code} is now called {m.name} on the master chart. Associations keep their own names unless you update them.')
            elif action == 'sync':               # make every association's name match the master
                m = MasterAccount.objects.get(pk=request.POST.get('account'))
                n = Account.objects.filter(code=m.code).exclude(name=m.name).update(name=m.name)
                messages.success(request, f'{m.code} is now called {m.name} in {n} association{"s" if n != 1 else ""}.')
            elif action == 'edit':
                m = MasterAccount.objects.get(pk=request.POST.get('account'))
                if m.code in SYSTEM_CODES:
                    raise ValueError(f'{m.code} {m.name} is one the program posts to itself, so its number stays.')
                if Account.objects.filter(code=m.code, lines__isnull=False).exists():
                    raise ValueError(f'{m.code} {m.name} has entries in an association, so its number and kind are fixed. You can still rename it.')
                code = _clean_code(request.POST.get('code')) if 'code' in request.POST else m.code
                kind = _clean_kind(request.POST.get('kind')) if 'kind' in request.POST else m.kind
                if code != m.code and MasterAccount.objects.filter(code=code).exists():
                    raise ValueError(f'{code} is already on the master chart ({MasterAccount.objects.get(code=code).name}). Pick another number.')
                old = m.code
                with transaction.atomic():
                    skipped = []
                    taken = set(Account.objects.filter(code=code).values_list('association_id', flat=True)) if code != old else set()
                    for a in Account.objects.filter(code=old).select_related('association'):
                        if a.association_id in taken:
                            skipped.append(a.association.name)
                            continue
                        a.code, a.kind = code, kind
                        a.save(update_fields=['code', 'kind'])
                    m.code, m.kind = code, kind
                    m.save(update_fields=['code', 'kind'])
                note = (' Left as it was where the new number is already used: ' + ', '.join(skipped) + '.') if skipped else ''
                messages.success(request, f'{old} {m.name} is now {code} ({Account.Kind(kind).label.lower()}), here and in every association.{_mismatch_note(code, kind)}{note}')
            elif action == 'toggle':
                m = MasterAccount.objects.get(pk=request.POST.get('account'))
                if m.is_active and m.code in SYSTEM_CODES:
                    raise ValueError(f'{m.code} {m.name} is one the program posts to itself, so it stays on.')
                m.is_active = not m.is_active
                m.save(update_fields=['is_active'])
                messages.success(request, f'{m.code} {m.name} is {"on" if m.is_active else "off"} for new associations. Existing ones are not changed.')
        except MasterAccount.DoesNotExist:
            messages.error(request, 'That account is gone. Reload the page.')
        except ValueError as e:
            messages.error(request, str(e))
        return redirect(here)
    return render(request, 'accounting/master_chart.html', {'groups': _master_rows(), 'kinds': Account.Kind.choices, 'books': len(_books()), 'reserve_note': RESERVE_NOTE,
                                                            'acc_back': reverse('ws_financials'), 'acc_back_label': 'Financials'})
