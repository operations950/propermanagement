"""CAM reconciliation: the books and the leases in, one charge or credit per tenant out.

The year's real cost of each pool comes from the property's own ledger (the accrual book unless the pool says cash) for the pool's expense accounts. camcalc.py divides it between the
tenants. The CAM estimates really billed to each tenant in the year (the charges on the Estimated CAM Charges account, 4210) are taken off, and the difference is the true-up:

* The tenant owes it: one charge (kind Special, on 4210) dated the approval date and due `due_days` later.
* The tenant is credited: a credit held for the tenant, the way a payment made ahead is held (a receipt of method Other that debits 4210 and credits Prepaid Fees, in both books), which
  the next charges use at once (charges.apply_credits). It shows as a negative balance for the tenant and never as an open charge, so aging is untouched.

An approved reconciliation is a record: its lines are kept as approved and it cannot be approved a second time. Undoing it voids its charges and credits.
"""
import io
from datetime import date, timedelta
from decimal import Decimal

from django.core.mail import EmailMessage
from django.db import transaction
from django.utils import timezone

from accounting import board_send, charges, ledger
from accounting import standard_chart as sc
from accounting.models import Account, Charge, JournalEntry, Receipt

from . import camcalc
from .models import CamEstimateStep, CamLine, CamPool, CamReconciliation, CamShare, Lease

ZERO = Decimal('0.00')
CAM_CODE = sc.COMMERCIAL_CODES['cam']


class CamError(Exception):
    pass


METHOD_LABELS = dict(CamShare.Method.choices)


# ---------------------------------------------------------------- periods and costs

def period(prop, year):
    """(first day, last day) of the property's fiscal year that starts in `year`."""
    month = ledger.profile_for(prop).fiscal_year_start_month or 1
    start = date(year, month, 1)
    return start, date(year + 1, month, 1) - timedelta(days=1)


def default_year(today=None):
    """The year most likely to be reconciled now: last year until midsummer, then the current one."""
    today = today or timezone.localdate()
    return today.year - 1 if today.month <= 6 else today.year


def lease_year_fraction(lease, start, end):
    """The part of the year (0 to 1, by the day) the lease had the space."""
    if lease.status == Lease.Status.ENDED and not lease.end_date:
        return ZERO
    total = (end - start).days + 1
    first = max(start, lease.start_date) if lease.start_date else start
    last = min(end, lease.end_date) if lease.end_date else end
    return Decimal(max((last - first).days + 1, 0)) / Decimal(total)


def actual_costs(pool):
    """{account code: what the ledger says was spent} for each of the pool's recoverable accounts, over the pool's year."""
    start, end = period(pool.property, pool.year)
    stmt = ledger.income_statement(pool.property, start, end, basis=pool.basis)
    spent = {r['account'].code: r['amount'] for r in stmt['expense']}
    return {a.code: spent.get(a.code, ZERO) for a in pool.accounts.all()}


def estimates_billed(lease, start, end):
    """What the lease was really billed in monthly CAM estimates in the period: the CAM charges of the tenant, not the true-up charges of an earlier year."""
    qs = Charge.objects.filter(unit=lease.unit, owner=lease.tenant, is_void=False, due_date__gte=start, due_date__lte=end, income_account__code=CAM_CODE).exclude(kind=Charge.Kind.SPECIAL)
    return sum((c.amount for c in qs), ZERO)


def default_method(lease):
    return lease.cam_method if lease.cam_method in camcalc.METHODS else camcalc.SQFT


def ensure_shares(pool):
    """Every lease that had space in the year takes part in the pool until someone says otherwise: make the missing shares (the lease's own method and percent as the start)."""
    start, end = period(pool.property, pool.year)
    made = 0
    for lease in Lease.objects.filter(property=pool.property).select_related('unit', 'tenant'):
        if lease_year_fraction(lease, start, end) <= 0 or pool.shares.filter(lease=lease).exists():
            continue
        CamShare.objects.create(pool=pool, lease=lease, method=default_method(lease), percent=lease.cam_share_percent)
        made += 1
    return made


