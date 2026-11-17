"""The budget screens: the list of associations for a year, and one association's budget through its four stages."""
import json
from decimal import ROUND_HALF_UP, Decimal

from django.contrib import messages
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import budget_export
from . import budget_project as bp
from . import budgeting as bg
from . import closing, ledger
from .models import ZERO, Account, BudgetVersionLine
from .web import admin_only, association_or_404, books_or_redirect, parse_date, parse_money, acc_up


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
        sm = bp.summary(prop, year, bud, have, bp.get_project(bud))
        if sm['next'] is None:                                       # all seven steps done
            status, label, detail = 'done', 'Complete', f'Loaded into the system for {year}'
        elif latest:
            status, label, detail = 'attention', f'Step {sm["next"]["n"]} of {sm["of"]}', sm['next']['title']
        rows.append({'assoc': prop, 'status': status, 'label': label, 'detail': detail, 'closed': bg.closed_note(info), 'can_start': bg.can_start(info), 'steps_done': sm['done'], 'steps': sm['of']})
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


COLS = ('prior', 'projected', 'proposed')


def _r(x, cents):
    """A figure as the sheet shows it: whole dollars (halves up) or cents."""
    return Decimal(str(x)).quantize(Decimal('0.01') if cents else Decimal('1'), rounding=ROUND_HALF_UP)


def _on_sheet(r, cents):
    """Whole dollars: a line of $1 or less in both budgets is left off, whatever its actuals. Cents: a line is left off only when all three figures are nil, so the totals still add up to the cent."""
    if cents:
        return any(abs(r[k]) >= 0.005 for k in COLS)
    return abs(r['prior']) > 1 or abs(r['proposed']) > 1


