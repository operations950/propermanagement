"""The arithmetic of a CAM (common area maintenance) reconciliation, with no database in it.

One pool, one year. The pool starts from what the shared costs really were (a dict of account code to amount), takes out what is excluded, grosses the variable costs up when the building
was emptier than the lease assumes, and adds the administration fee. Each tenant then takes its share by its own method (square feet, a percent, an equal split, a weight, or a fixed
amount), for the part of the year it had the space, and the lease's limits are applied in this order: base-year stop, cap on the rise over last year, most dollars a square foot.

Every number is a Decimal and money is rounded to the cent at the end of each step that produces money.
"""
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal('0.01')
ZERO = Decimal('0.00')
HUNDRED = Decimal(100)

SQFT, PERCENT, EQUAL_TENANTS, EQUAL_UNITS, WEIGHT, FIXED, NONE = 'sqft', 'percent', 'equal_tenants', 'equal_units', 'weight', 'fixed', 'none'
METHODS = (SQFT, PERCENT, EQUAL_TENANTS, EQUAL_UNITS, WEIGHT, FIXED, NONE)


def money(x):
    return Decimal(x).quantize(CENT, rounding=ROUND_HALF_UP)


def dec(x, default=None):
    return default if x is None or x == '' else Decimal(x)


def pool_cost(actual, excluded=(), gross_up_codes=(), gross_up_percent=None, occupancy_percent=None, admin_fee_percent=0):
    """What the pool comes to before it is divided.
    actual: {account code: cost} of the recoverable accounts. excluded: codes left out. gross_up_codes: the variable accounts to gross up (none given: all of them).
    gross_up_percent / occupancy_percent: the occupancy the lease assumes and the occupancy there really was (percent, 0 to 100).
    Returns {'recoverable', 'gross_up', 'admin_fee', 'total'}."""
    excluded = set(excluded)
    kept = {code: Decimal(amount) for code, amount in actual.items() if code not in excluded}
    recoverable = money(sum(kept.values(), ZERO))
    variable_codes = set(gross_up_codes) - excluded
    variable = money(sum((v for c, v in kept.items() if (c in variable_codes if variable_codes else True)), ZERO))
    gross_up = ZERO
    if gross_up_percent is not None and occupancy_percent is not None and Decimal(occupancy_percent) > 0 and Decimal(occupancy_percent) < Decimal(gross_up_percent):
        gross_up = money(variable * (Decimal(gross_up_percent) / Decimal(occupancy_percent) - 1))
    admin_fee = money((recoverable + gross_up) * Decimal(admin_fee_percent or 0) / HUNDRED)
    return {'recoverable': recoverable, 'gross_up': gross_up, 'admin_fee': admin_fee, 'total': recoverable + gross_up + admin_fee}


def fraction_of(method, tenant, pool):
    """The share of the pool a tenant takes by its method, as a Decimal (0 to 1 and a little over for a bad setup). Returns (fraction, why it is zero or '')."""
    if method == NONE:
        return ZERO, 'not charged'
    if method == SQFT:
        denominator = Decimal(pool.get('denominator_sf') or 0)
        if denominator <= 0:
            return ZERO, 'no square feet to divide by'
        return Decimal(tenant.get('sf') or 0) / denominator, ''
    if method == PERCENT:
        if tenant.get('percent') is None:
            return ZERO, 'no percent set'
        return Decimal(tenant['percent']) / HUNDRED, ''
    if method == EQUAL_TENANTS:
        n = int(pool.get('equal_count') or 0)
        return (Decimal(1) / n, '') if n > 0 else (ZERO, 'nobody to split between')
    if method == EQUAL_UNITS:
        n = int(pool.get('unit_count') or 0)
        return (Decimal(1) / n, '') if n > 0 else (ZERO, 'no units to split between')
    if method == WEIGHT:
        total = Decimal(pool.get('weights_total') or 0)
        if total <= 0 or tenant.get('weight') is None:
            return ZERO, 'no weight set'
        return Decimal(tenant['weight']) / total, ''
    return ZERO, ''


