"""Load the tenants and leases from the Yardi info sheet: the commercial tenants of 324, 325 and 381 NE 3rd Avenue and the three long-term rentals' tenants.

    manage.py load_yardi_tenants            (a dry run: everything is rolled back)
    manage.py load_yardi_tenants --apply

The facts are in this file (read from the sheet and the answers given in chat), so the command does not need the spreadsheet. It is safe to run again: a lease already there (same unit and
tenant, or same rental and tenant) is left alone except for blank fields it fills.
"""
from datetime import date
from decimal import Decimal as D

from django.core.management.base import BaseCommand
from django.db import transaction

from accounting import ledger
from accounting.models import BankAccount
from commercial import rent
from commercial.models import Lease, LongTermLease, SeasonalRent
from core.models import Contact, Property, Unit

# property name -> [units]: (unit label, tenant, email, lease from, lease to, monthly rent, monthly CAM estimate, rent rate expires, paid into (bank account name or None), extra)
COMMERCIAL = {
    '324 Lofts': [
        ('1', '324 Delray Restaurant LLC (Ethereal Restaurant)', 'etherealdelray@gmail.com', date(2025, 2, 28), date(2035, 2, 27), D('12832.50'), D('3992.00'), date(2027, 2, 28), None, {}),
        ('2', 'Direct Response Pros', 'Accounting@betteraddictioncare.com', date(2024, 3, 1), date(2030, 9, 21), D('10270.04'), D('3566.50'), date(2027, 2, 28), None, {}),
        ('3', 'Dandelight, LLC', 'office@dandelight.com', date(2023, 9, 1), date(2033, 8, 31), D('9141.21'), D('3549.00'), date(2027, 8, 31), '324 JMPC Acct', {}),
        ('4', 'Delray Energy Partners, LLC', 'tsherwood@clarencecapitalpartners.com', date(2024, 2, 1), date(2029, 1, 31), D('9003.50'), D('3564.40'), date(2027, 1, 31), None, {}),
        ('ROOFTOP', 'Dandelight, LLC', 'office@dandelight.com', date(2026, 5, 1), date(2028, 6, 30), D('1100.00'), D('0.00'), date(2027, 4, 30), '324 Other Acct',
         {'season': ('Off season (May to October)', 5, 10, D('850.00'))}),
    ],
    'Del Park LLC': [
        ('C', '325 Delray Cafe, LLC', 'glimmerdelray@gmail.com', date(2025, 4, 1), date(2030, 4, 30), D('4956.88'), D('1347.50'), date(2027, 3, 31), None, {}),
    ],
    'Red Barn 381': [
        ('MAIN', 'BCS Real Estate Management, Inc.', 'kim@bedners.com', date(2021, 4, 1), date(2032, 3, 1), D('16160.95'), D('5489.34'), date(2027, 3, 31), None, {}),
    ],
}

# rental name contains -> (tenant, email, phone, lease from, lease to, monthly rent, rent rate expires)
LONG_TERM = {
    '2036 Alta Meadows': ('Kershyra Myrick', 'kmyrick@jacksonville.edu', '(904) 210-9567', date(2026, 4, 1), date(2027, 3, 31), D('2600.00'), date(2027, 3, 31)),
    '225 NE 1st': ('Juan Lamus', 'juan.lamus.v@gmail.com', '', date(2026, 8, 1), date(2027, 7, 31), D('3125.00'), date(2027, 7, 31)),
    '300 Ross': ('Padgett', 'seemabwassmer@hotmail.com', '', date(2026, 7, 1), date(2027, 6, 30), D('3600.00'), date(2027, 6, 30)),
}


class Rollback(Exception):
    pass


def _tenant(name, email, phone=''):
    person = Contact.objects.filter(name__iexact=name, contact_type=Contact.ContactType.TENANT).first()
    if person is None:
        person = Contact.objects.create(name=name, contact_type=Contact.ContactType.TENANT, source=Contact.Source.MANUAL, email=email or '', phone=phone or '')
    else:
        changed = []
        for f, v in (('email', email), ('phone', phone)):
            if v and not getattr(person, f):
                setattr(person, f, v)
                changed.append(f)
        if changed:
            person.save(update_fields=changed)
    return person


class Command(BaseCommand):
    help = 'Load the Yardi tenants and leases (commercial and long-term). A dry run unless --apply.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **opts):
        try:
            with transaction.atomic():
                self.run()
                if not opts['apply']:
                    raise Rollback
        except Rollback:
            self.stdout.write('DRY RUN: rolled back.')
        else:
            self.stdout.write('APPLIED.')

    def run(self):
        for prop_name, rows in COMMERCIAL.items():
            prop = Property.objects.get(name=prop_name, property_type=Property.Type.COMMERCIAL)
            ledger.ensure_chart(prop)
            for label, tenant, email, start, end, rent_amount, cam, rate_expires, bank_name, extra in rows:
                unit = Unit.objects.get_or_create(property=prop, label=label)[0]
                person = _tenant(tenant, email)
                bank = BankAccount.objects.get(association=prop, name=bank_name) if bank_name else None
                lease, made = Lease.objects.get_or_create(property=prop, unit=unit, tenant=person, defaults=dict(
                    status=Lease.Status.ACTIVE, start_date=start, end_date=end, base_rent=rent_amount, cam_estimate=cam, rate_expires_on=rate_expires, bank_account=bank, billing_emails=email))
                if not made:
                    for f, v in (('bank_account', bank), ('rate_expires_on', rate_expires), ('billing_emails', email)):
                        if v and not getattr(lease, f):
                            setattr(lease, f, v)
                    lease.save()
                if 'season' in extra and not lease.seasons.exists():
                    name, first, last, amount = extra['season']
                    SeasonalRent.objects.create(lease=lease, name=name, start_month=first, end_month=last, monthly_rent=amount)
                rent.ensure_ownership(lease)
                self.stdout.write(f"  {'NEW ' if made else 'have'} {prop.name:<14} unit {label:<8} {person.name[:44]:<44} {start} to {end}  rent {rent_amount:>10}  CAM {cam:>8}  into {bank.name if bank else 'operating'}")
        for key, (tenant, email, phone, start, end, rent_amount, rate_expires) in LONG_TERM.items():
            prop = Property.objects.get(name__icontains=key, property_type=Property.Type.LONG_TERM_RENTAL)
            person = _tenant(tenant, email, phone)
            person.properties.add(prop)
            lease, made = LongTermLease.objects.get_or_create(property=prop, tenant=person, defaults=dict(
                status=LongTermLease.Status.ACTIVE, start_date=start, end_date=end, monthly_rent=rent_amount, rate_expires_on=rate_expires))
            note = f'  (the rental was set to {prop.monthly_rent}; the lease rent is now what the rent check expects)' if prop.monthly_rent not in (None, rent_amount) else ''
            self.stdout.write(f"  {'NEW ' if made else 'have'} {prop.name:<24} {person.name:<18} {start} to {end}  rent {rent_amount:>8}{note}")
