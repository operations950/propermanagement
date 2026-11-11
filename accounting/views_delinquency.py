"""The delinquency screen (who is behind, charge the late fees and interest, make the late letters) and one owner's case."""
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from . import charges, delinquency, ledger
from .models import ZERO, CaseEvent, DelinquencyCase
from .web import admin_only, association_or_404, books_or_redirect, parse_date


def _ids(request, name='unit'):
    return [int(i) for i in request.POST.getlist(name) if i.isdigit()]


@admin_only
def delinquencies(request, pk):
    prop = association_or_404(pk)
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
            elif action == 'make_letters':
                if not units:
                    raise ValueError('Tick the owners first.')
                made, problems = _make_letters(prop, units, request.user)
                if made:
                    messages.success(request, f'Made {len(made)} late letter{"s" if len(made) != 1 else ""} as drafts and opened their cases. Open each one to read it through and make it final.')
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
           'acc_back': reverse('accounting:hub', args=[pk]), 'acc_back_label': 'Accounting'}
    return render(request, 'accounting/delinquencies.html', ctx)


def _make_letters(prop, unit_ids, user):
    """A draft Notice of Late Assessment for each unit, filled from the books. Making one opens the owner's case."""
    from core.models import Unit
    from legalforms import services
    from legalforms.models import DocumentTemplate
    template = DocumentTemplate.objects.filter(slug='nola', is_active=True).first()
    if template is None:
        return [], ['The Notice of Late Assessment form is not set up.']
    made, problems = [], []
    for unit in Unit.objects.filter(property=prop, pk__in=unit_ids):
        values = services.initial_values(template, prop, unit, user)
        raw = {k: ('' if v is None else str(v)) for k, v in values.items()}
        doc, errors = services.save_document(template, prop, unit, user, raw)
        if doc:
            made.append(doc)
        else:
            problems.append(f'Unit {unit.label}: ' + '; '.join(str(e) for e in (errors.values() if isinstance(errors, dict) else [errors])))
    return made, problems


@admin_only
def case_detail(request, pk, case_pk):
    prop = association_or_404(pk)
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
    ctx = {'assoc': prop, 'case': case, 'b': b, 'open_charges': open_charges, 'events': events, 'icons': delinquency.case_icons(case), 'stages': [s for s in DelinquencyCase.Stage if s != DelinquencyCase.Stage.RESOLVED],
           'letters': GeneratedDocument.objects.filter(property=prop, unit=case.unit, template__slug='nola').order_by('-created_at'), 'today': timezone.localdate(),
           'acc_back': reverse('accounting:delinquencies', args=[pk]), 'acc_back_label': 'Delinquencies'}
    return render(request, 'accounting/delinquency_case.html', ctx)
