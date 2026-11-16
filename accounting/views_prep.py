"""The actions of the *Preparing the letter* step of Owner collections: draft, make final, prepare the affidavit, mark printed, confirm the mailing date, cancel a case opened by mistake, and
the combined PDFs to print (see letter_prep.py)."""
from django.contrib import messages
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from . import ledger, letter_prep
from .models import DelinquencyCase
from .web import admin_only, association_only_or_404, books_or_redirect, parse_date

Stage = DelinquencyCase.Stage
LABEL = {'letter': 'letter', 'affidavit': 'affidavit', 'envelope': 'envelope'}


def _back(request, prop, tab='preparing'):
    target = request.POST.get('next') or ''
    if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        return target
    return reverse('accounting:collections', args=[prop.pk]) + f'?tab={tab}'


def _case(prop, case_pk):
    return get_object_or_404(DelinquencyCase.objects.select_related('unit', 'owner', 'association'), pk=case_pk, association=prop, resolved_on__isnull=True)


@admin_only
def prep_act(request, pk, case_pk):
    prop = association_only_or_404(pk)
    redirect_to = books_or_redirect(request, prop)
    if redirect_to:
        return redirect_to
    case = _case(prop, case_pk)
    if request.method != 'POST':
        return redirect(reverse('accounting:case', args=[pk, case.pk]))
    action = request.POST.get('action')
    who = f'{case.owner.name}, unit {case.unit.label}'
    target = _back(request, prop)
    try:
        if case.stage != Stage.PREPARING:
            raise letter_prep.PrepError('This case is no longer on the preparing step.')
        if action == 'draft':
            letter_prep.draft_missing(case, request.user, asof=parse_date(request.POST.get('asof')))
            messages.success(request, f'{who}: the letter is drafted.')
        elif action == 'finalize':
            letter_prep.finalize_letter(case, request.user)
            messages.success(request, f'{who}: the letter is final.')
        elif action == 'reopen':
            letter_prep.reopen_letter(case, request.user)
            messages.success(request, f'{who}: the letter is a draft again.')
        elif action == 'affidavit':
            day = parse_date(request.POST.get('mailing_date'))
            letter_prep.prepare_affidavit(case, request.user, day=day, replace=request.POST.get('replace') == '1')
            messages.success(request, f'{who}: the mailing affidavit is prepared.')
        elif action in ('printed', 'unprinted'):
            what = request.POST.get('what')
            if action == 'printed':
                letter_prep.mark_printed(case, what, request.user)
                messages.success(request, f'{who}: the {LABEL.get(what, what)} is marked printed.')
            else:
                letter_prep.unmark_printed(case, what)
                messages.success(request, f'{who}: the {LABEL.get(what, what)} is no longer marked printed.')
        elif action == 'mailed':
            letter_prep.mark_mailed(case, parse_date(request.POST.get('mailed_on')), request.user)
            messages.success(request, f'{who}: marked mailed on {case.stage_since:%b} {case.stage_since.day}. The case is now on Letter sent.')
            target = _back(request, prop, tab='letter') if not request.POST.get('next') else target
        elif action == 'cancel':
            if request.POST.get('confirm') != 'yes':
                raise letter_prep.PrepError('Confirm to cancel the case.')
            letter_prep.cancel_case(case)
            messages.success(request, f'{who}: the case was cancelled and its drafts deleted.')
            if request.POST.get('next', '').endswith(f'/cases/{case.pk}/') or not request.POST.get('next'):
                target = reverse('accounting:collections', args=[prop.pk]) + '?tab=none'
        else:
            raise letter_prep.PrepError('Choose what to do.')
    except (letter_prep.PrepError, ValueError, ledger.LedgerError) as e:
        messages.error(request, str(e))
    return redirect(target)


@admin_only
def prep_bulk(request, pk):
    """Draft the letter for every case on the preparing step that has none (or only the ticked ones)."""
    prop = association_only_or_404(pk)
    redirect_to = books_or_redirect(request, prop)
    if redirect_to:
        return redirect_to
    if request.method != 'POST':
        return redirect(reverse('accounting:collections', args=[pk]) + '?tab=preparing')
    ticked = [int(i) for i in request.POST.getlist('case') if i.isdigit()]
    cases = DelinquencyCase.objects.filter(association=prop, stage=Stage.PREPARING, resolved_on__isnull=True).select_related('unit', 'owner', 'prep')
    if ticked:
        cases = cases.filter(pk__in=ticked)
    made = 0
    for case in cases:
        prep = letter_prep._get(case)
        if prep is not None and prep.letter_doc_id:
            continue
        try:
            letter_prep.draft_missing(case, request.user, asof=parse_date(request.POST.get('asof')))
            made += 1
        except (letter_prep.PrepError, ValueError) as e:
            messages.error(request, f'Unit {case.unit.label}: {e}')
    messages.success(request, f'Drafted {made} letter{"s" if made != 1 else ""}.' if made else 'Every case already has a letter.')
    return redirect(reverse('accounting:collections', args=[pk]) + '?tab=preparing')


@admin_only
def prep_pdf(request, pk, kind):
    """One PDF with every letter, affidavit or envelope of the ticked cases (all the cases on the preparing step when none are ticked). Printing does not mark anything printed."""
    prop = association_only_or_404(pk)
    if kind not in ('letter', 'affidavit', 'envelope'):
        raise Http404
    ids = [int(i) for i in request.GET.getlist('case') if i.isdigit()]
    cases = DelinquencyCase.objects.filter(association=prop, stage=Stage.PREPARING, resolved_on__isnull=True).select_related('unit', 'owner', 'prep', 'prep__letter_doc', 'prep__affidavit_doc').order_by('unit__label')
    if ids:
        cases = cases.filter(pk__in=ids)
    cases = list(cases)
    try:
        if kind == 'letter':
            data = letter_prep.letters_pdf(cases)
        elif kind == 'affidavit':
            data = letter_prep.affidavits_pdf(cases)
        else:
            data = letter_prep.envelopes_pdf(prop, cases)
    except letter_prep.PrepError as e:
        messages.error(request, str(e))
        return redirect(reverse('accounting:collections', args=[pk]) + '?tab=preparing')
    response = HttpResponse(data, content_type='application/pdf')
    response['Content-Disposition'] = f'inline; filename="late-{kind}s-{prop.pk}.pdf"'
    return response