def _sheet(version, info, units, cents=False):
    """The board sheet: no inputs. Last year's budget, where last year will land, and next year's budget, with the change from last year's approved budget. Other income comes first;
    the assessments needed are the expenses and reserves less that income.

    Every figure on the sheet is either one line rounded (to the dollar, or to the cent when `cents`) or a sum of lines as shown: a section's total is the sum of the lines under it, the
    expenses with reserves are the sum of the sections, and the regular assessments are those expenses less the other income shown, so the page always adds up and the total income
    equals the total expenses with reserves. A change is the difference of the two figures beside it."""
    rows = bg.line_rows(version, info)
    t = bg.totals(rows, units)
    year = version.budget.year
    zero = Decimal('0')

    def pack(prior, projected, proposed, income=False):
        d, p = proposed - prior, _chg(proposed, prior)
        return {'prior': float(prior), 'projected': float(projected), 'proposed': float(proposed), 'd': float(d), 'p': p, 'tone': _tone(d, income), 'ptone': _tone(d, income)}

    def total_of(items):
        return {c: sum((v[c] for _, v in items), zero) for c in COLS}

    shown = {s_: [(r['name'], {c: _r(r[c], cents) for c in COLS}) for r in rows if r['section'] == s_ and _on_sheet(r, cents)] for s_ in bg.SECTIONS}
    sums = {s_: total_of(shown[s_]) for s_ in bg.SECTIONS}
    op = {c: sum((sums[s_][c] for s_ in bg.EXPENSE_SECTIONS), zero) for c in COLS}
    exp = {c: op[c] + sums['Reserves'][c] for c in COLS}                       # expenses with reserves
    oi = sums['Income']
    A = bg.assessment_row(version, info, t['needed'])
    reg = {'prior': _r(A['prior'], cents), 'projected': _r(A['projected'], cents), 'proposed': exp['proposed'] - oi['proposed']}      # next year's assessments: what the budget needs less the other income
    sections = []
    for s_ in bg.SECTIONS:
        inc = s_ == 'Income'
        items = list(shown[s_])
        tot = dict(sums[s_])
        if inc:                  # the assessments come first, the way a P&L starts: what the budget needs them to bring in
            items.insert(0, ('Regular assessments', reg))
            tot = {c: tot[c] + reg[c] for c in COLS}
        packed = [dict(pack(v['prior'], v['projected'], v['proposed'], inc), name=name) for name, v in items]
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
    return {'year': year, 'proj_word': 'projected' if info['remaining'] else 'actual', 'sections': sections, 'op': pack(op['prior'], op['projected'], op['proposed']),
            'total': pack(exp['prior'], exp['projected'], exp['proposed']), 'needed': pack(exp['prior'] - oi['prior'], exp['projected'] - oi['projected'], reg['proposed']),
            'calc': calc, 'basis_note': basis_note, 'units': units, 'cents': cents}



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
            elif action == 'rounding' and bud:          # how the board sheet, its PDF and its Excel show the numbers; nothing in the budget itself changes
                bud.show_cents = request.POST.get('mode') == 'cents'
                bud.save(update_fields=['show_cents'])
                back = request.POST.get('stage')
                return redirect(here + (f'?stage={back}' + ('&sheet=1' if request.POST.get('sheet') else '') if back in bg.STAGE_KEYS else ''))
        except ledger.LedgerError as e:
            messages.error(request, str(e))
        return redirect(here)
    have = bg.versions(bud)
    ctx = {'assoc': prop, 'year': year, 'info': info, 'closed': bg.closed_note(info), **acc_up(request, prop, ['accounting:budget_project', 'accounting:budgets'], reverse('accounting:budget_project', args=[pk, year]), f'{year} budget project'),
           'stages': [{'key': k, 'label': bg.STAGE_LABEL[k]} for k in bg.STAGE_KEYS if k in have], 'have': have, 'units': bg.unit_count(prop), 'cents': bool(bud and bud.show_cents)}
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
    if stage not in ('compare', 'meeting') and stage in have:      # at the meeting the numbers are fixed: nothing is brought up to date under the board's feet
        bg.heal(have[stage], have)       # a draft made before the months ahead of our books counted as actual: bring it up to date, nothing typed in changes
    sheet_view = stage == 'approved' or (stage in have and request.GET.get('sheet'))
    ctx['sheet_on'] = bool(sheet_view) and stage != 'approved'
    export = request.GET.get('export')
    if stage != 'compare' and stage in have and export in ('pdf', 'xlsx'):
        ver = have[stage]
        sheet = _sheet(ver, bg.info_for(ver), ctx['units'], ctx['cents'])
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
        ctx['sheet'] = _sheet(have[stage], bg.info_for(have[stage]), ctx['units'], ctx['cents'])
        ctx['sheet_note'] = 'approved' if stage == 'approved' else f'{bg.STAGE_LABEL[stage].lower()} for the board'
        ctx['approved_at'] = have[stage].created_at
        ctx['downloads'] = [('pdf', 'PDF'), ('xlsx', 'Excel')]
    elif stage == 'meeting':             # the meeting: last year, the board draft, what changed, and one box to type the final number in
        ctx['view'] = 'meeting'
        ctx['focus'] = bool(request.GET.get('focus'))        # run the meeting: the menus out of the way, the whole width for the numbers
        ctx['state_json'] = bg.editor_state(have[stage], have)
        ctx['edit_url'] = reverse('accounting:budget_edit', args=[pk, year]) + f'?stage={stage}'
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
    if op == 'assess_projected':             # where this year's regular assessments will land: typed by hand; blank goes back to the worked-out figure
        text = '' if body.get('value') is None else str(body.get('value')).strip()
        if text == '':
            ver.assess_projected = None
        else:
            try:
                ver.assess_projected = parse_money(text)
            except ValueError as e:
                return _err(str(e))
        ver.save(update_fields=['assess_projected'])
    elif op == 'set':
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
        elif field == 'final':           # the meeting screen: the final number itself, not a change to the old one; blank puts the board draft's number back
            if ver.stage != 'meeting':
                return _err('A final number is typed on the Meeting final.')
            text = '' if value is None else str(value).strip()
            if text == '':
                line.meeting_amount = None
            else:
                try:
                    amount = parse_money(text)
                except ValueError as e:
                    return _err(str(e))
                prev = have.get(bg.STAGE_KEYS[bg.STAGE_KEYS.index(ver.stage) - 1])
                board = next((l.proposed for l in prev.lines.all() if l.account_id == line.account_id), None) if prev else None
                line.meeting_amount = None if board is not None and amount == board else amount        # the same as the board draft is not a change
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
        if ver.stage == 'meeting':            # a line added at the meeting starts at nothing: the number is whatever is typed for it
            line.method, line.custom = 'custom', ZERO
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


# ---------------------------------------------------------------- the budget project: the steps, the mailing packet, loading the budget for its year

def _stage_pdf(prop, bud, have, stage):
    """The board sheet of one stage as a PDF."""
    ver = have[stage]
    sheet = _sheet(ver, bg.info_for(ver), bg.unit_count(prop), bool(bud.show_cents))
    sheet.update({'assoc': prop.name, 'stage_label': bg.STAGE_LABEL[stage], 'on': timezone.localdate()})
    return budget_export.pdf(sheet)


def _pdf_response(data, name, inline=True):
    resp = HttpResponse(data, content_type='application/pdf')
    resp['Content-Disposition'] = f'{"inline" if inline else "attachment"}; filename="{name}"'
    return resp


