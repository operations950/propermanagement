"""Owner collections (the board of late owners and where each case stands) and the Post late fees screen."""
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.db.models import Max
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from . import charges, closing, delinquency, ledger, owner_collections
from .models import ZERO, CaseEvent, Charge, DelinquencyCase
from .views_delinquency import _ids, _make_letters
from .web import admin_only, association_only_or_404, association_or_404, books_or_redirect, parse_date, parse_money, acc_up


def _asof(request, today):
    """(the date the list is worked out for, whether the person chose it). Unless a date is chosen it is the end of last month; a date in the future means today."""
    day = parse_date(request.POST.get('asof') or request.GET.get('asof') or '')
    if day is None:
        return owner_collections.default_asof(today), False
    return min(day, today), True


def _back(here, tab, asof, given):
    """Back to this screen on the same tab and the same date (the date only when the person chose one, so the plain address stays plain)."""
    bits = ([f'tab={tab}'] if tab else []) + ([f'asof={asof:%Y-%m-%d}'] if given else [])
    return here + ('?' + '&'.join(bits) if bits else '')


@admin_only
def collections_home(request):
    """Owner collections for every association we keep the books for: who needs a look in each."""
    today = timezone.localdate()
    rows = []
    for prof in closing.associations(commercial=False):
        look = owner_collections.attention(prof.property, today)
        rows.append({'assoc': prof.property, **look})
    return render(request, 'accounting/collections_home.html', {'rows': rows, 'total': sum(r['count'] for r in rows), **acc_up(request, None, ['ws_financials'], reverse('ws_financials'), 'Financials')})


@admin_only
def collections(request, pk):
    prop = association_only_or_404(pk)            # a commercial property has no owner collections
    ledger.profile_for(prop)
    redirect_to = books_or_redirect(request, prop)
    if redirect_to:
        return redirect_to
    profile = ledger.profile_for(prop)
    today = timezone.localdate()
    here = reverse('accounting:collections', args=[pk])
    tab = request.POST.get('tab') or request.GET.get('tab') or ''
    asof, given = _asof(request, today)
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'threshold':
                amount = parse_money(request.POST.get('amount'))
                if amount < 0:
                    raise ValueError('The amount cannot be below zero.')
                profile.delinquency_threshold = amount
                profile.save(update_fields=['delinquency_threshold'])
                messages.success(request, f'Saved: owners are listed once they owe more than ${amount:,.2f}.')
            elif action == 'advance':
                case = get_object_or_404(DelinquencyCase.objects.select_related('unit', 'owner'), pk=request.POST.get('case'), association=prop, resolved_on__isnull=True)
                stage = delinquency.advance(case, request.user, note=request.POST.get('note') or '', attorney=request.POST.get('attorney') or '')
                messages.success(request, f'{case.owner.name}, unit {case.unit.label}: now at "{DelinquencyCase.Stage(stage).label}". The next review is in 30 days.')
                tab = str(stage)
            elif action == 'make_letters':
                units = _ids(request)
                if not units:
                    raise ValueError('Tick the owners first.')
                made, problems = _make_letters(prop, units, request.user, asof=asof if given else None)
                if made:
                    messages.success(request, f'Made {len(made)} late letter{"s" if len(made) != 1 else ""} as drafts and opened their cases. Open each one to read it through, make it final and mail it.')
                    tab = 'letter'
                for p in problems:
                    messages.error(request, p)
        except (ValueError, ledger.LedgerError) as e:
            messages.error(request, str(e))
        return redirect(_back(here, tab, asof, given))
    data = owner_collections.board(prop, today, asof)
    by_key = {t['key']: t for t in data['tabs']}
    if tab not in by_key:                      # open on the first step that has something to do, else the first that has anyone
        tab = next((t['key'] for t in data['tabs'] if t['key'] != 'resolved' and t['alerts']), None) or next((t['key'] for t in data['tabs'] if t['key'] != 'resolved' and t['count']), 'none')
    ctx = {'assoc': prop, 'profile': profile, 'data': data, 'tabs': data['tabs'], 'tab': by_key[tab], 'today': today, 'terms': charges.terms_sentences(profile), 'threshold': data['threshold'],
           'asof': asof, 'default_asof': data['default_asof'], 'asof_given': given, 'asof_is_default': asof == data['default_asof'], 'asof_is_today': asof == today,
           'asof_qs': f'&asof={asof:%Y-%m-%d}' if given else '',
           'letter_days': owner_collections.LETTER_DAYS, 'notice_days': owner_collections.NOTICE_DAYS, **acc_up(request, prop, ['accounting:hub', 'accounting:close', 'accounting:collections_home', 'accounting:late_fees'], reverse('accounting:hub', args=[pk]), 'Accounting')}
    return render(request, 'accounting/collections.html', ctx)


