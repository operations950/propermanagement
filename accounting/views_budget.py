"""The budget screens: the list of associations for a year, and one association's budget through its four stages."""
import json
from decimal import Decimal

from django.contrib import messages
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import budget_export
from . import budgeting as bg
from . import closing, ledger
from .models import ZERO, Account, BudgetVersionLine
from .web import admin_only, association_or_404, books_or_redirect, parse_money


def default_year(today=None):
    """From July on we are working on next year's budget."""
    today = today or timezone.localdate()
    return today.year + 1 if today.month >= 7 else today.year


def _year(raw, fallback):
    try:
        y = int(raw)
        return y if 2000 <= y <= 2100 else fallback
    except (TypeError, ValueError):
        return fallback


@admin_only
def budgets(request):
    year = _year(request.GET.get('year'), default_year())
    rows = []
    for prof in closing.associations():
        prop = prof.property
        info = bg.base_info(prop, year)
        bud = bg.get_budget(prop, year)
        have = bg.versions(bud)
        latest = next((k for k in reversed(bg.STAGE_KEYS) if k in have), None)
        if latest == 'approved':
            status, label, detail = 'done', 'Complete', 'Approved and locked'
        elif latest:
            status, label, detail = 'attention', bg.STAGE_LABEL[latest], bg.STAGE_HINT[latest]
        else:
            status, label = '', 'No budget yet'
            detail = f'No budget in the system for {year}.'
        rows.append({'assoc': prop, 'status': status, 'label': label, 'detail': detail, 'closed': bg.closed_note(info), 'can_start': bg.can_start(info)})
    return render(request, 'accounting/budgets.html', {'rows': rows, 'year': year, 'previous': year - 1, 'next': year + 1, 'done': sum(1 for r in rows if r['status'] == 'done')})


def _chg(new, old):
    if not old:
        return None if new else 0.0
    return float((Decimal(str(new)) / Decimal(str(old)) - 1) * 100)


def _tone(d, income=False, eps=0.5):
    """Whether a change is good for the owners: green for good, red for bad. A drop in an expense (or in what the assessments must bring in) is good; more income is good."""
    if abs(d) < eps:
        return ''
    return 'good' if (d > 0) == income else 'bad'


def _sheet(version, info, units):
    """The board sheet: no inputs. Last year's budget, where last year will land, and next year's budget, with the change from last year's approved budget. Other income comes first;
    the assessments needed are the expenses and reserves less that income."""
    rows = bg.line_rows(version, info)
    t = bg.totals(rows, units)
    year = version.budget.year

    def pack(prior, projected, proposed, income=False):
        d, p = proposed - prior, _chg(proposed, prior)
        return {'prior': prior, 'projected': projected, 'proposed': proposed, 'd': d, 'p': p, 'tone': _tone(d, income), 'ptone': _tone(d, income)}

    A = bg.assessment_row(version, info, t['needed'])
    sections = []
    for s_ in bg.SECTIONS:
        inc = s_ == 'Income'
        lines = [r for r in rows if r['section'] == s_ and (abs(r['prior']) > 1 or abs(r['proposed']) > 1)]       # a line of $1 or less in both budgets is left off, whatever its actuals
        tot = dict(t['sections'][s_])
        packed = [dict(pack(r['prior'], r['projected'], r['proposed'], inc), name=r['name']) for r in lines]
        if inc:                  # the assessments come first, the way a P&L starts: what the budget needs them to bring in
            packed.insert(0, dict(pack(A['prior'], A['projected'], A['proposed'], True), name='Regular assessments'))
            tot = {'prior': tot['prior'] + A['prior'], 'projected': tot['projected'] + A['projected'], 'proposed': tot['proposed'] + A['proposed']}
        sections.append(dict(pack(tot['prior'], tot['projected'], tot['proposed'], inc), name='Income' if inc else 'Reserve contributions' if s_ == 'Reserves' else s_, key=s_, income=inc, lines=packed))
    groups, basis = bg.unit_groups(version.budget.association)
    calc = []
    for g in groups:
        f = g['fraction']
        rows_ = []
        for label, mult, bold in (('Monthly assessment', 1, True), ('Annual per unit', 12, False)):
            old, new = t['needed_prior'] * f / 12 * mult, t['needed'] * f / 12 * mult
            d, p = new - old, _chg(new, old)
            tone = _tone(d, False, 0.005)
            rows_.append({'label': label, 'old': old, 'new': new, 'd': d, 'p': p, 'tone': tone, 'ptone': tone, 'bold': bold})
        calc.append({'header': g['label'], 'units': g['units'], 'rows': rows_})
    basis_note = 'Every unit pays the same.' if basis == 'equal' else "Split by each unit's share of the common expenses."
    return {'year': year, 'proj_word': 'projected' if info['remaining'] else 'actual', 'sections': sections, 'op': pack(t['op']['prior'], t['op']['projected'], t['op']['proposed']),
            'total': pack(t['total_prior'], t['total_projected'], t['total']), 'needed': pack(t['needed_prior'], t['needed_projected'], t['needed']),
            'calc': calc, 'basis_note': basis_note, 'units': units}