def _clean_name(text):
    return ''.join(ch if ch.isalnum() or ch in ' -_' else '' for ch in text).strip().replace(' ', '_')


def _project_post(request, prop, year, bud, have, project):
    """One button on the project page. Raises ProjectError or LedgerError with the reason it cannot be done."""
    action = request.POST.get('action')
    user = request.user
    if action == 'start':
        bg.start_draft(prop, year, user)
        messages.success(request, f'The {year} budget is started.')
        return
    if bud is None:
        raise bp.ProjectError(f'Start the {year} budget first.')
    project = bp.ensure_project(bud)
    if action == 'details':
        project.meeting_date = parse_date(request.POST.get('meeting_date'))
        project.meeting_time = (request.POST.get('meeting_time') or '').strip()[:40]
        project.meeting_place = (request.POST.get('meeting_place') or '').strip()[:200]
        project.save(update_fields=['meeting_date', 'meeting_time', 'meeting_place'])
        messages.success(request, 'The meeting details are saved.')
        if project.notice_doc_id:
            messages.warning(request, 'The notice was made with the old details. Make a new one on the packet page before you mail it.')
    elif action == 'to_board':
        if 'draft' not in have or 'board' in have:
            raise bp.ProjectError('The Board draft is already started.' if 'board' in have else 'There is no draft yet.')
        bg.advance(bud, user)
        messages.success(request, 'The Board draft is ready.')
    elif action == 'final':
        if 'board' not in have:
            raise bp.ProjectError('There is no Board draft yet.')
        project.final_at = timezone.now()
        project.save(update_fields=['final_at'])
        messages.success(request, 'The Board draft is marked final for mailing.')
    elif action == 'unfinal':
        project.final_at = None
        project.save(update_fields=['final_at'])
        messages.success(request, 'The Board draft is open again.')
    elif action == 'mailed':
        if not bp.meeting_ready(project):
            raise bp.ProjectError('Enter the date, time and place of the meeting first.')
        on = parse_date(request.POST.get('mailed_on'))
        if on is None:
            raise bp.ProjectError('Enter the date it was mailed.')
        posted = parse_date(request.POST.get('posted_on'))
        count = request.POST.get('count')
        count = int(count) if (count or '').strip().isdigit() else None
        bp.record_mailing(prop, bud, project, user, on, request.POST.get('method') or '', (request.POST.get('by') or '').strip(), (request.POST.get('title') or '').strip(), posted, count)
        n = bp.days_before(project)
        messages.success(request, 'The mailing is recorded and the affidavit is made.')
        if n is not None and n < 14:
            messages.warning(request, f'That is {n} day{"s" if n != 1 else ""} before the meeting. The statute asks for not less than 14.')
    elif action == 'to_meeting':
        if 'board' not in have or 'meeting' in have:
            raise bp.ProjectError('The meeting version is already made.' if 'meeting' in have else 'There is no Board draft yet.')
        if not project.mailed_on:
            raise bp.ProjectError('Record the mailing first: preparing the meeting version freezes the Board draft that was mailed.')
        bg.advance(bud, user)
        messages.success(request, 'The meeting version is ready.')
    elif action == 'held':
        if 'meeting' not in have:
            raise bp.ProjectError('Prepare the meeting version first.')
        project.held_on = parse_date(request.POST.get('held_on')) or timezone.localdate()
        project.save(update_fields=['held_on'])
        messages.success(request, 'The meeting is marked over.')
    elif action == 'lock':
        if 'meeting' not in have or 'approved' in have:
            raise bp.ProjectError('The budget is already approved.' if 'approved' in have else 'Prepare the meeting version first.')
        if not project.held_on:
            raise bp.ProjectError('Mark the meeting over first.')
        bg.advance(bud, user)
        messages.success(request, f'The {year} budget is approved and locked.')
    elif action == 'reopen':
        bg.reopen(bud)
        project.loaded_at, project.loaded_note = None, ''
        project.save(update_fields=['loaded_at', 'loaded_note'])
        messages.success(request, 'The approved budget is open again as the Meeting final.')
    else:
        raise bp.ProjectError('Unknown step.')


