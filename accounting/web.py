"""Small helpers shared by the accounting screens."""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone

from core import nav
from core.models import Property
from core.permissions import has_financial as _is_admin

from . import labels, ledger


def admin_only(view):
    """Only an administrator may open the books."""
    return login_required(user_passes_test(_is_admin)(view))


def association_or_404(pk):
    """The property whose books these are: an association or a commercial building (the same books, with other words: see accounting/labels.py)."""
    prop = get_object_or_404(Property, pk=pk, property_type__in=labels.BOOKS_TYPES)
    ledger.profile_for(prop)
    return prop


def association_only_or_404(pk):
    """For the screens that only an association has (owner collections, the delinquency cases): a commercial property gets a plain 404."""
    return get_object_or_404(Property, pk=pk, property_type=Property.Type.ASSOCIATION)


def books_or_redirect(request, prop):
    """None when the association's books are switched on; otherwise a redirect to the settings with a note."""
    if not ledger.profile_for(prop).we_do_financials:
        messages.error(request, f'We are not keeping the books for {prop.name} yet. Switch on "We do the financials" in the accounting settings.')
        return redirect(reverse('accounting:settings', args=[prop.pk]))
    ledger.ensure_chart(prop)   # cheap, and it brings an older association up to the current standard chart
    return None


def parse_month(text, default):
    try:
        return datetime.strptime(text or '', '%Y-%m').date().replace(day=1)
    except ValueError:
        return default


def parse_date(text, default=None):
    for fmt in ('%Y-%m-%d', '%m/%d/%Y'):
        try:
            return datetime.strptime((text or '').strip(), fmt).date()
        except ValueError:
            continue
    return default


def parse_money(text):
    """A dollar amount typed by a person: $1,750.00 and 1750 both work. Raises ValueError with a message for them."""
    s = (text or '').replace('$', '').replace(',', '').strip()
    if not s:
        raise ValueError('Type an amount.')
    try:
        return Decimal(s).quantize(Decimal('0.01'))
    except InvalidOperation:
        raise ValueError(f'"{text}" is not an amount.')


def default_month():
    return ledger.previous_month(timezone.localdate())


def month_context(month):
    return {'month': month, 'previous': ledger.previous_month(month), 'next': ledger.next_month(month), 'month_end': ledger.month_end(month)}


# ---------------------------------------------------------------- Up (see core/nav.py)

# What Up says for each wide screen a book screen can be reached from.
UP_LABELS = {
    'accounting:home': 'Associations', 'accounting:hub': 'Accounting', 'accounting:close': 'Month-end close', 'accounting:units': 'Units', 'accounting:charges': 'Charges',
    'accounting:bills': 'Bills', 'accounting:reports': 'Reports', 'accounting:collections': 'Owner collections', 'accounting:collections_home': 'Owner collections',
    'accounting:delinquencies': 'Delinquencies', 'accounting:late_fees': 'Late fees and interest', 'accounting:checks': 'Checks', 'accounting:bank': 'Bank reconciliation',
    'accounting:payables_home': 'Payables', 'accounting:budgets': 'Budgets', 'accounting:budget_project': 'Budget project', 'accounting:contracts': 'Contracts',
    'accounting:unit': 'Unit', 'accounting:report_aging': 'Aging', 'accounting:report_payables': 'Payables report', 'accounting:recurring': 'Recurring', 'accounting:journal': 'Journal',
    'commercial:leases': 'Leases', 'commercial:invoices': 'Tenant invoices', 'commercial:cam': 'CAM', 'commercial:lease': 'Tenant',
    'ws_financials': 'Financials', 'close_home': 'Month-end checklist',
}


def _up_label(prop, name):
    if prop is not None and name == 'accounting:units':
        return labels.label(prop, 'units_title')
    if prop is not None and name == 'accounting:charges':
        return labels.label(prop, 'charges_title')
    return UP_LABELS.get(name, name)


def acc_up(request, prop, names, default_url, default_label):
    """acc_back / acc_back_label for a book screen: the wide screen (one of `names`, url names, or (url name, label) pairs) the person came from, else the screen's usual parent.
    Used as **acc_up(...) in the view's context; base_acc.html draws it with the shared Up link."""
    parents = {}
    for item in names:
        name, label = item if isinstance(item, tuple) else (item, _up_label(prop, item))
        parents[name] = label
    found = nav.up(request, parents=parents, default=(default_url, default_label))
    return {'acc_back': found.url, 'acc_back_label': found.label}