def _compare(have, units):
    keys, rows = bg.compare_rows(have, units)
    tot = {}
    for k in keys:
        tot[k] = bg.totals(bg.line_rows(have[k], bg.info_for(have[k])), units)
    sections = []
    for s in bg.SECTIONS:
        lines = []
        for r in rows:
            if r['section'] != s:
                continue
            cells = []
            for i, k in enumerate(keys):
                cells.append({'v': r['v'][k], 'd': (r['v'][k] - r['v'][keys[i - 1]]) if i else None})
            lines.append({'name': f'{r["account"].name}', 'prior': r['prior'], 'cells': cells, 'changed': r['changed'], 'why': r['why']})
        if lines:
            sections.append({'name': 'Reserve contributions' if s == 'Reserves' else s, 'lines': lines})
    def trow(label, getter, money2=False):
        cells = []
        for i, k in enumerate(keys):
            v = getter(tot[k])
            cells.append({'v': v, 'd': (v - getter(tot[keys[i - 1]])) if i else None})
        return {'label': label, 'prior': getter(tot[keys[0]], True), 'cells': cells, 'money2': money2}
    totals = [
        trow('Operating expenses', lambda t, prior=False: t['op']['prior' if prior else 'proposed']),
        trow('Reserves', lambda t, prior=False: t['rs']['prior' if prior else 'proposed']),
        trow('Other income', lambda t, prior=False: t['oi']['prior' if prior else 'proposed']),
        trow('Total assessments needed', lambda t, prior=False: t['needed_prior' if prior else 'needed']),
        trow('Per unit per month', lambda t, prior=False: t['unit_month_prior' if prior else 'unit_month'], True),
    ]
    totals = [r for r in totals if r['label'] != 'Other income' or r['prior'] or any(c['v'] for c in r['cells'])]
    return {'keys': keys, 'labels': [bg.STAGE_LABEL[k] for k in keys], 'sections': sections, 'totals': totals, 'cols': 2 * len(keys) + 2}


