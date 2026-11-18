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


# ---------------------------------------------------------------- Up (see core/nav.py and core/nav_tree.py)

def acc_up(request, prop, default_url, default_label, labels=None):
    """acc_back / acc_back_label for a book screen: the wide screen above this one (core/nav_tree.py) that the person came from, else the screen's usual parent
    (`default_url`, `default_label`: the view knows the association's name, the unit ...). `labels` ({url name: text}) is for a link whose words depend on the record on the page.
    Used as **acc_up(...) in the view's context; base_acc.html draws it with the shared Up link."""
    found = nav.up(request, default=(default_url, default_label), labels=labels, ctx={'prop': prop})
    return {'acc_back': found.url, 'acc_back_label': found.label}
