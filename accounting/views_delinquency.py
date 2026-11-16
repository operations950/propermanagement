"""The delinquency screen (who is behind, charge the late fees and interest, make the late letters) and one owner's case."""
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from . import charges, delinquency, ledger
from .models import ZERO, CaseEvent, DelinquencyCase
from .web import admin_only, association_only_or_404, books_or_redirect, parse_date, acc_up


def _ids(request, name='unit'):
    return [int(i) for i in request.POST.getlist(name) if i.isdigit()]


@admin_only
def delinquencies(request, pk):
    prop = association_only_or_404(pk)
    ledger.profile_for(prop)
    redirect_to = books_or_redirect(request, prop)
    if redirect_to:
        return redirect_to
    profile = ledger.profile_for(prop)
    today = timezone.localdate()
    here = reverse('accounting:delinquencies', args=[pk])
    try:
        threshold = Decimal(str(request.GET.get('threshold') or request.POST.get('threshold') or profile.delinquency_threshold)).quantize(Decimal('0.01'))
    except InvalidOperation:
        threshold = profile.delinquency_threshold
    if request.method == 'POST':
        action = request.POST.get('action')
        units = _ids(request)
        back = f'{here}?threshold={threshold}'
        try:
            if action == 'threshold':
                profile.delinquency_threshold = max(threshold, ZERO)
                profile.save(update_fields=['delinquency_threshold'])
                messages.success(request, f'Saved: owners are chased once they owe more than ${threshold:,.2f}.')
            elif action in ('charge_late', 'charge_interest'):
                if not units:
                    raise ValueError('Tick the owners first.')
                made = charges.assess_late_fees(prop, today, request.user, units=units) if action == 'charge_late' else charges.assess_interest(prop, today, request.user, units=units)
                what = 'late fee' if action == 'charge_late' else 'interest charge'
                messages.success(request, f'Charged {len(made)} {what}{"s" if len(made) != 1 else ""}: ${sum((c.amount for c in made), ZERO):,.2f}.' if made else f'No {what}s were owed to the owners you ticked.')
                for c in made:
                    case = delinquency.open_case_for(prop, c.unit)
                    if case:
                        delinquency.add_event(case, CaseEvent.Kind.FEE, f'{c.description}: ${c.amount:,.2f}.', request.user)
            elif action in ('open_cases', 'make_letters'):
                if not units:
                    raise ValueError('Tick the owners first.')
                made, problems = _make_letters(prop, units, request.user)
                if made:
                    messages.success(request, f'Opened {len(made)} case{"s" if len(made) != 1 else ""} and drafted the late letter{"s" if len(made) != 1 else ""}. Finish them on Owner collections, under Preparing the letter.')
                for p in problems:
                    messages.error(request, p)
        except (ValueError, ledger.LedgerError, charges.ChargeError) as e:
            messages.error(request, str(e))
        return redirect(back)
    rows = delinquency.delinquent_rows(prop, today, threshold)
    cases = delinquency.open_cases(prop, today)
    shown_units = {r['unit'].pk for r in rows}
    ctx = {'assoc': prop, 'profile': profile, 'rows': rows, 'cases': cases, 'threshold': threshold, 'today': today,
           'summary': {'count': len(rows), 'owed': sum((r['b']['total'] for r in rows), ZERO), 'fees': sum((r['fee_due'] for r in rows), ZERO), 'interest': sum((r['interest_due'] for r in rows), ZERO),
                       'review': sum(1 for c in cases if c['review_due']), 'letters': sum(1 for r in rows if r['needs_letter'])},
           'other_cases': [c for c in cases if c['case'].unit_id not in shown_units], 'terms': charges.terms_sentences(profile),
           **acc_up(request, prop, ['accounting:hub', 'accounting:close'], reverse('accounting:hub', args=[pk]), 'Accounting')}
    return render(request, 'accounting/delinquencies.html', ctx)


