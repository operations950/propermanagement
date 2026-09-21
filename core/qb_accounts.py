"""Tying each short-term rental to its two QuickBooks accounts.

  * the INCOME-STATEMENT account (`Property.qb_expense_account`) is the rental's
    reimbursable-expense account. Expenses the company pays are coded to it, and
    the monthly reimbursement comes back into it — so the account is not simply
    "the property's expenses": its debits are expenses paid, its credits are
    reimbursements.
  * the BALANCE-SHEET account (`Property.qb_trust_account`) is the owner's trust
    account. Owner deposits come in; going out are direct expenses, reimbursements
    to the company, management commission and the owner's monthly payment.

This module only does the mapping (which account belongs to which rental, kept
one-to-one so nothing is counted for two properties). Working out what each
transaction in those accounts IS builds on it.

QuickBooks itself says which side an account is on (its Classification): Asset,
Liability and Equity are balance-sheet accounts; Revenue and Expense are
income-statement accounts. That is what each picker is limited to."""
import re

from . import property_specs
from .models import Property, QuickBooksAccount, Unit

SIDES = {
    'expense': {'field': 'qb_expense_account', 'classes': QuickBooksAccount.INCOME_STATEMENT, 'label': 'Income-statement (reimbursable expense) account'},
    'trust': {'field': 'qb_trust_account', 'classes': QuickBooksAccount.BALANCE_SHEET, 'label': 'Balance-sheet (trust) account'},
}

_GENERIC = frozenset('ct court st street ave avenue blvd boulevard rd road dr drive ln lane way pl place cir circle trl trail the of llc inc unit apt'.split())


def applies_to(prop):
    """Only real, active short-term rentals are tied to accounts."""
    return property_specs.applies_to(prop)


def accounts_for(side, include_inactive_pk=None):
    """The accounts that may be chosen for one side: active ones of the right
    classification (plus the one currently chosen, even if QuickBooks has since
    retired it, so it stays visible)."""
    from django.db.models import Q
    q = Q(active=True, classification__in=SIDES[side]['classes'])
    if include_inactive_pk:
        q |= Q(pk=include_inactive_pk)
    return QuickBooksAccount.objects.filter(q).order_by('classification', 'account_type', 'fully_qualified_name')


def _label(target):
    return target.name if isinstance(target, Property) else f'{target.property.name} — {target.label}'


def used_by_other(account, side, target):
    """The other rental (a property, or a unit of one) already tied to this account
    on this side, or None. A property and its units share one pool of accounts, so
    nothing is counted twice."""
    field = SIDES[side]['field']
    other = Property.objects.filter(**{field: account})
    if isinstance(target, Property):
        other = other.exclude(pk=target.pk)
    found = other.first()
    if found is not None:
        return found
    others = Unit.objects.filter(**{field: account}).select_related('property')
    if isinstance(target, Unit):
        others = others.exclude(pk=target.pk)
    return others.first()


def save_mapping(prop, expense_id, trust_id):
    """Applies a chosen pair (blank = clear). Returns {side: message} for what was
    refused; whatever was valid is saved."""
    errors, chosen = {}, {}
    for side, raw in (('expense', expense_id), ('trust', trust_id)):
        raw = (raw or '').strip()
        if not raw:
            chosen[side] = None
            continue
        account = QuickBooksAccount.objects.filter(pk=raw).first() if raw.isdigit() else None
        current = getattr(prop, SIDES[side]['field'])
        if account is None:
            errors[side] = 'That account is not in the list — refresh the accounts from QuickBooks and pick again.'
        elif account.classification not in SIDES[side]['classes']:
            errors[side] = f'{account.fully_qualified_name} is a {account.classification.lower() or "unclassified"} account; this side needs an account from the {"income statement" if side == "expense" else "balance sheet"}.'
        elif not account.active and (current is None or current.pk != account.pk):
            errors[side] = f'{account.fully_qualified_name} is no longer active in QuickBooks.'
        else:
            other = used_by_other(account, side, prop)
            if other is not None:
                errors[side] = f'{account.fully_qualified_name} is already tied to {_label(other)}. Each account belongs to one rental — clear it there first.'
            else:
                chosen[side] = account
    changed = []
    for side, account in chosen.items():
        field = SIDES[side]['field']
        if getattr(prop, field + '_id') != (account.pk if account else None):
            setattr(prop, field, account)
            changed.append(field)
    if changed:
        prop.save(update_fields=changed)
    return errors


def _tokens(text):
    return re.findall(r'[a-z0-9]+', (text or '').lower())


def _key_tokens(target):
    prop = target if isinstance(target, Property) else target.property
    tokens = [t for t in _tokens(prop.name) if t not in _GENERIC]
    tokens = tokens or _tokens(prop.name)
    if isinstance(target, Unit):
        tokens += [t for t in _tokens(target.label) if t not in _GENERIC and t not in tokens]
    return tokens


def suggestions(prop, pool=None, limit=3):
    """Accounts whose names contain every significant word of the property's name
    ("324 Harmon Ct" -> an account called "...:324 Harmon"), closest name first,
    for each side, leaving out accounts another property already has. A hint to
    save typing — never applied on its own."""
    keys = _key_tokens(prop)
    pool = list(QuickBooksAccount.objects.filter(active=True)) if pool is None else pool
    out = {}
    for side, spec in SIDES.items():
        current = getattr(prop, spec['field'] + '_id')
        field = spec['field']
        taken_by_properties = Property.objects.exclude(**{field + '__isnull': True})
        taken_by_units = Unit.objects.exclude(**{field + '__isnull': True})
        if isinstance(prop, Property):
            taken_by_properties = taken_by_properties.exclude(pk=prop.pk)
        else:
            taken_by_units = taken_by_units.exclude(pk=prop.pk)
        taken = set(taken_by_properties.values_list(field + '_id', flat=True)) | set(taken_by_units.values_list(field + '_id', flat=True))
        found = []
        for account in pool:
            if account.classification not in spec['classes'] or account.pk in taken or account.pk == current:
                continue
            words = set(_tokens(account.fully_qualified_name))
            if keys and all(k in words for k in keys):
                found.append((len(_tokens(account.name)), account.fully_qualified_name, account))
        found.sort(key=lambda t: t[:2])
        out[side] = [a for _n, _f, a in found[:limit]]
    return out


def own_status(target):
    """'mapped' (both accounts), 'partial' or 'unmapped' — for a property's own pair
    or a unit's."""
    have = [target.qb_expense_account_id, target.qb_trust_account_id]
    if all(have):
        return 'mapped'
    return 'partial' if any(have) else 'unmapped'


def active_units(prop):
    return list(prop.units.filter(is_active=True).order_by('label'))


def status(prop):
    """'mapped', 'partial' or 'unmapped'. A property kept unit by unit is mapped when
    every active unit has both accounts (and it has at least one unit)."""
    if isinstance(prop, Property) and prop.financials_level == Property.FinancialsLevel.UNIT:
        units = active_units(prop)
        states = [own_status(u) for u in units]
        if units and all(s == 'mapped' for s in states):
            return 'mapped'
        return 'partial' if any(s != 'unmapped' for s in states) else 'unmapped'
    return own_status(prop)


def rentals_needing_accounts():
    return [p for p in Property.objects.filter(is_active=True, is_general=False, property_type=Property.Type.SHORT_TERM_RENTAL) if status(p) != 'mapped']