# ---------------------------------------------------------------- the reconciliation

def _prior_pool_detail(lease, prop, year, pool_name, key):
    """A number from last (or a base) year's approved reconciliation for this lease and pool: key is 'annual' or 'gross_annual'. None when there is none."""
    line = CamLine.objects.filter(reconciliation__property=prop, reconciliation__year=year, reconciliation__status=CamReconciliation.Status.APPROVED, lease=lease).first()
    if line is None:
        return None
    for p in (line.detail or {}).get('pools', []):
        if p.get('name') == pool_name and p.get(key) is not None:
            return Decimal(p[key])
    return None


def compute(prop, year):
    """The reconciliation worked out from the books and the leases now: {'year', 'start', 'end', 'pools': [...], 'lines': [...], 'totals': {...}, 'warnings': [...]}. Nothing is saved."""
    start, end = period(prop, year)
    units = list(prop.units.filter(is_active=True))
    total_sf = sum((Decimal(u.square_footage or 0) for u in units), ZERO)
    leases = list(Lease.objects.filter(property=prop).select_related('unit', 'tenant'))
    fractions = {l.pk: lease_year_fraction(l, start, end) for l in leases}
    occupied_sf = sum((Decimal(l.unit.square_footage or 0) * fractions[l.pk] for l in leases), ZERO)
    occupancy = min(occupied_sf / total_sf * 100, Decimal(100)) if total_sf > 0 else None
    warnings = []
    pools_out, lines = [], {}
    for pool in CamPool.objects.filter(property=prop, year=year).prefetch_related('accounts', 'excluded_accounts', 'gross_up_accounts'):
        shares = list(pool.shares.select_related('lease__unit', 'lease__tenant').prefetch_related('excluded_accounts'))
        shares = [s for s in shares if fractions.get(s.lease_id, ZERO) > 0 and s.method != camcalc.NONE]
        actual = actual_costs(pool)
        occ = pool.occupancy_override if pool.occupancy_override is not None else occupancy
        if pool.denominator == CamPool.Denominator.MANUAL:
            denominator = Decimal(pool.denominator_sf or 0)
        elif pool.denominator == CamPool.Denominator.OCCUPIED:
            denominator = sum((Decimal(s.lease.unit.square_footage or 0) for s in shares if s.method == camcalc.SQFT), ZERO)
        else:
            denominator = total_sf
        spec = {'actual': actual, 'excluded': {a.code for a in pool.excluded_accounts.all()}, 'gross_up_codes': {a.code for a in pool.gross_up_accounts.all()},
                'gross_up_percent': pool.gross_up_percent, 'occupancy_percent': occ, 'admin_fee_percent': pool.admin_fee_percent, 'denominator_sf': denominator,
                'equal_count': sum(1 for s in shares if s.method == camcalc.EQUAL_TENANTS), 'unit_count': len(units),
                'weights_total': sum((Decimal(s.weight or 0) for s in shares if s.method == camcalc.WEIGHT), ZERO)}
        cost = camcalc.pool_cost(actual, spec['excluded'], spec['gross_up_codes'], pool.gross_up_percent, occ, pool.admin_fee_percent)
        pool_total = ZERO
        for s in shares:
            lease = s.lease
            fraction = fractions[lease.pk]
            base = s.base_amount
            if base is None and s.base_year:
                got = _prior_pool_detail(lease, prop, s.base_year, pool.name, 'gross_annual')
                base = got
                if got is None:
                    warnings.append(f'{lease.tenant.name}: no approved reconciliation for {s.base_year} here, so its base-year stop in {pool.name} is not applied. Type the amount instead.')
            prior = _prior_pool_detail(lease, prop, year - 1, pool.name, 'annual') if s.cap_percent is not None else None
            tenant = {'sf': Decimal(lease.unit.square_footage or 0), 'year_fraction': fraction, 'method': s.method, 'percent': s.percent if s.percent is not None else lease.cam_share_percent,
                      'weight': s.weight, 'fixed': s.fixed_amount, 'prorate': s.prorate, 'cap_percent': s.cap_percent, 'prior_annual': prior, 'max_per_sf': s.max_per_sf,
                      'base_amount': base, 'excluded': {a.code for a in s.excluded_accounts.all()}}
            if s.cap_percent is not None and prior is None:
                warnings.append(f'{lease.tenant.name}: no approved {year - 1} reconciliation here, so the cap on its rise in {pool.name} is not applied.')
            if s.method == camcalc.SQFT and tenant['sf'] <= 0:
                warnings.append(f'{lease.tenant.name} in {lease.unit.label} has no square feet on the unit, so its share by square feet is zero.')
            res = camcalc.allocate_tenant(spec, tenant)
            pool_total += res['allocated']
            row = lines.setdefault(lease.pk, {'lease': lease, 'tenant': lease.tenant.name, 'unit': lease.unit.label, 'sf': tenant['sf'], 'year_fraction': fraction, 'allocated': ZERO, 'pools': [], 'annual': ZERO})
            gross_annual = camcalc.money(res['gross_share'] / res['year_fraction']) if res['year_fraction'] > 0 else ZERO
            row['allocated'] += res['allocated']
            row['annual'] += res['annual']
            row['pools'].append({'name': pool.name, 'method': s.method, 'method_label': METHOD_LABELS.get(s.method, s.method), 'pool_cost': res['cost']['total'], 'fraction': res['fraction'],
                                 'fraction_pct': f'{res["fraction"] * 100:.4f}', 'gross_share': res['gross_share'], 'allocated': res['allocated'], 'annual': res['annual'], 'gross_annual': gross_annual, 'steps': res['steps']})
        pools_out.append({'pool': pool, 'name': pool.name, 'actual': actual, 'cost': cost, 'denominator_sf': denominator, 'occupancy': occ, 'tenants': len(shares), 'allocated': pool_total,
                          'left_with_landlord': camcalc.money(cost['total'] - pool_total)})
    out = []
    for row in lines.values():
        lease = row['lease']
        est = estimates_billed(lease, start, end)
        row['estimates'] = est
        row['allocated'] = camcalc.money(row['allocated'])
        row['true_up'] = camcalc.true_up(row['allocated'], est)
        continuing = not (lease.end_date and lease.end_date <= end) and lease.status != Lease.Status.ENDED
        row['new_estimate'] = camcalc.new_monthly_estimate(row['annual'], 0) if continuing else ZERO
        out.append(row)
    out.sort(key=lambda r: (r['unit'], r['tenant']))
    in_a_pool = {r['lease'].pk for r in out}
    for l in leases:
        if fractions[l.pk] > 0 and l.pk not in in_a_pool and (l.cam_estimate or 0) > 0:
            warnings.append(f'{l.tenant.name} in {l.unit.label} is billed a CAM estimate but is not in any pool for {year}.')
    if not pools_out:
        warnings.append(f'There are no CAM pools for {year} yet.')
    totals = {'allocated': sum((r['allocated'] for r in out), ZERO), 'estimates': sum((r['estimates'] for r in out), ZERO), 'true_up': sum((r['true_up'] for r in out), ZERO),
              'pool_cost': sum((p['cost']['total'] for p in pools_out), ZERO), 'left_with_landlord': sum((p['left_with_landlord'] for p in pools_out), ZERO)}
    return {'year': year, 'start': start, 'end': end, 'pools': pools_out, 'lines': out, 'totals': totals, 'warnings': warnings, 'occupancy': occupancy}


