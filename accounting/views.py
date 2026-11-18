"""The association books: the entry points, the close sheet, units and owners, shares and charges. (Money screens are in views_money.py, settings in views_setup.py.)"""
import json
from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.models import Contact, Property, Unit

from . import charges, closing, documents, labels, ledger, owners
from . import standard_chart as sc
from .models import (MonthLock, Account, AssociationProfile, BoardTerm, Charge, ChargeRule, Ownership, OwnerProfile, Receipt, ShareKind, UnitProfile, UnitShare)
from .web import (acc_up, admin_only, association_or_404, books_or_redirect, default_month, month_context, parse_date, parse_money, parse_month)


def _go(name, *args, **query):
    url = reverse(f'accounting:{name}', args=args)
    return url + ('?' + '&'.join(f'{k}={v}' for k, v in query.items()) if query else '')


# ---------------------------------------------------------------- the list and the hub

def _lease_bank(prop, unit):
    """The bank account a commercial unit's lease says its rent is paid into (the form's first choice), or None."""
    if unit is None or prop.property_type != Property.Type.COMMERCIAL:
        return None
    from commercial.models import Lease
    lease = Lease.objects.filter(unit=unit, bank_account__isnull=False).exclude(status='ended').select_related('bank_account').order_by('-start_date').first()
    return lease.bank_account if lease and lease.bank_account.is_active else None


def _close_list(request, commercial):
    month = parse_month(request.GET.get('month'), default_month())
    rows = []
    for prof in closing.associations():
        if labels.is_commercial(prof.property) != commercial:
            continue
        card = closing.association_card(prof.property, month)
        rows.append({'assoc': prof.property, **card})
    kept = {prof.property_id for prof in closing.associations()}         # closing.associations() gives profiles: compare the PROPERTY ids
    others = [] if commercial else [p for p in Property.objects.filter(property_type__in=labels.BOOKS_TYPES, is_active=True, is_general=False).exclude(name__icontains='(general)').order_by('name') if p.pk not in kept and p.property_type != Property.Type.COMMERCIAL]
    return render(request, 'accounting/home.html', {'rows': rows, 'commercial': commercial, 'others': others, 'done': sum(1 for r in rows if r['status'] == 'done'), **month_context(month)})


@admin_only
def home(request):
    """Close associations: every association we keep the books for, and where its month stands."""
    return _close_list(request, False)


@admin_only
def home_commercial(request):
    """Close commercial properties: every commercial building we keep the books for, and where its month stands."""
    return _close_list(request, True)


@admin_only
def hub(request, pk):
    prop = association_or_404(pk)
    redirect_to = books_or_redirect(request, prop)
    if redirect_to:
        return redirect_to
    month = default_month()
    return render(request, 'accounting/hub.html', {'assoc': prop, 'profile': ledger.profile_for(prop), 'month': month, **acc_up(request, prop, reverse('property_detail', args=[prop.pk]), prop.name),
                                                   'banks': prop.bank_accounts.filter(is_active=True)})


@admin_only
def close_review(request, pk, month):
    """The last step before the financials go out: the month's financial package, and nothing else. Approving it closes (locks) the month and records who approved it; the step after it,
    sending the package to the board or owners, is what finishes the month. Opening the month again is here too."""
    prop = association_or_404(pk)
    redirect_to = books_or_redirect(request, prop)
    if redirect_to:
        return redirect_to
    first = parse_month(month, default_month())
    here = reverse('accounting:close_review', args=[pk, f'{first:%Y-%m}'])
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'approve':
                sheet = closing.close_sheet(prop, first)
                if sheet['locked']:
                    raise ledger.LedgerError(f'{first:%B %Y} is already closed.')
                if not sheet['can_lock']:
                    raise ledger.LedgerError('Finish the items on the close sheet first: some still need doing.')
                ledger.lock_month(prop, first, request.user)
                messages.success(request, f'{first:%B %Y} is approved and closed.')
            elif action == 'reopen':
                ledger.unlock_month(prop, first)
                messages.success(request, f'{first:%B %Y} is open again.')
        except ledger.LedgerError as e:
            messages.error(request, str(e))
        return redirect(here)
    sheet = closing.close_sheet(prop, first)
    lock = MonthLock.objects.filter(association=prop, month=first).select_related('locked_by').first()
    pdf = reverse('accounting:report_package', args=[pk]) + f'?month={first:%Y-%m}'
    waiting = [i for i in sheet['items'] if i['blocks'] and i['status'] != 'done' and i['key'] != 'lock']       # this screen IS the last step: it is never waiting on itself
    return render(request, 'accounting/close_review.html', {
        'assoc': prop, 'month': first, 'sheet': sheet, 'lock': lock, 'pdf': pdf, 'waiting': waiting, 'sheet_url': reverse('accounting:close', args=[pk, f'{first:%Y-%m}']),
        'send_url': reverse('accounting:send_board', args=[pk, f'{first:%Y-%m}']), 'send_label': labels.label(prop, 'package_step'),
        **acc_up(request, prop, reverse('accounting:close', args=[pk, f'{first:%Y-%m}']), 'Month-end close')})


