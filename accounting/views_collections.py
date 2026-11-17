"""Owner collections (the board of late owners and where each case stands) and the Post late fees screen."""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.db.models import Max
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from . import charges, closing, delinquency, ledger, owner_collections
from .models import ZERO, CaseEvent, Charge, CloseStepDone, DelinquencyCase
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


def step_done_context(prop, month_text, today):
    """What the top-right 'mark done' button on the owner collections screen needs: the month of the close it is for (the one the close sheet passed, else last month) and whether it is marked."""
    month_text = month_text if month_text and len(month_text) == 7 else ''
    month = _done_month(month_text, ledger.previous_month(today))
    done = CloseStepDone.objects.filter(association=prop, month=month, key='collections').select_related('marked_by').first()
    return {'step_done': done, 'step_month': month, 'step_label': 'owner collections', 'step_confirm': f'Mark owner collections done for {month:%B %Y}? This does not change anything for any owner; it tells the month-end close you have looked.',
            'step_hidden': [('month', f'{month:%Y-%m}')]}


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
            elif action in ('mark_done', 'undo_done'):
                month_text = request.POST.get('month') or ''
                done_for = _done_month(month_text if len(month_text) == 7 else '', ledger.previous_month(today))
                if action == 'mark_done':
                    CloseStepDone.objects.get_or_create(association=prop, month=done_for, key='collections', defaults={'marked_by': request.user})
                    messages.success(request, f'Owner collections are marked done for {done_for:%B %Y}.')
                else:
                    CloseStepDone.objects.filter(association=prop, month=done_for, key='collections').delete()
                    messages.success(request, f'Owner collections are open again for {done_for:%B %Y}.')
                return redirect(here + f'?month={done_for:%Y-%m}')
            elif action == 'advance':
                case = get_object_or_404(DelinquencyCase.objects.select_related('unit', 'owner'), pk=request.POST.get('case'), association=prop, resolved_on__isnull=True)
                stage = delinquency.advance(case, request.user, note=request.POST.get('note') or '', attorney=request.POST.get('attorney') or '')
                messages.success(request, f'{case.owner.name}, unit {case.unit.label}: now at "{DelinquencyCase.Stage(stage).label}". The next review is in 30 days.')
                tab = str(stage)
            elif action in ('open_cases', 'make_letters'):
                units = _ids(request)
                if not units:
                    raise ValueError('Tick the owners first.')
                made, problems = _make_letters(prop, units, request.user, asof=asof if given else None)
                if made:
                    messages.success(request, f'Opened {len(made)} case{"s" if len(made) != 1 else ""} and drafted the late letter{"s" if len(made) != 1 else ""}. Read each one through, make it final, print it and mail it.')
                    tab = 'preparing'
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
           'letter_days': owner_collections.LETTER_DAYS, 'notice_days': owner_collections.NOTICE_DAYS, **step_done_context(prop, request.GET.get('month'), today),
           **acc_up(request, prop, ['accounting:hub', 'accounting:close', 'accounting:collections_home'], reverse('accounting:hub', args=[pk]), 'Accounting')}      # late fees is a step beside it, not above it: Up skips it, back to the checklist
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


ASSESSMENT_KINDS = (Charge.Kind.ASSESSMENT, Charge.Kind.SPECIAL, Charge.Kind.OPENING)


def _charged_text(done):
    """'Late fee $30.75 on Oct 31, interest $12.30 on Oct 31': what was already charged on an owner's late assessments (one line for each kind, the latest day)."""
    bits = []
    for kind, label in ((Charge.Kind.LATE_FEE, 'Late fee'), (Charge.Kind.INTEREST, 'Interest')):
        mine = [(a, d) for k, a, d in done if k == kind]
        if mine:
            day = max(d for a, d in mine)
            bits.append(f'{label} ${sum((a for a, d in mine), ZERO):,.2f} on {day:%b} {day.day}')
    return ', '.join(bits)


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


