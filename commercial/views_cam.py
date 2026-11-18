"""The CAM screens of a commercial property: the pools and each tenant's share, the yearly reconciliation (look, approve, send the statements) and a tenant's statement."""
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from accounting import board_send, ledger
from accounting.models import Account
from accounting.web import acc_up, admin_only, parse_date

from . import camcalc, cam
from .invoices import InvoiceError
from .models import CamLine, CamPool, CamReconciliation, CamShare, Lease
from .views import commercial_or_404


def _dec(text, default=None):
    """A number typed in a form (a percent, dollars, a weight) or the default when it is blank. Raises ValueError when it is not a number."""
    text = (text or '').strip().replace('$', '').replace(',', '').replace('%', '')
    if not text:
        return default
    try:
        return Decimal(text)
    except InvalidOperation:
        raise ValueError(f'"{text}" is not a number.')


def _year(request, default):
    try:
        year = int(request.GET.get('year') or request.POST.get('year') or default)
    except ValueError:
        year = default
    return year if 2000 <= year <= 2100 else default


def expense_accounts(prop):
    return list(Account.objects.filter(association=prop, kind=Account.Kind.EXPENSE, is_active=True).order_by('code'))


# ---------------------------------------------------------------- the CAM page: pools of a year and the history

@admin_only
def cam_home(request, pk):
    prop = commercial_or_404(pk)
    year = _year(request, cam.default_year())
    here = reverse('commercial:cam', args=[prop.pk]) + f'?year={year}'
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'copy_forward':
                made = cam.copy_pools(prop, year - 1, year)
                messages.success(request, f'{made} pool{"s" if made != 1 else ""} copied from {year - 1}.' if made else f'Nothing to copy: {year} already has every pool {year - 1} had, or {year - 1} has none.')
            elif action == 'delete_pool':
                pool = get_object_or_404(CamPool, pk=request.POST.get('pool'), property=prop)
                if CamReconciliation.objects.filter(property=prop, year=pool.year, status=CamReconciliation.Status.APPROVED).exists():
                    raise ValueError(f'{pool.year} is approved: take the approval back before changing its pools.')
                pool.delete()
                messages.success(request, 'The pool is removed.')
        except ValueError as e:
            messages.error(request, str(e))
        return redirect(here)
    pools = []
    for p in CamPool.objects.filter(property=prop, year=year).prefetch_related('accounts', 'shares'):
        actual = cam.actual_costs(p)
        pools.append({'pool': p, 'accounts': sorted(a.code for a in p.accounts.all()), 'actual': sum(actual.values(), Decimal('0.00')), 'tenants': p.shares.exclude(method=camcalc.NONE).count()})
    recon = CamReconciliation.objects.filter(property=prop, year=year).first()
    history = list(CamReconciliation.objects.filter(property=prop, status=CamReconciliation.Status.APPROVED).order_by('-year'))
    start, end = cam.period(prop, year)
    return render(request, 'commercial/cam.html', {'assoc': prop, 'year': year, 'years': list(range(year - 3, year + 2)), 'pools': pools, 'recon': recon, 'history': history, 'start': start, 'end': end,
                                                   'has_previous': CamPool.objects.filter(property=prop, year=year - 1).exists(), **acc_up(request, prop, reverse('commercial:leases', args=[prop.pk]), 'Leases')})


# ---------------------------------------------------------------- one pool and the tenants' shares