def _make_letters(prop, unit_ids, user, asof=None):
    """Open a case for each unit: its draft Notice of Late Assessment is made from the books and the owner goes on the *Preparing the letter* step (nothing is sent). A unit that already has an
    open case is skipped. Returns ([draft documents], [problems and notes]). `asof`: the date the owner was behind as of (the letter's "balance as of")."""
    from core.models import Unit
    from . import letter_prep
    made, problems = [], []
    for unit in Unit.objects.filter(property=prop, pk__in=unit_ids):
        if delinquency.open_case_for(prop, unit):
            problems.append(f'Unit {unit.label} already has an open case: it was left as it is.')
            continue
        doc, problem = letter_prep.make_draft(prop, unit, user, asof)
        if doc:
            made.append(doc)
        else:
            problems.append(problem)
    return made, problems


@admin_only
def case_detail(request, pk, case_pk):
    prop = association_only_or_404(pk)
    ledger.profile_for(prop)
    redirect_to = books_or_redirect(request, prop)
    if redirect_to:
        return redirect_to
    case = get_object_or_404(DelinquencyCase.objects.select_related('unit', 'owner'), pk=case_pk, association=prop)
    here = reverse('accounting:case', args=[pk, case.pk])
    if request.method == 'POST':
        action = request.POST.get('action')
        note = (request.POST.get('note') or '').strip()
        try:
            if action in ('note', 'call', 'letter'):
                if not note:
                    raise ValueError('Type what happened.')
                kind = {'note': CaseEvent.Kind.NOTE, 'call': CaseEvent.Kind.CALL, 'letter': CaseEvent.Kind.LETTER}[action]
                delinquency.add_event(case, kind, note, request.user, on=parse_date(request.POST.get('date')))
                messages.success(request, 'Added.')
            elif action == 'stage':
                if case.stage == DelinquencyCase.Stage.PREPARING:
                    raise ValueError('Finish preparing the letter first: it moves to Letter sent when the mailing date is confirmed.')
                delinquency.set_stage(case, request.POST.get('stage'), request.user, note=note, attorney=request.POST.get('attorney') or '')
                messages.success(request, 'Moved on. The next review is in 30 days.')
            elif action == 'review':
                delinquency.review(case, request.user, note)
                messages.success(request, 'Reviewed. It comes back in 30 days.')
            elif action == 'review_date':
                day = parse_date(request.POST.get('review_date'))
                if day is None:
                    raise ValueError('Give the date of the next review.')
                delinquency.set_review_date(case, day, request.user)
                messages.success(request, 'Saved.')
            elif action == 'resolve':
                delinquency.resolve(case, request.user, note)
                messages.success(request, 'The case is resolved.')
        except (ValueError, ledger.LedgerError) as e:
            messages.error(request, str(e))
        return redirect(here)
    from legalforms.models import GeneratedDocument
    b = delinquency.breakdown(case.unit, case.owner)
    open_charges = charges.open_charges(case.unit, case.owner)
    events = list(case.events.select_related('document', 'by'))
    prep_row = None
    if case.stage == DelinquencyCase.Stage.PREPARING and not case.resolved_on:
        from . import letter_prep
        prep_row = {'case': case, 'prep': letter_prep.status(case)}
    ctx = {'assoc': prop, 'case': case, 'b': b, 'open_charges': open_charges, 'events': events, 'icons': delinquency.case_icons(case), 'prep_row': prep_row,
           'stages': [s for s in DelinquencyCase.Stage if s not in (DelinquencyCase.Stage.RESOLVED, DelinquencyCase.Stage.PREPARING)],
           'letters': GeneratedDocument.objects.filter(property=prop, unit=case.unit, template__slug='nola').order_by('-created_at'), 'today': timezone.localdate(),
           **acc_up(request, prop, ['accounting:collections', 'accounting:delinquencies'], reverse('accounting:collections', args=[pk]) + f'?tab={case.stage}', 'Owner collections')}
    return render(request, 'accounting/delinquency_case.html', ctx)