@admin_only
def close_sheet(request, pk, month):
    prop = association_or_404(pk)
    redirect_to = books_or_redirect(request, prop)
    if redirect_to:
        return redirect_to
    first = parse_month(month, default_month())
    here = _go('close', pk, f'{first:%Y-%m}')
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'lock':
                sheet = closing.close_sheet(prop, first)
                if not sheet['can_lock']:
                    raise ledger.LedgerError('Finish the items above first.')
                ledger.lock_month(prop, first, request.user)
                messages.success(request, f'{first:%B %Y} is closed.')
            elif action == 'unlock':
                ledger.unlock_month(prop, first)
                messages.success(request, f'{first:%B %Y} is open again.')
        except (ledger.LedgerError, charges.ChargeError) as e:
            messages.error(request, str(e))
        return redirect(here)
    sheet = closing.close_sheet(prop, first)
    return render(request, 'accounting/close_sheet.html', {'assoc': prop, 'sheet': sheet, 'profile': ledger.profile_for(prop), **acc_up(request, prop, _go('home') + f'?month={first:%Y-%m}', labels.label(prop, 'close_steps')),
                                                           **month_context(first)})


# ---------------------------------------------------------------- units and owners

def _unit_rows(prop):
    by_unit = {}
    for r in charges.accounts(prop):
        by_unit.setdefault(r['unit'].pk, []).append(r)
    rows = []
    for unit in owners.active_units(prop):
        own = owners.current_ownerships(unit)
        accounts = by_unit.get(unit.pk, [])
        balance = sum((a['balance'] for a in accounts), Decimal('0.00'))
        rows.append({'unit': unit, 'ownerships': own, 'balance': balance, 'away': any(o.occupancy == 'absentee' for o in own),
                     'state': 'owes' if balance > 0 else 'ahead' if balance < 0 else 'zero', 'building': unit.assoc_profile.building if hasattr(unit, 'assoc_profile') else ''})
    return rows


@admin_only
def units(request, pk):
    prop = association_or_404(pk)
    rows = _unit_rows(prop)
    totals = charges.totals(charges.accounts(prop))
    former = [r for r in charges.accounts(prop) if not r['current'] and r['balance'] != 0]
    return render(request, 'accounting/units.html', {'assoc': prop, 'rows': rows, 'totals': totals, 'former': former, 'buildings': sorted({r['building'] for r in rows if r['building']}),
                                                     'counts': {'owes': sum(1 for r in rows if r['state'] == 'owes'), 'ahead': sum(1 for r in rows if r['state'] == 'ahead'), 'away': sum(1 for r in rows if r['away'])},
                                                     'books_on': ledger.profile_for(prop).we_do_financials, **acc_up(request, prop, reverse('property_detail', args=[prop.pk]), prop.name)})


@admin_only
def units_import(request, pk):
    prop = association_or_404(pk)
    key = f'acc_owner_import_{prop.pk}'
    plan = rows = None
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'read':
            f = request.FILES.get('file')
            if f is None:
                messages.error(request, 'Choose the owners file.')
            else:
                try:
                    rows = owners.read_owner_rows(f.name, f.read())
                    request.session[key] = [dict(r, since=r['since'].isoformat() if r['since'] else None) for r in rows]
                    plan = owners.plan_import(prop, rows)
                except owners.OwnerError as e:
                    messages.error(request, str(e))
        elif action == 'apply':
            stored = request.session.get(key)
            if not stored:
                messages.error(request, 'Upload the file again: the import was not kept.')
            else:
                rows = [dict(r, since=date.fromisoformat(r['since']) if r['since'] else None) for r in stored]
                billed = {k[len('billed_'):]: v for k, v in request.POST.items() if k.startswith('billed_') and v}
                made = owners.apply_import(prop, rows, billed=billed)
                request.session.pop(key, None)
                messages.success(request, f'Imported: {made["units"]} new units, {made["owners"]} new owners, {made["ownerships"]} ownerships.')
                return redirect(_go('units', prop.pk))
    if plan is not None:
        plan['units_list'] = {u: o for u, o in plan['multi_owner_units'].items()}
    return render(request, 'accounting/units_import.html', {'assoc': prop, 'plan': plan, **acc_up(request, prop, _go('units', prop.pk), labels.label(prop, 'units_title'))})