def with_growth(plan, growth_percent):
    """The plan with each tenant's recommended new monthly estimate grown by a percent."""
    for r in plan['lines']:
        r['new_estimate'] = camcalc.new_monthly_estimate(r['annual'], growth_percent) if r['new_estimate'] > 0 else ZERO
    return plan


def draft_for(prop, year):
    return CamReconciliation.objects.get_or_create(property=prop, year=year, defaults={'approval_date': None})[0]


# ---------------------------------------------------------------- approving

def _pool_record(p):
    return {'name': p['name'], 'actual': {k: str(v) for k, v in p['actual'].items()}, 'recoverable': str(p['cost']['recoverable']), 'gross_up': str(p['cost']['gross_up']), 'admin_fee': str(p['cost']['admin_fee']),
            'total': str(p['cost']['total']), 'denominator_sf': str(p['denominator_sf']), 'occupancy': str(p['occupancy']) if p['occupancy'] is not None else '', 'allocated': str(p['allocated']),
            'left_with_landlord': str(p['left_with_landlord'])}


def _line_detail(row):
    return {'year_fraction': str(row['year_fraction']), 'annual': str(row['annual']),
            'pools': [{'name': p['name'], 'method': p['method'], 'method_label': p['method_label'], 'pool_cost': str(p['pool_cost']), 'fraction': str(p['fraction']), 'gross_share': str(p['gross_share']),
                       'allocated': str(p['allocated']), 'annual': str(p['annual']), 'gross_annual': str(p['gross_annual']), 'steps': p['steps']} for p in row['pools']]}