def _done_month(month, as_of):
    """The first day of the month the 'mark done' applies to: the month the close sheet passed, else the month of the as-of day."""
    if month:
        try:
            return date(int(month[:4]), int(month[5:]), 1)
        except ValueError:
            pass
    return as_of.replace(day=1)


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
    """The Post late fees screen for one day. One row for each unit that is late on `as_of` (an assessment unpaid past its grace period that day), whether or not its late fee and
    interest were already charged, and that owes at least `min_balance` in all: total owed (principal, late fees and interest still open that day), the date a late fee was last charged and the date interest was last charged (two dates), the late fee and interest READY to charge, and what was ALREADY charged (so an owner does not drop off the list once charged), with the charges that make the numbers up.
    With `show_all`, units that are not late but owe something are listed too. Everything is as the account stood on `as_of`."""
    profile = ledger.profile_for(prop)
    grace = profile.late_fee_grace_days or 0
    fee_for = {c.pk: fee for c, fee, due in charges._late_fee_rows(prop, as_of, as_of=as_of)}
    interest_for = {}
    for c, month_first, end, amount in charges._interest_rows(prop, as_of, as_of=as_of):
        interest_for.setdefault(c.pk, []).append((month_first, end, amount))
    def last_of(kind):
        return dict(Charge.objects.filter(association=prop, is_void=False, kind=kind, due_date__lte=as_of).values_list('unit_id').annotate(d=Max('due_date')))
    last_fee, last_interest = last_of(Charge.Kind.LATE_FEE), last_of(Charge.Kind.INTEREST)       # each kept on its own: the date a late fee was last charged, and the date interest was
    # what was already charged on each assessment: its late fee and its interest, so an owner charged already still shows what was charged and when
    already = {}
    for child in Charge.objects.filter(association=prop, is_void=False, kind__in=[Charge.Kind.LATE_FEE, Charge.Kind.INTEREST], parent__isnull=False):
        already.setdefault(child.parent_id, []).append(child)
    units = {}
    qs = Charge.objects.filter(association=prop, is_void=False, due_date__lte=as_of).exclude(kind=Charge.Kind.LOAN).select_related('unit', 'owner').prefetch_related('applications').order_by('due_date', 'id')
    for c in qs:
        open_ = charges.balance_as_of(c, as_of)
        if open_ <= 0:
            continue
        row = units.setdefault(c.unit_id, {'unit': c.unit, 'owner': c.owner, 'principal': ZERO, 'late_fees': ZERO, 'interest': ZERO, 'total': ZERO, 'fee_ready': ZERO, 'interest_ready': ZERO,
                                           'late': False, 'lines': [], 'last_fee': last_fee.get(c.unit_id), 'last_interest': last_interest.get(c.unit_id), 'charged': []})
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
        done = [(k, a, d) for k, a, d in ((ch.kind, ch.amount, ch.due_date) for ch in already.get(c.pk, []))]
        row['lines'].append({'charge': c, 'balance': open_, 'late': is_late, 'fee': fee, 'months': months, 'charged': done})
        row['charged'].extend(done)
    rows = []
    for row in units.values():
        ready = row['fee_ready'] + row['interest_ready']
        row['charged_text'] = _charged_text(row['charged'])
        row['last_charged'] = max((d for d in (row['last_fee'], row['last_interest']) if d), default=None)     # the later of the two, kept for anything that wants one date
        row['ready'] = ready > 0
        if not (ready > 0 or row['late'] or show_all):
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
    done_month = _done_month(month, as_of)
    if request.method == 'POST':
        action = request.POST.get('action')
        post_day = parse_date(request.POST.get('post_on') or '', as_of)
        try:
            if action in ('charge_late', 'charge_interest'):
                rows_now = late_fee_data(prop, as_of, min_balance, show_all)
                listed = {r['unit'].pk for r in rows_now}
                ready = {r['unit'].pk for r in rows_now if r['fee_ready'] > 0 or r['interest_ready'] > 0}
                ticked = _ids(request)
                units = [u for u in ticked if u in listed] if ticked else sorted(ready)          # never an owner who is not on the list; with none ticked, those with something ready
                if not units:
                    raise ValueError('Nobody listed has anything ready to charge, so nothing was charged.')
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
            elif action == 'mark_done':
                if request.POST.get('confirm') != '1':
                    raise ValueError('Tick the box to say you have charged everyone you wish to charge.')
                CloseStepDone.objects.get_or_create(association=prop, month=done_month, key='late_fees', defaults={'marked_by': request.user})
                messages.success(request, f'Late fees and interest are marked done for {done_month:%B %Y}.')
            elif action == 'undo_done':
                CloseStepDone.objects.filter(association=prop, month=done_month, key='late_fees').delete()
                messages.success(request, f'Late fees and interest are open again for {done_month:%B %Y}.')
        except (ValueError, ledger.LedgerError, charges.ChargeError) as e:
            messages.error(request, str(e))
        return redirect(back)
    rows = late_fee_data(prop, as_of, min_balance, show_all)
    # Up: the close sheet when this screen was opened from it (its link always carries ?month=), else Accounting; the trail still wins when it knows where the person came from
    up_default = (reverse('accounting:close', args=[pk, month]), 'Month-end close') if month and _done_month(month, as_of).strftime('%Y-%m') == month else (reverse('accounting:hub', args=[pk]), 'Accounting')
    ctx = {'assoc': prop, 'profile': profile, 'rows': rows, 'fees': sum((r['fee_ready'] for r in rows), ZERO), 'interest': sum((r['interest_ready'] for r in rows), ZERO), 'owed': sum((r['total'] for r in rows), ZERO),
           'step_done': CloseStepDone.objects.filter(association=prop, month=done_month, key='late_fees').select_related('marked_by').first(), 'step_month': done_month, 'step_label': 'late fees and interest',
           'step_confirm': f'Mark late fees and interest done for {done_month:%B %Y}? This charges nobody; it tells the month-end close you have charged everyone you wish to charge.',
           'step_hidden': [('month', month), ('as_of', f'{as_of:%Y-%m-%d}')],
           'ready_n': sum(1 for r in rows if r['ready']), 'done_month': done_month, 'done': CloseStepDone.objects.filter(association=prop, month=done_month, key='late_fees').select_related('marked_by').first(),
           'today': today, 'as_of': as_of, 'as_of_note': as_of_note, 'min_balance': min_balance, 'show_all': show_all, 'month': month, 'post_on': as_of,
           'is_set': profile.late_fee_is_set() or profile.interest_is_set(), 'terms': charges.terms_sentences(profile), 'look': owner_collections.attention(prop, today),
           **acc_up(request, prop, ['accounting:close', 'accounting:hub'], *up_default)}                  # Owner collections is the next step beside it, not above it
    return render(request, 'accounting/late_fees.html', ctx)