def late_fee_rows(prop, today):
    """One row for each unit that has a late fee or interest ready to charge: who, how much of each."""
    units = {}
    for c, fee, due in charges._late_fee_rows(prop, today):
        row = units.setdefault(c.unit_id, {'unit': c.unit, 'owner': c.owner, 'fee': ZERO, 'interest': ZERO, 'what': []})
        row['fee'] += fee
        row['what'].append(f'Late fee on {c.description}')
    for c, mf, me, amount in charges._interest_rows(prop, today):
        row = units.setdefault(c.unit_id, {'unit': c.unit, 'owner': c.owner, 'fee': ZERO, 'interest': ZERO, 'what': []})
        row['interest'] += amount
        row['what'].append(f'Interest on {c.description}, {mf:%B %Y}')
    return sorted(units.values(), key=lambda r: (r['unit'].label,))


ASSESSMENT_KINDS = (Charge.Kind.ASSESSMENT, Charge.Kind.SPECIAL)


def _late_as_of(params, today):
    """(the day the screen judges lateness for, a note when the day asked for could not be used). No day is today; so is one that is not a date or is in the future."""
    raw = (params.get('as_of') or params.get('asof') or '').strip()
    day = parse_date(raw)
    if day is None:
        return today, ('That is not a date, so today is used.' if raw else '')
    if day > today:
        return today, 'That day has not come yet, so today is used.'
    return day, ''


def _min_balance(params):
    """The smallest total owed that is listed (blank is none). A bad amount is none."""
    raw = (params.get('min') or '').replace('$', '').replace(',', '').strip()
    try:
        return max(Decimal(raw).quantize(Decimal('0.01')), ZERO) if raw else ZERO
    except InvalidOperation:
        return ZERO


def _late_qs(as_of, min_balance, show_all, month):
    bits = [f'as_of={as_of:%Y-%m-%d}']
    if min_balance > 0:
        bits.append(f'min={min_balance:.2f}')
    if show_all:
        bits.append('all=1')
    if month:
        bits.append(f'month={month}')
    return '&'.join(bits)


def late_fee_data(prop, as_of, min_balance=ZERO, show_all=False):
    """The Post late fees screen for one day. One row for each unit that is late on `as_of` (it has a late fee or interest ready, or, with `show_all`, any assessment unpaid past its
    grace period that day) and owes at least `min_balance` in all: total owed (principal, late fees and interest still open that day), when it was last charged a late fee or interest, and
    the late fee and interest ready to charge, with the charges that make the numbers up. Everything is as the account stood on `as_of`."""
    profile = ledger.profile_for(prop)
    grace = profile.late_fee_grace_days or 0
    fee_for = {c.pk: fee for c, fee, due in charges._late_fee_rows(prop, as_of, as_of=as_of)}
    interest_for = {}
    for c, month_first, end, amount in charges._interest_rows(prop, as_of, as_of=as_of):
        interest_for.setdefault(c.pk, []).append((month_first, end, amount))
    last_charged = dict(Charge.objects.filter(association=prop, is_void=False, kind__in=[Charge.Kind.LATE_FEE, Charge.Kind.INTEREST]).values_list('unit_id').annotate(d=Max('due_date')))
    units = {}
    qs = Charge.objects.filter(association=prop, is_void=False, due_date__lte=as_of).select_related('unit', 'owner').prefetch_related('applications').order_by('due_date', 'id')
    for c in qs:
        open_ = charges.balance_as_of(c, as_of)
        if open_ <= 0:
            continue
        row = units.setdefault(c.unit_id, {'unit': c.unit, 'owner': c.owner, 'principal': ZERO, 'late_fees': ZERO, 'interest': ZERO, 'total': ZERO, 'fee_ready': ZERO, 'interest_ready': ZERO,
                                           'late': False, 'lines': [], 'last_charged': last_charged.get(c.unit_id)})
        row['owner'] = c.owner
        if c.kind == Charge.Kind.LATE_FEE:
            row['late_fees'] += open_
        elif c.kind == Charge.Kind.INTEREST:
            row['interest'] += open_
        else:
            row['principal'] += open_
        row['total'] += open_
        is_late = c.kind in ASSESSMENT_KINDS and c.due_date < as_of - timedelta(days=grace)
        row['late'] = row['late'] or is_late
        fee = fee_for.get(c.pk, ZERO)
        months = interest_for.get(c.pk, [])
        row['fee_ready'] += fee
        row['interest_ready'] += sum((a for mf, end, a in months), ZERO)
        row['lines'].append({'charge': c, 'balance': open_, 'late': is_late, 'fee': fee, 'months': months})
    rows = []
    for row in units.values():
        ready = row['fee_ready'] + row['interest_ready']
        if not (ready > 0 or (show_all and row['late'])):
            continue
        if row['total'] < min_balance:
            continue
        rows.append(row)
    rows.sort(key=lambda r: (r['unit'].label,))
    return rows