def allocate_tenant(pool, tenant):
    """One tenant's share of one pool.
    pool: {'actual': {code: cost}, 'excluded': set, 'gross_up_codes': set, 'gross_up_percent', 'occupancy_percent', 'admin_fee_percent', 'denominator_sf', 'equal_count', 'unit_count', 'weights_total'}
    tenant: {'sf', 'year_fraction' (0 to 1: the part of the year it had the space), 'method', 'percent', 'weight', 'fixed', 'prorate', 'cap_percent', 'prior_annual' (last year's annualized
    share of this pool, or None), 'max_per_sf', 'base_amount' (the yearly amount it does not pay), 'excluded' (codes this tenant does not pay)}.
    Returns {'cost' (the pool as this tenant sees it), 'fraction', 'year_fraction', 'gross_share', 'after_base', 'allocated', 'annual' (allocated over the year fraction), 'steps': [text]}."""
    method = tenant.get('method') or SQFT
    year_fraction = Decimal(tenant.get('year_fraction') if tenant.get('year_fraction') is not None else 1)
    pf = year_fraction if tenant.get('prorate', True) else Decimal(1)
    steps = []
    cost = pool_cost(pool['actual'], set(pool.get('excluded') or ()) | set(tenant.get('excluded') or ()), pool.get('gross_up_codes') or (), pool.get('gross_up_percent'), pool.get('occupancy_percent'),
                     pool.get('admin_fee_percent') or 0)
    if method == FIXED:
        fixed = Decimal(tenant.get('fixed') or 0)
        gross = money(fixed * pf)
        fraction, why = ZERO, ''
        steps.append(f'Fixed amount {fixed:,.2f}' + (f' for {pf * 100:.2f}% of the year' if pf != 1 else ''))
    else:
        fraction, why = fraction_of(method, tenant, pool)
        gross = money(cost['total'] * fraction * pf)
        if why:
            steps.append(why)
        else:
            steps.append(f'Pool {cost["total"]:,.2f} x share {fraction * 100:.4f}%' + (f' x {pf * 100:.2f}% of the year' if pf != 1 else ''))
    after = gross
    base = tenant.get('base_amount')
    if base is not None and Decimal(base) > 0 and method != NONE:
        stop = money(Decimal(base) * pf)
        after = max(after - stop, ZERO)
        steps.append(f'Base-year stop {stop:,.2f} not charged')
    cap = tenant.get('cap_percent')
    prior = tenant.get('prior_annual')
    if cap is not None and prior is not None and method != NONE:
        limit = money(Decimal(prior) * (Decimal(1) + Decimal(cap) / HUNDRED) * pf)
        if after > limit:
            steps.append(f'Capped at {limit:,.2f} ({Decimal(cap):g}% over last year)')
            after = limit
    max_sf = tenant.get('max_per_sf')
    if max_sf is not None and Decimal(tenant.get('sf') or 0) > 0 and method != NONE:
        limit = money(Decimal(max_sf) * Decimal(tenant['sf']) * pf)
        if after > limit:
            steps.append(f'Held to {limit:,.2f} ({Decimal(max_sf):g} a square foot)')
            after = limit
    allocated = money(after)
    annual = money(allocated / pf) if pf > 0 else ZERO
    return {'cost': cost, 'fraction': fraction, 'year_fraction': pf, 'gross_share': gross, 'after_base': money(after), 'allocated': allocated, 'annual': annual, 'steps': steps}


def true_up(allocated, estimates):
    """What the tenant owes (plus) or is credited (minus): the year's share less the estimates already billed."""
    return money(Decimal(allocated) - Decimal(estimates))


def new_monthly_estimate(annual_total, growth_percent=0):
    """The next year's monthly estimate: this year's annualized share, grown by a percent, over twelve."""
    return money(Decimal(annual_total) * (Decimal(1) + Decimal(growth_percent or 0) / HUNDRED) / 12)