@admin_only
def budget_year(request, pk, year):
    prop = association_or_404(pk)
    redirect_to = books_or_redirect(request, prop)
    if redirect_to:
        return redirect_to
    info = bg.base_info(prop, year)
    bud = bg.get_budget(prop, year)
    here = reverse('accounting:budget_year', args=[pk, year])
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'start':
                bg.start_draft(prop, year, request.user)
                messages.success(request, f'The {year} budget is started.')
            elif action == 'advance' and bud:
                ver = bg.advance(bud, request.user)
                messages.success(request, f'{bg.STAGE_LABEL[ver.stage]} is ready.' if ver.stage != 'approved' else f'The {year} budget is approved and locked.')
                return redirect(here + f'?stage={ver.stage}')
            elif action == 'refresh' and bud:
                have_now = bg.versions(bud)
                ver = have_now.get(request.POST.get('stage'))
                if ver is None or not bg.is_editable(ver, have_now):
                    raise ledger.LedgerError('Only a working version can be brought up to date.')
                bg.refresh_actuals(ver)
                messages.success(request, f'Actuals now run through {bg.info_for(ver)["through"]:%B %Y}. Your methods, adjustments and reasons are unchanged.')
                return redirect(here + f'?stage={ver.stage}')
            elif action == 'reopen' and bud:
                bg.reopen(bud)
                messages.success(request, 'The approved budget is open again as the Meeting final.')
                return redirect(here + '?stage=meeting')
        except ledger.LedgerError as e:
            messages.error(request, str(e))
        return redirect(here)
    have = bg.versions(bud)
    ctx = {'assoc': prop, 'year': year, 'info': info, 'closed': bg.closed_note(info), 'acc_back': reverse('accounting:budgets') + f'?year={year}', 'acc_back_label': 'Budgets',
           'stages': [{'key': k, 'label': bg.STAGE_LABEL[k]} for k in bg.STAGE_KEYS if k in have], 'have': have, 'units': bg.unit_count(prop)}
    if not have:
        ctx['view'] = 'none'
        ctx['can_start'] = bg.can_start(info)
        return render(request, 'accounting/budget_year.html', ctx)
    latest = next(k for k in reversed(bg.STAGE_KEYS) if k in have)
    stage = request.GET.get('stage') or latest
    if stage != 'compare' and stage not in have:
        stage = latest
    ctx['stage'] = stage
    ctx['stage_label'] = 'Compare stages' if stage == 'compare' else bg.STAGE_LABEL[stage]
    ctx['hint'] = 'Everything side by side: what we proposed, what the board came back with, what changed at the meeting.' if stage == 'compare' else bg.STAGE_HINT[stage]
    ctx['latest'] = latest
    ctx['next_label'] = bg.NEXT_LABEL.get(latest) if stage == latest and latest != 'approved' else ''
    if stage != 'compare' and stage in have:
        bg.heal(have[stage], have)       # a draft made before the months ahead of our books counted as actual: bring it up to date, nothing typed in changes
    sheet_view = stage == 'approved' or (stage in have and request.GET.get('sheet'))
    ctx['sheet_on'] = bool(sheet_view) and stage != 'approved'
    export = request.GET.get('export')
    if stage != 'compare' and stage in have and export in ('pdf', 'xlsx'):
        ver = have[stage]
        sheet = _sheet(ver, bg.info_for(ver), ctx['units'])
        sheet.update({'assoc': prop.name, 'stage_label': bg.STAGE_LABEL[stage], 'on': timezone.localdate()})
        name = budget_export.file_name(prop.name, year, bg.STAGE_LABEL[stage], export)
        data = budget_export.pdf(sheet) if export == 'pdf' else budget_export.xlsx(sheet)
        resp = HttpResponse(data, content_type='application/pdf' if export == 'pdf' else 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        resp['Content-Disposition'] = f'attachment; filename="{name}"'
        return resp
    if stage == 'compare':
        ctx['view'] = 'compare'
        ctx['cmp'] = _compare({k: v for k, v in have.items() if k != 'approved'}, ctx['units'])
    elif sheet_view:
        ctx['view'] = 'approved'
        ctx['sheet'] = _sheet(have[stage], bg.info_for(have[stage]), ctx['units'])
        ctx['sheet_note'] = 'approved' if stage == 'approved' else f'{bg.STAGE_LABEL[stage].lower()} for the board'
        ctx['approved_at'] = have[stage].created_at
        ctx['downloads'] = [('pdf', 'PDF'), ('xlsx', 'Excel')]
    else:
        ctx['view'] = 'editor'
        ctx['state_json'] = bg.editor_state(have[stage], have)
        ctx['edit_url'] = reverse('accounting:budget_edit', args=[pk, year]) + f'?stage={stage}'
        ctx['info_rest'] = bg.info_for(have[stage])
        ctx['detail_url'] = reverse('accounting:budget_detail', args=[pk, year]) + f'?stage={stage}'
        ctx['can_refresh'] = bg.is_stale(have[stage]) and bg.is_editable(have[stage], have)
    if latest == 'approved':
        ctx['can_reopen'] = True
    return render(request, 'accounting/budget_year.html', ctx)


@admin_only
def budget_detail(request, pk, year):
    """The entries behind one line's actual, for the drill-down on the working screen."""
    prop = association_or_404(pk)
    have = bg.versions(bg.get_budget(prop, year))
    ver = have.get(request.GET.get('stage'))
    line = ver.lines.select_related('account').filter(pk=request.GET.get('line')).first() if ver else None
    if line is None:
        return JsonResponse({'error': 'That line is gone. Reload the page.'}, status=404)
    return JsonResponse(bg.actual_detail(ver, line))


def _err(msg, status=400):
    return JsonResponse({'error': msg}, status=status)


@admin_only
@require_POST
def budget_edit(request, pk, year):
    """One change on a draft screen (a method, an adjustment, a note, add or remove a line). Answers with the whole recalculated screen."""
    prop = association_or_404(pk)
    bud = bg.get_budget(prop, year)
    have = bg.versions(bud)
    stage = request.GET.get('stage')
    ver = have.get(stage)
    if ver is None:
        return _err('That stage does not exist.', 404)
    if not bg.is_editable(ver, have):
        return _err('This version is locked: a later stage has been started from it.', 403)
    try:
        body = json.loads(request.body or '{}')
    except ValueError:
        return _err('Bad request.')
    info = bg.info_for(ver)
    op = body.get('op')
    if op == 'set':
        line = ver.lines.filter(pk=body.get('line')).first()
        if line is None:
            return _err('That line is gone. Reload the page.', 404)
        field, value = body.get('field'), body.get('value')
        if field == 'method':
            if value not in bg.METHODS or value == 'custom':
                return _err('Unknown method.')
            line.method = value
        elif field == 'custom':
            try:
                line.custom = parse_money(str(value))
            except ValueError as e:
                return _err(str(e))
            line.method = 'custom'
        elif field == 'adjust':
            try:
                bg.parse_adjust(str(value or ''))
            except ValueError as e:
                return _err(str(e))
            line.adjust = str(value or '').strip()[:20]
        elif field == 'note':
            line.note = str(value or '').strip()[:200]
        else:
            return _err('Unknown field.')
        bg.recompute(line, info)
        line.save()
    elif op == 'add':
        acct = Account.objects.filter(association=prop, pk=body.get('account')).first()
        if acct is None or not bg.is_budgetable(acct) or ver.lines.filter(account=acct).exists():
            return _err('That line cannot be added.')
        prior = bg.prior_budget(prop, year).get(acct.pk, ZERO)
        act = bg.actuals_for(prop, info, [acct])[0].get(acct.pk, ZERO)
        line = BudgetVersionLine(version=ver, account=acct, prior_budget=prior, actual=act, recent=ZERO, method='none' if bg.is_reserve(acct) else bg.default_method(acct))
        bg.recompute(line, info)
        line.save()
    elif op == 'remove':
        line = ver.lines.filter(pk=body.get('line')).first()
        if line is None or line.prior_budget or line.actual:
            return _err('Only a line added by hand, with nothing in last year, can be removed.')
        line.delete()
    else:
        return _err('Unknown change.')
    return JsonResponse(bg.editor_state(ver, have))