@admin_only
def unit_detail(request, pk, unit_pk):
    prop = association_or_404(pk)
    unit = get_object_or_404(Unit, pk=unit_pk, property=prop)
    here = _go('unit', prop.pk, unit.pk)
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'add_owner':
                name = ' '.join((request.POST.get('name') or '').split())
                if not name:
                    raise owners.OwnerError('Type the owner\'s name.')
                person = owners.person_for_name(prop, name)
                owners.add_owner(unit, person, start=parse_date(request.POST.get('start')), occupancy=request.POST.get('occupancy') or 'occupant', billed=bool(request.POST.get('billed')) or None)
                messages.success(request, f'{person.name} added.')
            elif action == 'set_billed':
                owners.set_billed(unit, get_object_or_404(Contact, pk=request.POST.get('person')))
                messages.success(request, 'The bill now goes to that owner.')
            elif action == 'end_ownership':
                own = get_object_or_404(Ownership, pk=request.POST.get('ownership'), unit=unit)
                owners.end_ownership(own, parse_date(request.POST.get('end')) or timezone.localdate())
                messages.success(request, f'{own.person.name} is no longer an owner.')
            elif action == 'sale':
                names = [n for n in (request.POST.get('names') or '').replace('\n', ';').split(';') if n.strip()]
                sale_date = parse_date(request.POST.get('sale_date'))
                if not names or sale_date is None:
                    raise owners.OwnerError('Give the sale date and at least one new owner.')
                people = [(owners.person_for_name(prop, n), request.POST.get('occupancy') or 'occupant') for n in names]
                owners.record_sale(unit, sale_date, people)
                messages.success(request, f'The sale is recorded. Charges from {sale_date:%b} {sale_date.day} go to {people[0][0].name}.')
            elif action == 'void_charge':
                charges.void_charge(get_object_or_404(Charge, pk=request.POST.get('charge'), unit=unit), request.user)
                messages.success(request, 'The charge was taken back.')
        except (owners.OwnerError, charges.ChargeError, ledger.LedgerError) as e:
            messages.error(request, str(e))
        return redirect(here)
    accounts = [a for a in charges.accounts(prop) if a['unit'].pk == unit.pk]
    own = list(unit.ownerships.select_related('person').order_by('end_date', '-is_billed', 'start_date'))
    from tickets.models import Ticket
    open_tickets = Ticket.objects.filter(property=prop, unit=unit).exclude(status__in=['completed', 'verified', 'skipped', 'not_applicable', 'cancelled']).count() if hasattr(Ticket, 'unit') else 0
    return render(request, 'accounting/unit_detail.html', {'assoc': prop, 'unit': unit, 'building': unit.assoc_profile.building if hasattr(unit, 'assoc_profile') else '', 'current': [o for o in own if o.is_current],
                                                           'former': [o for o in own if not o.is_current], 'accounts': accounts, 'balance': sum((a['balance'] for a in accounts), Decimal('0.00')), 'loan': sum((a['loan'] for a in accounts), Decimal('0.00')), 'loan_rows': charges.loan_rows(unit),
                                                           'rows': list(reversed(charges.ledger_rows(unit))), 'share': UnitShare.objects.filter(unit=unit, kind__is_default=True).first(),
                                                           'open_tickets': open_tickets, 'books_on': ledger.profile_for(prop).we_do_financials, 'today': timezone.localdate(), 'leases': list(unit.leases.select_related('tenant')) if labels.is_commercial(prop) else [],
                                                           **acc_up(request, prop, _go('units', prop.pk), labels.label(prop, 'units_title'))})


