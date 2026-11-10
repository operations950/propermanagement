"""The budget screens: the list of associations for a year, and one association's budget through its four stages."""
import json
from decimal import Decimal

from django.contrib import messages
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

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


def _sheet(version, info, units):
    """The approved budget as a clean sheet: no inputs, 2026 beside 2027 with the change."""
    rows = bg.line_rows(version, info)
    t = bg.totals(rows, units)
    sections = []
    for s in bg.SECTIONS:
        lines = [r for r in rows if r['section'] == s and (r['prior'] or abs(r['proposed']) >= 0.5)]
        tot = t['sections'][s]
        sections.append({'name': 'Reserve contributions' if s == 'Reserves' else s, 'key': s, 'lines': [dict(r, d=r['proposed'] - r['prior'], p=_chg(r['proposed'], r['prior'])) for r in lines],
                         'prior': tot['prior'], 'proposed': tot['proposed'], 'd': tot['proposed'] - tot['prior'], 'p': _chg(tot['proposed'], tot['prior'])})
    per = [
        {'label': 'Monthly assessment', 'old': t['unit_month_prior'], 'new': t['unit_month'], 'bold': True},
        {'label': 'Annual per unit', 'old': t['unit_month_prior'] * 12, 'new': t['unit_year']},
        {'label': 'Of that, operating (monthly)', 'old': t['op']['prior'] / (units or 1) / 12, 'new': t['op']['proposed'] / (units or 1) / 12},
        {'label': 'Of that, reserves (monthly)', 'old': t['rs']['prior'] / (units or 1) / 12, 'new': t['rs']['proposed'] / (units or 1) / 12},
    ]
    for p in per:
        p['d'], p['p'] = p['new'] - p['old'], _chg(p['new'], p['old'])
    return {'sections': sections, 'op': {'prior': t['op']['prior'], 'proposed': t['op']['proposed'], 'd': t['op']['proposed'] - t['op']['prior'], 'p': _chg(t['op']['proposed'], t['op']['prior'])},
            'total': {'prior': t['total_prior'], 'proposed': t['total'], 'd': t['total'] - t['total_prior'], 'p': _chg(t['total'], t['total_prior'])}, 'per': per}


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
        trow('Total assessments needed', lambda t, prior=False: t['total_prior' if prior else 'total']),
        trow('Per unit per month', lambda t, prior=False: t['unit_month_prior' if prior else 'unit_month'], True),
    ]
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
    if stage == 'compare':
        ctx['view'] = 'compare'
        ctx['cmp'] = _compare({k: v for k, v in have.items() if k != 'approved'}, ctx['units'])
    elif stage == 'approved':
        ctx['view'] = 'approved'
        ctx['sheet'] = _sheet(have['approved'], bg.info_for(have['approved']), ctx['units'])
        ctx['approved_at'] = have['approved'].created_at
    else:
        ctx['view'] = 'editor'
        ctx['state_json'] = bg.editor_state(have[stage], have)
        ctx['edit_url'] = reverse('accounting:budget_edit', args=[pk, year]) + f'?stage={stage}'
        ctx['info_rest'] = bg.info_for(have[stage])
    if latest == 'approved':
        ctx['can_reopen'] = True
    return render(request, 'accounting/budget_year.html', ctx)


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
        act = bg._actuals(prop, info['window_start'], info['window_end'], [acct]).get(acct.pk, ZERO) if info['n'] else ZERO
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