@admin_only
def late_fees(request, pk):
    """Charge the late fees and interest that are owed, judged as of a day the person picks (the end of the month being closed, say). Nothing is charged until the button is pressed, so the list
    can be looked at first. Owners who owe less than the amount typed are not listed and are never charged."""
    prop = association_or_404(pk)
    redirect_to = books_or_redirect(request, prop)
    if redirect_to:
        return redirect_to
    profile = ledger.profile_for(prop)
    today = timezone.localdate()
    here = reverse('accounting:late_fees', args=[pk])
    params = request.POST if request.method == 'POST' else request.GET
    as_of, as_of_note = _late_as_of(params, today)
    min_balance = _min_balance(params)
    show_all = params.get('all') == '1'
    month = params.get('month') or ''
    month = month if len(month) == 7 and month[:4].isdigit() and month[4] == '-' and month[5:].isdigit() else ''
    back = here + '?' + _late_qs(as_of, min_balance, show_all, month)
    if request.method == 'POST':
        action = request.POST.get('action')
        post_day = parse_date(request.POST.get('post_on') or '', as_of)
        try:
            if action in ('charge_late', 'charge_interest'):
                listed = {r['unit'].pk for r in late_fee_data(prop, as_of, min_balance, show_all)}
                ticked = _ids(request)
                units = [u for u in ticked if u in listed] if ticked else sorted(listed)             # never an owner who is not on the list
                if not units:
                    raise ValueError('Nobody is listed, so nothing was charged.')
                if ledger.is_locked(prop, post_day):
                    raise ledger.LedgerError(f'{post_day:%B %Y} is closed. Open the month again to post into it, or choose another day to post on.')
                if action == 'charge_late':
                    made = charges.assess_late_fees(prop, today, request.user, units=units, as_of=as_of, post_day=post_day)
                else:
                    made = charges.assess_interest(prop, today, request.user, units=units, as_of=as_of, post_day=post_day)
                what = 'late fee' if action == 'charge_late' else 'interest charge'
                messages.success(request, f'Charged {len(made)} {what}{"s" if len(made) != 1 else ""}: ${sum((c.amount for c in made), ZERO):,.2f}, dated {post_day:%b} {post_day.day}.' if made else f'No {what}s were owed.')
                for c in made:
                    case = delinquency.open_case_for(prop, c.unit)
                    if case:
                        delinquency.add_event(case, CaseEvent.Kind.FEE, f'{c.description}: ${c.amount:,.2f}.', request.user)
        except (ValueError, ledger.LedgerError, charges.ChargeError) as e:
            messages.error(request, str(e))
        return redirect(back)
    rows = late_fee_data(prop, as_of, min_balance, show_all)
    ctx = {'assoc': prop, 'profile': profile, 'rows': rows, 'fees': sum((r['fee_ready'] for r in rows), ZERO), 'interest': sum((r['interest_ready'] for r in rows), ZERO), 'owed': sum((r['total'] for r in rows), ZERO),
           'today': today, 'as_of': as_of, 'as_of_note': as_of_note, 'min_balance': min_balance, 'show_all': show_all, 'month': month, 'post_on': as_of,
           'is_set': profile.late_fee_is_set() or profile.interest_is_set(), 'terms': charges.terms_sentences(profile), 'look': owner_collections.attention(prop, today),
           **acc_up(request, prop, ['accounting:close', 'accounting:hub', 'accounting:collections'], reverse('accounting:hub', args=[pk]), 'Accounting')}
    return render(request, 'accounting/late_fees.html', ctx)