@admin_only
def owner_detail(request, pk, contact_pk):
    prop = association_or_404(pk)
    person = get_object_or_404(Contact, pk=contact_pk)
    profile, _ = OwnerProfile.objects.get_or_create(contact=person)
    here = _go('owner', prop.pk, person.pk)
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'save':
            person.phone = (request.POST.get('phone') or '').strip()[:30]
            person.email = (request.POST.get('email') or '').strip()
            person.save(update_fields=['phone', 'email'])
            for f in ('legal_name', 'mailing_address', 'seasonal_address', 'phone_alt', 'email_alt', 'language', 'emergency_contact'):
                setattr(profile, f, (request.POST.get(f) or '').strip())
            profile.statements_by = request.POST.get('statements_by') if request.POST.get('statements_by') in ('mail', 'email', 'both') else 'mail'
            for f in ('ok_to_text', 'is_voter', 'emergency_assist', 'is_company'):
                setattr(profile, f, bool(request.POST.get(f)))
            profile.seasonal_from, profile.seasonal_to = parse_date(request.POST.get('seasonal_from')), parse_date(request.POST.get('seasonal_to'))
            profile.save()
            messages.success(request, 'Saved.')
        elif action == 'add_term':
            position = (request.POST.get('position') or '').strip()
            if not position:
                messages.error(request, 'Type the position.')
            else:
                owners.add_board_term(prop, person, position, start=parse_date(request.POST.get('start')), end=None)
                messages.success(request, 'Board term added.')
        elif action == 'end_term':
            term = get_object_or_404(BoardTerm, pk=request.POST.get('term'), person=person)
            term.end_date = parse_date(request.POST.get('end')) or timezone.localdate()
            term.save(update_fields=['end_date'])
            messages.success(request, 'Board term ended.')
        return redirect(here)
    own = list(person.ownerships.filter(unit__property=prop).select_related('unit').order_by('end_date', 'unit__label'))
    accts = {a['unit'].pk: a for a in charges.accounts(prop) if a['owner'].pk == person.pk}
    for o in own:
        o.account = accts.get(o.unit_id)
    return render(request, 'accounting/owner_detail.html', {'assoc': prop, 'person': person, 'profile': profile, 'ownerships': own, 'terms': person.board_terms.filter(association=prop),
                                                            'balance': sum((a['balance'] for a in accts.values()), Decimal('0.00')), **acc_up(request, prop, _go('units', prop.pk), labels.label(prop, 'units_title'))})


# ---------------------------------------------------------------- shares

@admin_only
def shares(request, pk):
    prop = association_or_404(pk)
    kind = owners.default_share_kind(prop)
    here = _go('shares', prop.pk)
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'equal':
            n = owners.set_equal_shares(prop, kind)
            messages.success(request, f'Every one of the {n} units now has an equal share.')
        elif action == 'save':
            changed = 0
            for u in owners.active_units(prop):
                raw = request.POST.get(f'share_{u.pk}')
                if raw is None or raw.strip() == '':
                    continue
                try:
                    pct = Decimal(raw.replace('%', '').strip())
                except Exception:
                    messages.error(request, f'"{raw}" is not a percentage (unit {u.label}).')
                    return redirect(here)
                UnitShare.objects.update_or_create(unit=u, kind=kind, defaults={'percent': pct})
                changed += 1
            messages.success(request, 'Shares saved.')
        return redirect(here)
    rows = owners.shares(prop, kind)
    total = sum((p for u, p in rows), Decimal('0'))
    rule = ChargeRule.objects.filter(association=prop, is_active=True).first()
    per = None
    if rule:
        per = dict(owners.split_by_share(rule.amount if rule.method == 'share' else rule.amount * len(rows), rows)) if total > 0 else None
    return render(request, 'accounting/shares.html', {'assoc': prop, 'kind': kind, 'rows': rows, 'total': total, 'ok': total == Decimal('100'), 'rule': rule, 'per': per,
                                                      **acc_up(request, prop, _go('charges', prop.pk), labels.label(prop, 'charges_title'))})


# ---------------------------------------------------------------- charges to owners

