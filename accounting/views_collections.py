"""Owner collections (the board of late owners and where each case stands) and the Post late fees screen."""
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from . import charges, closing, delinquency, ledger, owner_collections
from .models import ZERO, CaseEvent, DelinquencyCase
from .views_delinquency import _ids, _make_letters
from .web import admin_only, association_or_404, books_or_redirect


@admin_only
def collections_home(request):
    """Owner collections for every association we keep the books for: who needs a look in each."""
    today = timezone.localdate()
    rows = []
    for prof in closing.associations():
        look = owner_collections.attention(prof.property, today)
        rows.append({'assoc': prof.property, **look})
    return render(request, 'accounting/collections_home.html', {'rows': rows, 'total': sum(r['count'] for r in rows), 'acc_back': reverse('ws_financials'), 'acc_back_label': 'Financials'})


@admin_only
def collections(request, pk):
    prop = association_or_404(pk)
    redirect_to = books_or_redirect(request, prop)
    if redirect_to:
        return redirect_to
    profile = ledger.profile_for(prop)
    today = timezone.localdate()
    here = reverse('accounting:collections', args=[pk])
    tab = request.POST.get('tab') or request.GET.get('tab') or ''
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'advance':
                case = get_object_or_404(DelinquencyCase.objects.select_related('unit', 'owner'), pk=request.POST.get('case'), association=prop, resolved_on__isnull=True)
                stage = delinquency.advance(case, request.user, note=request.POST.get('note') or '', attorney=request.POST.get('attorney') or '')
                messages.success(request, f'{case.owner.name}, unit {case.unit.label}: now at "{DelinquencyCase.Stage(stage).label}". The next review is in 30 days.')
                tab = str(stage)
            elif action == 'make_letters':
                units = _ids(request)
                if not units:
                    raise ValueError('Tick the owners first.')
                made, problems = _make_letters(prop, units, request.user)
                if made:
                    messages.success(request, f'Made {len(made)} late letter{"s" if len(made) != 1 else ""} as drafts and opened their cases. Open each one to read it through, make it final and mail it.')
                    tab = 'letter'
                for p in problems:
                    messages.error(request, p)
        except (ValueError, ledger.LedgerError) as e:
            messages.error(request, str(e))
        return redirect(f'{here}?tab={tab}' if tab else here)
    data = owner_collections.board(prop, today)
    by_key = {t['key']: t for t in data['tabs']}
    if tab not in by_key:                      # open on the first step that has something to do, else the first that has anyone
        tab = next((t['key'] for t in data['tabs'] if t['key'] != 'resolved' and t['alerts']), None) or next((t['key'] for t in data['tabs'] if t['key'] != 'resolved' and t['count']), 'none')
    ctx = {'assoc': prop, 'profile': profile, 'data': data, 'tabs': data['tabs'], 'tab': by_key[tab], 'today': today, 'terms': charges.terms_sentences(profile), 'threshold': data['threshold'],
           'letter_days': owner_collections.LETTER_DAYS, 'notice_days': owner_collections.NOTICE_DAYS, 'acc_back': reverse('accounting:hub', args=[pk]), 'acc_back_label': 'Accounting'}
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


@admin_only
def late_fees(request, pk):
    """Charge the late fees and interest that are owed. Nothing is charged until the button is pressed, so the list can be looked at first."""
    prop = association_or_404(pk)
    redirect_to = books_or_redirect(request, prop)
    if redirect_to:
        return redirect_to
    profile = ledger.profile_for(prop)
    today = timezone.localdate()
    here = reverse('accounting:late_fees', args=[pk])
    if request.method == 'POST':
        action = request.POST.get('action')
        units = _ids(request) or None                       # nothing ticked means everyone
        try:
            if action in ('charge_late', 'charge_interest'):
                made = charges.assess_late_fees(prop, today, request.user, units=units) if action == 'charge_late' else charges.assess_interest(prop, today, request.user, units=units)
                what = 'late fee' if action == 'charge_late' else 'interest charge'
                messages.success(request, f'Charged {len(made)} {what}{"s" if len(made) != 1 else ""}: ${sum((c.amount for c in made), ZERO):,.2f}.' if made else f'No {what}s were owed.')
                for c in made:
                    case = delinquency.open_case_for(prop, c.unit)
                    if case:
                        delinquency.add_event(case, CaseEvent.Kind.FEE, f'{c.description}: ${c.amount:,.2f}.', request.user)
        except (ValueError, ledger.LedgerError, charges.ChargeError) as e:
            messages.error(request, str(e))
        return redirect(here)
    rows = late_fee_rows(prop, today)
    ctx = {'assoc': prop, 'profile': profile, 'rows': rows, 'fees': sum((r['fee'] for r in rows), ZERO), 'interest': sum((r['interest'] for r in rows), ZERO), 'today': today,
           'is_set': profile.late_fee_is_set() or profile.interest_is_set(), 'terms': charges.terms_sentences(profile), 'look': owner_collections.attention(prop, today),
           'acc_back': reverse('accounting:hub', args=[pk]), 'acc_back_label': 'Accounting'}
    return render(request, 'accounting/late_fees.html', ctx)