def _save_pool(request, prop, pool, year):
    """Read the pool form: the pool itself, then one share per lease. Returns the saved pool. Raises ValueError with a plain message."""
    post = request.POST
    name = ' '.join((post.get('name') or '').split())
    if not name:
        raise ValueError('Give the pool a name (for example Building, Parking, or Taxes and insurance).')
    valid = {str(a.pk): a for a in expense_accounts(prop)}
    pick = lambda key: [valid[v] for v in post.getlist(key) if v in valid]
    den = post.get('denominator') if post.get('denominator') in dict(CamPool.Denominator.choices) else CamPool.Denominator.ALL
    fields = dict(name=name, basis=post.get('basis') if post.get('basis') in ('accrual', 'cash') else 'accrual', admin_fee_percent=_dec(post.get('admin_fee_percent'), Decimal('0')),
                  gross_up_percent=_dec(post.get('gross_up_percent')), occupancy_override=_dec(post.get('occupancy_override')), denominator=den, denominator_sf=_dec(post.get('denominator_sf')),
                  notes=(post.get('notes') or '').strip())
    for key in ('admin_fee_percent', 'gross_up_percent', 'occupancy_override'):
        v = fields[key]
        if v is not None and not (Decimal('0') <= v <= Decimal('1000')):
            raise ValueError('A percent must be between 0 and 1000.')
    if den == CamPool.Denominator.MANUAL and not fields['denominator_sf']:
        raise ValueError('Type the square-foot total, or choose another way to count the square feet.')
    with transaction.atomic():
        if pool is None:
            pool = CamPool(property=prop, year=year)
        for k, v in fields.items():
            setattr(pool, k, v)
        try:
            pool.save()
        except IntegrityError:
            raise ValueError(f'There is already a pool called "{name}" for {year}.')
        pool.accounts.set(pick('accounts'))
        pool.excluded_accounts.set(pick('excluded'))
        pool.gross_up_accounts.set(pick('gross_up'))
        start, end = cam.period(prop, pool.year)
        for lease in Lease.objects.filter(property=prop):
            if f'in_{lease.pk}' not in post:
                continue
            method = post.get(f'm_{lease.pk}')
            existing = pool.shares.filter(lease=lease).first()
            if method not in camcalc.METHODS:                     # "not in this pool": no share at all
                if existing:
                    existing.delete()
                continue
            share = existing or CamShare(pool=pool, lease=lease)
            value = _dec(post.get(f'v_{lease.pk}'))
            share.method = method
            share.percent = value if method == camcalc.PERCENT else None
            share.weight = value if method == camcalc.WEIGHT else None
            share.fixed_amount = value if method == camcalc.FIXED else None
            share.prorate = bool(post.get(f'pr_{lease.pk}'))
            share.cap_percent = _dec(post.get(f'cap_{lease.pk}'))
            share.max_per_sf = _dec(post.get(f'msf_{lease.pk}'))
            by = (post.get(f'by_{lease.pk}') or '').strip()
            share.base_year = int(by) if by.isdigit() else None
            share.base_amount = _dec(post.get(f'ba_{lease.pk}'))
            share.note = (post.get(f'note_{lease.pk}') or '').strip()[:300]
            if method == camcalc.PERCENT and share.percent is None:
                raise ValueError(f'{lease.tenant.name}: type the percent.')
            if method == camcalc.WEIGHT and share.weight is None:
                raise ValueError(f'{lease.tenant.name}: type the weight.')
            if method == camcalc.FIXED and share.fixed_amount is None:
                raise ValueError(f'{lease.tenant.name}: type the amount for the year.')
            share.save()
            share.excluded_accounts.set([valid[v] for v in post.getlist(f'ex_{lease.pk}') if v in valid])
    return pool


@admin_only
def cam_pool(request, pk, pool_pk=None):
    prop = commercial_or_404(pk)
    pool = get_object_or_404(CamPool, pk=pool_pk, property=prop) if pool_pk else None
    year = pool.year if pool else _year(request, cam.default_year())
    locked = CamReconciliation.objects.filter(property=prop, year=year, status=CamReconciliation.Status.APPROVED).exists()
    if request.method == 'POST':
        if locked:
            messages.error(request, f'{year} is approved: take the approval back before changing its pools.')
            return redirect(request.path)
        try:
            is_new = pool is None
            saved = _save_pool(request, prop, pool, year)
            if is_new and not any(k.startswith('in_') for k in request.POST):
                cam.ensure_shares(saved)                     # a new pool starts with every tenant who had space in it, until someone takes one out
            messages.success(request, f'{saved.name} saved.')
            return redirect('commercial:cam_pool', pk=prop.pk, pool_pk=saved.pk)
        except ValueError as e:
            messages.error(request, str(e))
    start, end = cam.period(prop, year)
    shares = {s.lease_id: s for s in pool.shares.prefetch_related('excluded_accounts')} if pool else {}
    rows = []
    for lease in Lease.objects.filter(property=prop).select_related('unit', 'tenant'):
        fraction = cam.lease_year_fraction(lease, start, end)
        s = shares.get(lease.pk)
        if fraction <= 0 and s is None:
            continue
        value = ''
        if s is not None:
            value = {camcalc.PERCENT: s.percent, camcalc.WEIGHT: s.weight, camcalc.FIXED: s.fixed_amount}.get(s.method)
            value = '' if value is None else f'{value:f}'.rstrip('0').rstrip('.') if '.' in f'{value:f}' else f'{value:f}'
        rows.append({'lease': lease, 'share': s, 'value': value, 'fraction': fraction, 'sf': lease.unit.square_footage, 'excluded': {a.pk for a in s.excluded_accounts.all()} if s else set()})
    accounts = expense_accounts(prop)
    return render(request, 'commercial/cam_pool.html', {
        'assoc': prop, 'pool': pool, 'year': year, 'rows': rows, 'accounts': accounts, 'locked': locked, 'methods': CamShare.Method.choices, 'denominators': CamPool.Denominator.choices,
        'chosen': {a.pk for a in pool.accounts.all()} if pool else set(), 'excluded': {a.pk for a in pool.excluded_accounts.all()} if pool else set(),
        'gross': {a.pk for a in pool.gross_up_accounts.all()} if pool else set(), 'start': start, 'end': end,
        **acc_up(request, prop, reverse('commercial:cam', args=[prop.pk]) + f'?year={year}', f'CAM {year}', labels={'commercial:cam': f'CAM {year}'})})