@admin_only
def charges_list(request, pk):
    prop = association_or_404(pk)
    redirect_to = books_or_redirect(request, prop)
    if redirect_to:
        return redirect_to
    today = timezone.localdate()
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'post_now':
                rule = get_object_or_404(ChargeRule, pk=request.POST.get('rule'), association=prop)
                n = sum(charges.post_rule(rule, d, request.user) for d in charges.rule_due_dates(rule, today))
                messages.success(request, f'{n} charge{"s" if n != 1 else ""} posted.' if n else 'Nothing more was due.')
            elif action == 'late_fees':
                made = charges.assess_late_fees(prop, today, request.user)
                messages.success(request, f'{len(made)} late fee{"s" if len(made) != 1 else ""} charged.' if made else 'No late fees were owed.')
            elif action == 'stop':
                ChargeRule.objects.filter(pk=request.POST.get('rule'), association=prop).update(is_active=False)
                messages.success(request, 'That charge will no longer be posted.')
            elif action == 'resume':
                ChargeRule.objects.filter(pk=request.POST.get('rule'), association=prop).update(is_active=True)
        except (charges.ChargeError, ledger.LedgerError, owners.OwnerError) as e:
            messages.error(request, str(e))
        return redirect(_go('charges', prop.pk))
    rows = []
    n_units = len(owners.active_units(prop))
    for rule in ChargeRule.objects.filter(association=prop).select_related('income_account'):
        st = charges.rule_status(rule, today)
        per_unit = rule.amount if rule.method == 'flat' else None
        total = rule.amount * n_units if rule.method == 'flat' else rule.amount
        rows.append({'rule': rule, 'status': st, 'per_unit': per_unit, 'total': total})
    return render(request, 'accounting/charges.html', {'assoc': prop, 'rows': rows, 'profile': ledger.profile_for(prop), 'units_count': n_units, **acc_up(request, prop, _go('hub', prop.pk), 'Accounting')})


@admin_only
def charge_form(request, pk, rule_pk=None):
    prop = association_or_404(pk)
    redirect_to = books_or_redirect(request, prop)
    if redirect_to:
        return redirect_to
    rule = get_object_or_404(ChargeRule, pk=rule_pk, association=prop) if rule_pk else None
    income_accounts = prop.accounts.filter(kind='income', is_active=True).order_by('code')
    kind = owners.default_share_kind(prop)
    if request.method == 'POST':
        try:
            name = (request.POST.get('name') or '').strip()
            if not name:
                raise ValueError('Give the charge a name.')
            first_due = parse_date(request.POST.get('first_due'))
            if first_due is None:
                raise ValueError('Give the first due date.')
            method = 'share' if request.POST.get('method') == 'share' else 'flat'
            amount = parse_money(request.POST.get('amount'))
            if amount <= 0:
                raise ValueError('The amount must be above zero.')
            freq = request.POST.get('frequency') or 'quarterly'
            if freq not in dict(ChargeRule.Frequency.choices):
                raise ValueError('Choose how often.')
            account = get_object_or_404(Account, pk=request.POST.get('income_account'), association=prop)
            installments = int(request.POST['installments']) if (request.POST.get('installments') or '').strip() else None
            ahead = int(request.POST.get('post_days_ahead') or 0)
            if not 0 <= ahead <= 60:
                raise ValueError('Post the charges between 0 and 60 days early.')
            fields = dict(name=name, is_special=bool(request.POST.get('is_special')), frequency=freq, first_due=first_due, method=method, amount=amount,
                          share_kind=kind if method == 'share' else None, income_account=account, installments=installments, post_days_ahead=ahead)
            if rule is None:
                rule = ChargeRule.objects.create(association=prop, **fields)
            else:
                for k, v in fields.items():
                    setattr(rule, k, v)
                rule.save()
            messages.success(request, 'Saved.')
            if request.POST.get('post_now'):
                n = sum(charges.post_rule(rule, d, request.user) for d in charges.rule_due_dates(rule, timezone.localdate()))
                messages.success(request, f'{n} charge{"s" if n != 1 else ""} posted.' if n else 'Nothing was due yet.')
            return redirect(_go('charges', prop.pk))
        except (ValueError, ledger.LedgerError, charges.ChargeError, owners.OwnerError) as e:
            messages.error(request, str(e))
    units_n = len(owners.active_units(prop))
    share_rows = [(u.label, p) for u, p in owners.shares(prop, kind)[:5]]
    default_income = prop.accounts.filter(code=sc.INCOME_ASSESSMENT).first()
    return render(request, 'accounting/charge_form.html', {'assoc': prop, 'rule': rule, 'income_accounts': income_accounts, 'default_income': default_income, 'units_n': units_n, 'share_rows': share_rows,
                                                           'frequencies': ChargeRule.Frequency.choices, 'today': timezone.localdate(), **acc_up(request, prop, _go('charges', prop.pk), labels.label(prop, 'charges_title'))})