@transaction.atomic
def approve(recon, user=None, approval_date=None):
    """Approve the year: keep the figures and post one charge or credit per tenant. Raises CamError when it was already approved, when the books are off, or when there is nothing to
    reconcile; ledger.LedgerError (a closed month) is left to the caller. Posts nothing twice."""
    recon = CamReconciliation.objects.select_for_update().get(pk=recon.pk)
    if recon.status == CamReconciliation.Status.APPROVED:
        raise CamError(f'The {recon.year} reconciliation was already approved.')
    prop = recon.property
    if not ledger.profile_for(prop).we_do_financials:
        raise CamError('The books for this property are not switched on.')
    plan = with_growth(compute(prop, recon.year), recon.growth_percent)
    if not plan['lines']:
        raise CamError(f'No tenant has a share in any CAM pool for {recon.year}.')
    day = approval_date or recon.approval_date or timezone.localdate()
    ledger.ensure_chart(prop)
    cam = ledger.account(prop, CAM_CODE)
    credited = []
    for row in plan['lines']:
        lease = row['lease']
        line = CamLine.objects.create(reconciliation=recon, lease=lease, tenant_name=row['tenant'], unit_label=row['unit'], square_feet=row['sf'], allocated=row['allocated'], estimates=row['estimates'],
                                      true_up=row['true_up'], new_estimate=row['new_estimate'], detail=_line_detail(row))
        amount = row['true_up']
        text = f'CAM reconciliation {recon.year}'
        if amount > 0:
            entry = ledger.post_entry(prop, day, [{'account': sc.AR, 'debit': amount, 'unit': lease.unit, 'owner': lease.tenant, 'memo': text}, {'account': cam, 'credit': amount, 'memo': text}],
                                      memo=text, basis=JournalEntry.Basis.ACCRUAL, source=JournalEntry.Source.CHARGE, source_ref=f'cam:{recon.pk}:{lease.pk}', user=user)
            line.charge = Charge.objects.create(association=prop, unit=lease.unit, owner=lease.tenant, kind=Charge.Kind.SPECIAL, description=text, due_date=day + timedelta(days=recon.due_days),
                                                amount=amount, income_account=cam, entry=entry)
        elif amount < 0:
            credit = -amount
            rec = Receipt.objects.create(association=prop, owner=lease.tenant, unit=lease.unit, date=day, amount=credit, method=Receipt.Method.OTHER, reference=f'CAM {recon.year}',
                                         memo=f'Credit from the {recon.year} CAM reconciliation', created_by=user)
            rec.entry = ledger.post_entry(prop, day, [{'account': cam, 'debit': credit, 'memo': text + ' credit'},
                                                      {'account': sc.PREPAID_FEES, 'credit': credit, 'unit': lease.unit, 'owner': lease.tenant, 'memo': text + ' credit'}],
                                          memo=text + ' credit', basis=JournalEntry.Basis.BOTH, source=JournalEntry.Source.RECEIPT, source_ref=f'receipt:{rec.pk}', user=user)
            rec.save(update_fields=['entry'])
            line.receipt = rec
            credited.append(lease)
        line.save()
        if recon.update_estimates and recon.estimates_effective and row['new_estimate'] > 0:
            CamEstimateStep.objects.update_or_create(lease=lease, effective_date=recon.estimates_effective, defaults={'monthly_amount': row['new_estimate'], 'note': f'CAM {recon.year} reconciliation'})
    for lease in credited:
        charges.apply_credits(prop, lease.unit, lease.tenant, user=user)           # a credit is used on what the tenant owes now, like money paid ahead
    recon.status = CamReconciliation.Status.APPROVED
    recon.approval_date, recon.approved_by, recon.approved_at = day, user, timezone.now()
    recon.pools = [_pool_record(p) for p in plan['pools']]
    recon.total_allocated, recon.total_estimates, recon.total_true_up = plan['totals']['allocated'], plan['totals']['estimates'], plan['totals']['true_up']
    recon.save()
    return recon