@admin_only
def budget_project(request, pk, year):
    """The steps of one association's budget for a year, each with its status and its button."""
    prop = association_or_404(pk)
    redirect_to = books_or_redirect(request, prop)
    if redirect_to:
        return redirect_to
    bud = bg.get_budget(prop, year)
    have = bg.versions(bud)
    project = bp.get_project(bud)
    here = reverse('accounting:budget_project', args=[pk, year])
    if request.method == 'POST':
        try:
            _project_post(request, prop, year, bud, have, project)
        except (ledger.LedgerError, bp.ProjectError) as e:
            messages.error(request, str(e))
        return redirect(here)
    info = bg.base_info(prop, year)
    steps = bp.step_list(prop, year, bud, have, project)
    for s in steps:
        s['chip'], s['chip_label'] = bp.chip(s['status'])
    live_rows = bp.mailing_rows(prop) if project and project.notice_doc_id and not project.mailed_on else []
    today = timezone.localdate()
    ctx = {'assoc': prop, 'year': year, 'steps': steps, 'have': have, 'project': project, 'info': info, 'closed': bg.closed_note(info), 'can_start': bg.can_start(info), 'units': bg.unit_count(prop),
           **acc_up(request, prop, ['accounting:budgets', 'accounting:hub'], reverse('accounting:budgets') + f'?year={year}', 'Budgets'), 'today': today, 'methods': bp.MAIL_METHODS, 'default_by': (project.mailed_by if project and project.mailed_by else bp.DEFAULT_AFFIANT),
           'default_title': bp.DEFAULT_TITLE, 'live_count': len(bp.deliverable(live_rows)) if live_rows else None, 'meeting_ready': bp.meeting_ready(project),
           'days_before': bp.days_before(project), 'changed': bp.changed_since_notice(prop, project, have), 'by_key': {s['key']: s for s in steps},
           'draft_url': reverse('accounting:budget_year', args=[pk, year]) + '?stage=draft', 'board_url': reverse('accounting:budget_year', args=[pk, year]) + '?stage=board',
           'meeting_url': reverse('accounting:budget_year', args=[pk, year]) + '?stage=meeting&focus=1', 'approved_url': reverse('accounting:budget_year', args=[pk, year]) + '?stage=approved',
           'packet_url': reverse('accounting:budget_packet', args=[pk, year]), 'load_url': reverse('accounting:budget_load', args=[pk, year])}
    return render(request, 'accounting/budget_project.html', ctx)


@admin_only
def budget_packet(request, pk, year):
    """The final draft for mailing: the notice, the budget, the mailing list, the envelopes, the affidavit; each to print."""
    prop = association_or_404(pk)
    redirect_to = books_or_redirect(request, prop)
    if redirect_to:
        return redirect_to
    bud = bg.get_budget(prop, year)
    have = bg.versions(bud)
    project = bp.get_project(bud)
    back = reverse('accounting:budget_project', args=[pk, year])
    if 'board' not in have:
        messages.error(request, 'The packet is made from the Board draft, which is not started yet.')
        return redirect(back)
    here = reverse('accounting:budget_packet', args=[pk, year])
    if request.method == 'POST':
        project = bp.ensure_project(bud)
        action = request.POST.get('action')
        try:
            if action == 'return':
                project.return_address = '\n'.join(l.strip() for l in (request.POST.get('return_address') or '').splitlines() if l.strip())[:600]
                project.envelope_size = 'full' if request.POST.get('envelope_size') == 'full' else 'no10'
                project.save(update_fields=['return_address', 'envelope_size'])
                messages.success(request, 'Saved.')
            elif action == 'details':                # the date, time and place of the meeting, right here (the notice needs them)
                project.meeting_date = parse_date(request.POST.get('meeting_date'))
                project.meeting_time = (request.POST.get('meeting_time') or '').strip()[:40]
                project.meeting_place = (request.POST.get('meeting_place') or '').strip()[:200]
                project.save(update_fields=['meeting_date', 'meeting_time', 'meeting_place'])
                messages.success(request, 'The meeting details are saved.')
                if project.notice_doc_id:
                    messages.warning(request, 'The notice was made with the old details. Make a new one before you mail it.')
            elif action == 'notice':
                atype = request.POST.get('association_type')
                bp.make_notice(prop, bud, project, request.user, have, atype if atype in ('Condominium', 'Homeowners association') else None)
                messages.success(request, 'The notice is made.')
            else:
                raise bp.ProjectError('Unknown button.')
        except (ledger.LedgerError, bp.ProjectError) as e:
            messages.error(request, str(e))
        return redirect(here)
    rows = bp.rows_for_packet(prop, project)
    sent = bp.deliverable(rows)
    flags = bp.flagged(rows)
    t = bp.proposed_totals(prop, have['board'])
    ctx = {'assoc': prop, 'year': year, 'project': project, 'rows': rows, 'sent': len(sent), 'flags': flags, 'owner_total': len(rows), 'meeting_ready': bp.meeting_ready(project), 'needed': t['needed'],
           'unit_month': t['unit_month'], 'changed': bp.changed_since_notice(prop, project, have), **acc_up(request, prop, ['accounting:budget_project'], back, f'{year} budget project'),
           'notice': project.notice_doc if project else None, 'affidavit': project.affidavit_doc if project else None, 'from_list': bool(project and project.mailed_on and project.mailing_snapshot),
           'return_text': '\n'.join(bp.return_lines(prop, project)), 'envelope_size': project.envelope_size if project else 'no10', 'association_type': bp.last_association_type(prop)}
    ctx['files'] = {w: reverse('accounting:budget_packet_file', args=[pk, year, w]) for w in ('notice', 'budget', 'affidavit', 'mailing_list', 'mailing_csv', 'envelopes', 'everything')}
    return render(request, 'accounting/budget_packet.html', ctx)