@admin_only
def charge_one(request, pk):
    """A charge to one unit: a fine, a key fob, an application or estoppel fee."""
    prop = association_or_404(pk)
    redirect_to = books_or_redirect(request, prop)
    if redirect_to:
        return redirect_to
    unit = Unit.objects.filter(pk=request.GET.get('unit') or request.POST.get('unit'), property=prop).first()
    if request.method == 'POST':
        try:
            if unit is None:
                raise ValueError('Choose the unit.')
            owner = get_object_or_404(Contact, pk=request.POST.get('owner')) if request.POST.get('owner') else owners.billed_owner(unit)
            if owner is None:
                raise ValueError('That unit has no owner to charge.')
            kind = request.POST.get('kind') if request.POST.get('kind') in ('fine', 'other') else 'other'
            desc = (request.POST.get('description') or '').strip()
            if not desc:
                raise ValueError('Say what the charge is for.')
            account = prop.accounts.filter(pk=request.POST.get('income_account'), kind='income').first()
            charges.post_charge(prop, unit, owner, kind, desc, parse_date(request.POST.get('due')) or timezone.localdate(), parse_money(request.POST.get('amount')), income_account=account, user=request.user)
            messages.success(request, 'Charged.')
            return redirect(_go('unit', prop.pk, unit.pk))
        except (ValueError, ledger.LedgerError, charges.ChargeError) as e:
            messages.error(request, str(e))
    return render(request, 'accounting/charge_one.html', {'assoc': prop, 'unit': unit, 'units': owners.active_units(prop), 'income_accounts': prop.accounts.filter(kind='income', is_active=True).order_by('code'),
                                                          'today': timezone.localdate(), **acc_up(request, prop, _go('unit', prop.pk, unit.pk) if unit else _go('hub', prop.pk), 'Back')})


@admin_only
def receipt_pick(request):
    """Record an owner's payment: first choose the association (only the ones whose books we keep), then the usual payment form opens."""
    from .models import AssociationProfile
    profiles = AssociationProfile.objects.filter(we_do_financials=True).exclude(property__is_general=True).exclude(property__name__icontains='(general)').select_related('property').order_by('property__name')
    if profiles.count() == 1:
        return redirect('accounting:receipt_new', pk=profiles[0].property_id)
    return render(request, 'accounting/receipt_pick.html', {'profiles': profiles})


@admin_only
def receipt_new(request, pk):
    """Record money an owner paid."""
    prop = association_or_404(pk)
    redirect_to = books_or_redirect(request, prop)
    if redirect_to:
        return redirect_to
    unit = Unit.objects.filter(pk=request.GET.get('unit') or request.POST.get('unit'), property=prop).first()
    if request.method == 'POST':
        try:
            if unit is None:
                raise ValueError('Choose the unit.')
            owner = get_object_or_404(Contact, pk=request.POST.get('owner')) if request.POST.get('owner') else owners.billed_owner(unit)
            if owner is None:
                raise ValueError('That unit has no owner.')
            bank = prop.bank_accounts.filter(pk=request.POST.get('bank'), is_active=True).first() or _lease_bank(prop, unit) or charges.default_bank(prop)
            method = request.POST.get('method') if request.POST.get('method') in dict(Receipt.Method.choices) else 'check'
            charges.receive_payment(prop, unit, owner, parse_date(request.POST.get('date')) or timezone.localdate(), parse_money(request.POST.get('amount')), method,
                                    reference=(request.POST.get('reference') or '').strip(), bank_account=bank, memo=(request.POST.get('memo') or '').strip(), user=request.user, to_loan=request.POST.get('to_loan') == '1')
            messages.success(request, 'Loan payment recorded.' if request.POST.get('to_loan') == '1' else 'Payment recorded.')
            return redirect(_go('unit', prop.pk, unit.pk))
        except (ValueError, ledger.LedgerError, charges.ChargeError) as e:
            messages.error(request, str(e))
    accounts = [a for a in charges.accounts(prop) if unit and a['unit'].pk == unit.pk]
    return render(request, 'accounting/receipt_form.html', {'assoc': prop, 'unit': unit, 'units': owners.active_units(prop), 'banks': prop.bank_accounts.filter(is_active=True), 'methods': Receipt.Method.choices,
                                                            'today': timezone.localdate(), 'accounts': accounts, 'billed': owners.billed_owner(unit) if unit else None, 'lease_bank': _lease_bank(prop, unit), 'loan': sum((a['loan'] for a in accounts), Decimal('0.00')),
                                                            **acc_up(request, prop, _go('unit', prop.pk, unit.pk) if unit else _go('hub', prop.pk), 'Back')})