@transaction.atomic
def undo(recon, user=None):
    """Take an approval back: the charges and credits it made are voided and the year is a draft again. Refused when a tenant has paid one of its charges (void that payment first)."""
    recon = CamReconciliation.objects.select_for_update().get(pk=recon.pk)
    if recon.status != CamReconciliation.Status.APPROVED:
        raise CamError('That year is not approved.')
    lines = list(recon.lines.select_related('charge', 'receipt'))
    for line in lines:
        if line.charge_id and line.charge.applications.exists():
            raise CamError(f'{line.tenant_name} has a payment on the CAM charge. Void that payment first.')
    for line in lines:
        if line.charge_id:
            charges.void_charge(line.charge, user=user)
        if line.receipt_id:
            charges.void_receipt(line.receipt, user=user)
    CamEstimateStep.objects.filter(lease__property=recon.property, note=f'CAM {recon.year} reconciliation').delete()
    recon.lines.all().delete()
    recon.status, recon.approved_by, recon.approved_at, recon.pools = CamReconciliation.Status.DRAFT, None, None, []
    recon.total_allocated = recon.total_estimates = recon.total_true_up = ZERO
    recon.save()
    return recon


# ---------------------------------------------------------------- next year

@transaction.atomic
def copy_pools(prop, from_year, to_year):
    """Make next year's pools from this year's (the same accounts, fees and shares), skipping any pool that already exists. Returns how many pools were made."""
    made = 0
    for pool in CamPool.objects.filter(property=prop, year=from_year).prefetch_related('accounts', 'excluded_accounts', 'gross_up_accounts', 'shares__excluded_accounts'):
        if CamPool.objects.filter(property=prop, year=to_year, name=pool.name).exists():
            continue
        new = CamPool.objects.create(property=prop, name=pool.name, year=to_year, basis=pool.basis, admin_fee_percent=pool.admin_fee_percent, gross_up_percent=pool.gross_up_percent,
                                     occupancy_override=pool.occupancy_override, denominator=pool.denominator, denominator_sf=pool.denominator_sf, notes=pool.notes)
        new.accounts.set(pool.accounts.all())
        new.excluded_accounts.set(pool.excluded_accounts.all())
        new.gross_up_accounts.set(pool.gross_up_accounts.all())
        for s in pool.shares.all():
            if s.lease.status == Lease.Status.ENDED:
                continue
            ns = CamShare.objects.create(pool=new, lease=s.lease, method=s.method, percent=s.percent, weight=s.weight, fixed_amount=s.fixed_amount, prorate=s.prorate, cap_percent=s.cap_percent,
                                         max_per_sf=s.max_per_sf, base_year=s.base_year, base_amount=s.base_amount, note=s.note)
            ns.excluded_accounts.set(s.excluded_accounts.all())
        old_start, old_end = period(prop, from_year)
        new_start, new_end = period(prop, to_year)
        for lease in Lease.objects.filter(property=prop).select_related('unit', 'tenant'):          # a tenant who is new this year joins; one left out of the old pool on purpose stays out
            if lease_year_fraction(lease, old_start, old_end) <= 0 < lease_year_fraction(lease, new_start, new_end) and not new.shares.filter(lease=lease).exists():
                CamShare.objects.create(pool=new, lease=lease, method=default_method(lease), percent=lease.cam_share_percent)
        made += 1
    return made


