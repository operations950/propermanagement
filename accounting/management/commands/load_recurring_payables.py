"""Loads the recurring payables the owner approved after the statement review (accounting/recurring_payables_data.py).

It looks first: with no flag it only reports, per association, what it would do (create a bill, leave one that is already there, a difference it will not touch, a name it could
not match). `--apply` does it. Safe to run again: a bill that is already there is left alone and never changed.

For an association whose books we do not keep yet, the bills are loaded switched off (they show on the recurring bills screen, ready to turn on when the books start); the standard chart
of accounts is added first if the association has none. For one whose books we keep, a bill is on unless the review left a question open on it. The first date of each bill is the next
time its day of the month comes round, so nothing is posted for a past month.
"""
import re
from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from accounting import ledger, payables, standard_chart
from accounting.models import Account, AssociationProfile, RecurringBill, VendorTerms
from accounting.recurring_payables_data import ASSOCIATIONS, FULL_NAMES, LEFT_OUT, ROWS
from core.models import Contact, Property

CODES = {code for code, _name, _kind in standard_chart.CHART}


def _letters(text):
    return re.sub(r'[^a-z0-9]', '', (text or '').lower())


def find_property(words):
    found = []
    for prop in Property.objects.filter(property_type=Property.Type.ASSOCIATION, is_active=True):
        name = prop.name.lower()
        if all((re.search(r'(?<!\d)' + re.escape(w) + r'(?!\d)', name) if w.isdigit() else w in name) for w in words):
            found.append(prop)
    return found


def find_vendor(name):
    """The contact we already have for this name (an exact match first, then the same letters and digits), or None."""
    name = FULL_NAMES.get(name, name)
    exact = Contact.objects.filter(name__iexact=name, contact_type=Contact.ContactType.VENDOR).first() or Contact.objects.filter(name__iexact=name).first()
    if exact:
        return exact, name
    key = _letters(name)
    for contact in Contact.objects.filter(contact_type=Contact.ContactType.VENDOR):
        if _letters(contact.name) == key:
            return contact, name
    return None, name


def next_due(day, today):
    day = min(day, 28)
    this = date(today.year, today.month, day)
    if this > today:
        return this
    return date(today.year + (today.month == 12), today.month % 12 + 1, day)


class Command(BaseCommand):
    help = 'Loads the approved recurring payables. Reports only, unless --apply is given.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Make the changes (without it, only report).')

    def handle(self, *args, **options):
        apply = options['apply']
        today = timezone.localdate()
        totals = {'create': 0, 'exists': 0, 'conflict': 0, 'skip': 0}
        self.stdout.write(self.style.WARNING('APPLYING' if apply else 'PREVIEW ONLY: nothing is written. Run again with --apply to load.'))
        for key, words in ASSOCIATIONS.items():
            rows = [r for r in ROWS if r[0] == key]
            props = find_property(words)
            if len(props) != 1:
                self.stdout.write(self.style.ERROR(f'\n[{key}] {"no association" if not props else "more than one association"} matches {words}: {len(rows)} bills not loaded. '
                                                   f'{[p.name for p in props]}'))
                totals['skip'] += len(rows)
                continue
            prop = props[0]
            profile = AssociationProfile.objects.filter(property=prop).first()
            keeps_books = bool(profile and profile.we_do_financials)
            have_chart = Account.objects.filter(association=prop).exists()
            self.stdout.write(f'\n== {prop.name}: books {"KEPT by us" if keeps_books else "not kept yet (bills load switched off)"}; chart of accounts {"present" if have_chart else "will be added"}')
            with transaction.atomic():
                if apply and not have_chart:
                    self.stdout.write(f'   added {ledger.ensure_chart(prop)} accounts')
                if apply:
                    ledger.profile_for(prop)
                for _k, vendor_name, desc, code, amount, how, match, day, active in rows:
                    totals[self._row(prop, keeps_books, vendor_name, desc, code, amount, how, match, day, active, today, apply)] += 1
        self.stdout.write('\nLeft out on purpose:')
        for line in LEFT_OUT:
            self.stdout.write('   - ' + line)
        self.stdout.write(self.style.SUCCESS(f'\n{"Loaded" if apply else "Would load"} {totals["create"]} bills; {totals["exists"]} already there; {totals["conflict"]} differ from what is there (left alone); '
                                             f'{totals["skip"]} not loaded.'))

    def _row(self, prop, keeps_books, vendor_name, desc, code, amount, how, match, day, active, today, apply):
        label = f'{vendor_name} - {desc}'
        if code not in CODES:
            self.stdout.write(self.style.ERROR(f'   NOT LOADED  {label}: account {code} is not in the standard chart'))
            return 'skip'
        vendor, full = find_vendor(vendor_name)
        shown = full if vendor is None else vendor.name
        money = Decimal(amount) if amount else None
        existing = None
        if vendor is not None:
            for rec in RecurringBill.objects.filter(association=prop, vendor=vendor):
                if rec.description.lower() == desc.lower() or (rec.amount == money and rec.pay_method == how) or (rec.amount is None and money is None):
                    existing = rec
                    break
        if existing is not None:
            same = existing.amount == money and existing.pay_method == how
            if same:
                self.stdout.write(f'   already there  {label}')
                return 'exists'
            self.stdout.write(self.style.WARNING(f'   DIFFERS  {label}: loaded as {self._what(existing.amount, existing.pay_method)} ({"on" if existing.is_active else "off"}); '
                                                 f'the review says {self._what(money, how)}. Left as it is.'))
            return 'conflict'
        is_on = bool(active and keeps_books)
        nxt = next_due(day, today)
        self.stdout.write(f'   {"loaded " if apply else "will load"}  {label}: {self._what(money, how)}, monthly from {nxt:%b} {nxt.day}, {"ON" if is_on else "off"}'
                          + (f'  [vendor: {"existing " + vendor.name if vendor else "NEW " + shown}]') + ('' if active else '  (a question is open on this one)'))
        if apply:
            if vendor is None:
                vendor = payables.vendor_for_name(shown)
            account = Account.objects.get(association=prop, code=code)
            RecurringBill.objects.create(association=prop, vendor=vendor, description=desc[:200], account=account, amount=money, frequency=RecurringBill.Frequency.MONTHLY, next_date=nxt,
                                         pay_method=how, statement_match=match[:80], is_active=is_on)
            VendorTerms.objects.get_or_create(association=prop, vendor=vendor, defaults={'default_account': account, 'pay_method': how})
        return 'create'

    @staticmethod
    def _what(amount, how):
        return (f'${amount:,.2f}' if amount is not None else 'amount varies') + (' by check' if how == 'check' else ' by ACH')