@admin_only
def budget_packet_file(request, pk, year, what):
    """One piece of the packet, as a file: opens in the browser so it can be printed from there."""
    from legalforms import pdf as lf_pdf
    prop = association_or_404(pk)
    bud = bg.get_budget(prop, year)
    have = bg.versions(bud)
    project = bp.get_project(bud)
    if 'board' not in have:
        raise Http404('No Board draft.')
    base = _clean_name(f'{prop.name} {year} budget')
    need_notice = what in ('notice', 'everything')
    if need_notice and not (project and project.notice_doc_id):
        raise Http404('The notice has not been made yet.')
    if what == 'notice':
        return _pdf_response(lf_pdf.document_pdf(project.notice_doc), f'{base}_notice.pdf')
    if what == 'affidavit':
        if not (project and project.affidavit_doc_id):
            raise Http404('The affidavit is made when the mailing is recorded.')
        return _pdf_response(lf_pdf.document_pdf(project.affidavit_doc), f'{base}_affidavit_of_mailing.pdf')
    if what == 'budget':
        return _pdf_response(_stage_pdf(prop, bud, have, 'board'), f'{base}_proposed_budget.pdf')
    rows = bp.rows_for_packet(prop, project)
    if what == 'mailing_list':
        return _pdf_response(bp.mailing_list_pdf(prop, year, rows), f'{base}_mailing_list.pdf')
    if what == 'mailing_csv':
        resp = HttpResponse(bp.mailing_csv(rows), content_type='text/csv; charset=utf-8')
        resp['Content-Disposition'] = f'attachment; filename="{base}_mailing_list.csv"'
        return resp
    if what == 'envelopes':
        size = request.GET.get('size') or (project.envelope_size if project else 'no10')
        test = bool(request.GET.get('test'))
        return _pdf_response(bp.envelopes_pdf(prop, rows, project, 'full' if size == 'full' else 'no10', test), f'{base}_envelopes{"_test" if test else ""}.pdf')
    if what == 'everything':
        parts = [lf_pdf.document_pdf(project.notice_doc), _stage_pdf(prop, bud, have, 'board'), bp.mailing_list_pdf(prop, year, rows)]
        if project.affidavit_doc_id:
            parts.append(lf_pdf.document_pdf(project.affidavit_doc))
        return _pdf_response(bp.merge_pdfs(parts), f'{base}_everything.pdf')
    raise Http404('Unknown file.')


@admin_only
def budget_load(request, pk, year):
    """Step 7: what loading the locked budget for its year will do, in numbers, and the button that does it."""
    prop = association_or_404(pk)
    redirect_to = books_or_redirect(request, prop)
    if redirect_to:
        return redirect_to
    bud = bg.get_budget(prop, year)
    have = bg.versions(bud)
    back = reverse('accounting:budget_project', args=[pk, year])
    if bud is None or 'approved' not in have:
        messages.error(request, 'Approve and lock the budget first.')
        return redirect(back)
    project = bp.get_project(bud)           # looking at the preview records nothing
    if request.method == 'POST':
        project = bp.ensure_project(bud)
        if not request.POST.get('confirm'):
            messages.error(request, 'Tick the box to confirm, then press Load.')
            return redirect(request.path)
        try:
            did = bp.load_apply(prop, bud, have, project, request.user, bool(request.POST.get('do_assessment')), bool(request.POST.get('do_reserve')))
            messages.success(request, 'Loaded: ' + '; '.join(did) + '.')
            return redirect(back)
        except (ledger.LedgerError, bp.ProjectError) as e:
            messages.error(request, str(e))
        return redirect(request.path)
    plan = bp.load_plan(prop, bud, have)
    return render(request, 'accounting/budget_load.html', {'assoc': prop, 'year': year, 'plan': plan, 'project': project, **acc_up(request, prop, ['accounting:budget_project'], back, f'{year} budget project')})