def close_note(prop, month_first):
    """A line for the close sheet in the first months of a year: last year's CAM reconciliation has pools (so it is in use) but is not approved. '' otherwise."""
    if month_first.month > 4:
        return ''
    year = month_first.year - 1
    if not CamPool.objects.filter(property=prop, year=year).exists():
        return ''
    if CamReconciliation.objects.filter(property=prop, year=year, status=CamReconciliation.Status.APPROVED).exists():
        return ''
    return f'CAM reconciliation for {year} not approved.'


# ---------------------------------------------------------------- the tenant's statement

def landlord_name(prop):
    profile = ledger.profile_for(prop)
    return profile.legal_name or prop.name


def statement_pdf(line):
    """The tenant's CAM statement as a PDF, from the figures kept when the year was approved."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    recon = line.reconciliation
    prop = recon.property
    styles = getSampleStyleSheet()
    body = ParagraphStyle('b', parent=styles['Normal'], fontSize=10, leading=13)
    small = ParagraphStyle('s', parent=body, fontSize=8.5, leading=11, textColor=colors.HexColor('#555555'))
    big = ParagraphStyle('h', parent=styles['Heading1'], fontSize=20, spaceAfter=2)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.8 * inch, rightMargin=0.8 * inch, topMargin=0.8 * inch, bottomMargin=0.8 * inch, title=f'CAM {recon.year} {line.tenant_name}')
    start, end = period(prop, recon.year)
    story = [Paragraph(f'CAM RECONCILIATION {recon.year}', big), Paragraph(landlord_name(prop), body), Paragraph(prop.address or '', small), Spacer(1, 12)]
    head = Table([[Paragraph(f'<b>Tenant</b><br/>{line.tenant_name}<br/>{prop.name}, unit {line.unit_label}' + (f'<br/>{line.square_feet:,.0f} square feet' if line.square_feet else ''), body),
                   Paragraph(f'<b>Period</b><br/>{start:%B} {start.day}, {start.year} to {end:%B} {end.day}, {end.year}<br/><b>Statement date</b><br/>{recon.approval_date:%B} {recon.approval_date.day}, {recon.approval_date.year}', body)]],
                 colWidths=[3.4 * inch, 3.4 * inch])
    head.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    story += [head, Spacer(1, 14)]
    rows = [['Pool and how the share is worked out', 'Pool cost', 'Your share', 'Amount']]
    for p in (line.detail or {}).get('pools', []):
        pct = Decimal(p['fraction']) * 100
        share = f'{pct:.4f}%' if Decimal(p['fraction']) else ''
        rows.append([Paragraph(f'<b>{p["name"]}</b><br/>{p["method_label"]}' + ''.join(f'<br/><font size="8" color="#555555">{s}</font>' for s in p.get('steps', [])[1:]), body),
                     f'${Decimal(p["pool_cost"]):,.2f}', share, f'${Decimal(p["allocated"]):,.2f}'])
    rows.append(['Your share of the year', '', '', f'${line.allocated:,.2f}'])
    rows.append(['Estimates billed in the year', '', '', f'-${line.estimates:,.2f}'])
    owed = line.true_up
    rows.append(['Amount due from you' if owed > 0 else ('Credit to you' if owed < 0 else 'Nothing more is due'), '', '', f'${abs(owed):,.2f}'])
    table = Table(rows, colWidths=[3.6 * inch, 1.05 * inch, 1.05 * inch, 1.1 * inch], repeatRows=1)
    table.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#eef1f6')), ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'), ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
                               ('VALIGN', (0, 0), (-1, -1), 'TOP'), ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'), ('LINEABOVE', (0, -3), (-1, -3), 0.8, colors.HexColor('#444444')),
                               ('LINEABOVE', (0, -1), (-1, -1), 0.8, colors.HexColor('#444444')), ('LINEBELOW', (0, 0), (-1, -4), 0.25, colors.HexColor('#dddddd')),
                               ('TOPPADDING', (0, 0), (-1, -1), 4), ('BOTTOMPADDING', (0, 0), (-1, -1), 4)]))
    story.append(table)
    if owed > 0 and line.charge_id:
        story += [Spacer(1, 10), Paragraph(f'The amount due is on your account, due {line.charge.due_date:%B} {line.charge.due_date.day}, {line.charge.due_date.year}.', body)]
    elif owed < 0:
        story += [Spacer(1, 10), Paragraph('The credit is on your account and is used on your next charges.', body)]
    if line.new_estimate and recon.update_estimates and recon.estimates_effective:
        story += [Spacer(1, 6), Paragraph(f'From {recon.estimates_effective:%B} {recon.estimates_effective.day}, {recon.estimates_effective.year} your monthly CAM estimate is ${line.new_estimate:,.2f}.', body)]
    from .rent import settings_for
    note = (settings_for(prop).payment_instructions or '').strip()
    if note and owed > 0:
        story += [Spacer(1, 14), Paragraph('<b>How to pay</b>', body)] + [Paragraph(l, body) for l in note.splitlines() if l.strip()]
    doc.build(story)
    return buf.getvalue()


def statement_filename(line):
    return f'CAM {line.reconciliation.year} {line.tenant_name}.pdf'.replace('/', '-')


def statement_subject(line):
    return f'CAM reconciliation {line.reconciliation.year} for {line.reconciliation.property.name}'


def statement_body(line):
    return (f'Hello,\n\nAttached is the {line.reconciliation.year} common area maintenance (CAM) reconciliation for {line.tenant_name}, unit {line.unit_label}, at {line.reconciliation.property.name}. '
            f'It compares your share of the year\'s costs with the estimates you were billed.\n\nPlease let me know if you have any questions.\n\nThank you,\n{board_send.SIGN_OFF}')


def send_statement(line, user=None, again=False):
    """Email the tenant's statement to the lease's billing addresses. Refused (nothing recorded) when it was already sent and `again` is not set, when there is no address, when the year
    is not approved, or when no mail delivery is set up."""
    from .invoices import InvoiceError
    if line.reconciliation.status != CamReconciliation.Status.APPROVED:
        raise InvoiceError('Approve the year before sending statements.')
    if line.sent_at and not again:
        raise InvoiceError('That statement was already sent. Choose "Send again" to send it a second time.')
    emails = [e for e in (board_send.valid_email(x) for x in line.lease.billing_list()) if e]
    if not emails:
        raise InvoiceError(f'{line.tenant_name} has no email address for invoices. Add one on the lease.')
    if not board_send.delivery_available():
        raise InvoiceError('No email delivery is set up yet: connect a Gmail mailbox in Admin Tools. Nothing was sent.')
    mail = EmailMessage(statement_subject(line), statement_body(line), board_send.FROM_EMAIL, to=emails)
    mail.attach(statement_filename(line), statement_pdf(line), 'application/pdf')
    try:
        sent = mail.send(fail_silently=False)
    except Exception as e:
        raise InvoiceError(f'The email could not be sent ({e}). Nothing was recorded.')
    if sent != 1:
        raise InvoiceError('The email could not be sent. Nothing was recorded; try again.')
    line.sent_at, line.sent_by, line.sent_to = timezone.now(), user, emails
    line.send_count += 1
    line.save(update_fields=['sent_at', 'sent_by', 'sent_to', 'send_count'])
    return line