# ---------------------------------------------------------------- the reconciliation

@admin_only
def cam_reconcile(request, pk):
    prop = commercial_or_404(pk)
    year = _year(request, cam.default_year())
    recon = cam.draft_for(prop, year)
    here = reverse('commercial:cam_reconcile', args=[prop.pk]) + f'?year={year}'
    approved = recon.status == CamReconciliation.Status.APPROVED
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action in ('settings', 'approve') and not approved:
                recon.approval_date = parse_date(request.POST.get('approval_date')) or recon.approval_date
                recon.due_days = int(request.POST.get('due_days') or recon.due_days)
                recon.growth_percent = _dec(request.POST.get('growth_percent'), Decimal('0')) or Decimal('0')
                recon.update_estimates = bool(request.POST.get('update_estimates'))
                recon.estimates_effective = parse_date(request.POST.get('estimates_effective')) or recon.estimates_effective
                if not 0 <= recon.due_days <= 365:
                    raise ValueError('Due days must be between 0 and 365.')
                if recon.update_estimates and not recon.estimates_effective:
                    raise ValueError('Say the date the new monthly estimates start.')
                recon.save()
                if action == 'settings':
                    messages.success(request, 'Saved.')
                else:
                    if request.POST.get('confirm') != 'yes':
                        raise ValueError('Tick the box to say you reviewed the figures before approving.')
                    cam.approve(recon, user=request.user)
                    messages.success(request, f'The {year} CAM reconciliation is approved and posted.')
            elif action == 'undo':
                cam.undo(recon, user=request.user)
                messages.success(request, f'The {year} approval is taken back; its charges and credits are voided.')
            elif action in ('send', 'resend'):
                line = get_object_or_404(CamLine, pk=request.POST.get('line'), reconciliation=recon)
                cam.send_statement(line, user=request.user, again=(action == 'resend'))
                messages.success(request, f'Statement sent to {", ".join(line.sent_to)}.')
            elif action == 'send_all':
                sent, problems = 0, []
                for line in recon.lines.filter(sent_at__isnull=True):
                    try:
                        cam.send_statement(line, user=request.user)
                        sent += 1
                    except InvoiceError as e:
                        problems.append(f'{line.tenant_name}: {e}')
                if sent:
                    messages.success(request, f'Sent {sent} statement{"s" if sent != 1 else ""}.')
                for p in problems:
                    messages.error(request, p)
                if not sent and not problems:
                    messages.info(request, 'Every statement was already sent.')
        except (ValueError, cam.CamError, InvoiceError, ledger.LedgerError) as e:
            messages.error(request, str(e))
        return redirect(here)
    context = {'assoc': prop, 'year': year, 'recon': recon, 'approved': approved, **acc_up(request, prop, reverse('commercial:cam', args=[prop.pk]) + f'?year={year}', f'CAM {year}', labels={'commercial:cam': f'CAM {year}'}),
               'delivery': board_send.delivery_available(), 'today': timezone.localdate()}
    if approved:
        lines = list(recon.lines.select_related('lease', 'charge'))
        for l in lines:
            l.rows = [{'name': p['name'], 'method_label': p['method_label'], 'pool_cost': Decimal(p['pool_cost']), 'pct': f'{Decimal(p["fraction"]) * 100:.4f}', 'allocated': Decimal(p['allocated']), 'steps': p.get('steps', [])}
                      for p in (l.detail or {}).get('pools', [])]
        context['lines'] = lines
        context['pools'] = recon.pools
    else:
        plan = cam.with_growth(cam.compute(prop, year), recon.growth_percent)
        context.update({'plan': plan, 'default_effective': recon.estimates_effective or (cam.period(prop, year)[1] + __import__('datetime').timedelta(days=1)),
                        'default_date': recon.approval_date or timezone.localdate()})
    return render(request, 'commercial/cam_reconcile.html', context)


@admin_only
def cam_statement_pdf(request, pk, line_pk):
    prop = commercial_or_404(pk)
    line = get_object_or_404(CamLine.objects.select_related('reconciliation__property', 'charge'), pk=line_pk, reconciliation__property=prop)
    resp = HttpResponse(cam.statement_pdf(line), content_type='application/pdf')
    resp['Content-Disposition'] = f'inline; filename="{cam.statement_filename(line)}"'
    return resp
