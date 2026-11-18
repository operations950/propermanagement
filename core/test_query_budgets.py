"""Query-count ceilings for the heaviest screens.

Each screen is loaded for an association with a handful of units, owners and open charges and the number of database queries is counted. The
ceilings are generous (about one and a half times what the screen needs today): they are not an exact figure to keep up, they are an alarm for
the day a screen goes back to asking the same question once for every unit, which is what made these screens slow. A second test for each
screen adds units and checks the count does not grow with them.
"""
from datetime import date
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounting import charges, ledger
from accounting.models import Charge
from accounting.test_collections_close import STORAGES
from accounting.tests import bank_for, make_assoc, owner_of
from core.models import StaffProfile, Unit

MONTH = '2026-10'

# screen -> ceiling in queries: about 1.5x what the screen took when this was written, with the menu's counts worked out cold and an association of four units.
CEILINGS = {
    'association list': 90,
    'owner collections': 45,
    'association property page': 108,
    'close sheet': 87,
    'close review': 90,
    'units': 40,
    'settings': 80,
    'bank rec': 56,
    'late fees': 60,
    'properties list': 56,
    'home': 38,
}


def screens(prop, bank):
    return {
        'association list': reverse('accounting:home') + '?month=' + MONTH,
        'owner collections': reverse('accounting:collections_home'),
        'association property page': reverse('property_detail', args=[prop.pk]),
        'close sheet': reverse('accounting:close', args=[prop.pk, MONTH]),
        'close review': reverse('accounting:close_review', args=[prop.pk, MONTH]),
        'units': reverse('accounting:units', args=[prop.pk]),
        'settings': reverse('accounting:settings', args=[prop.pk]),
        'bank rec': reverse('accounting:bank', args=[prop.pk, bank.pk, MONTH]),
        'late fees': reverse('accounting:late_fees', args=[prop.pk]),
        'properties list': reverse('property_list'),
        'home': reverse('ws_home'),
    }


@override_settings(STORAGES=STORAGES)
class QueryBudgetTests(TestCase):
    def setUp(self):
        self.boss = get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678')
        StaffProfile.objects.create(user=self.boss, role='admin', is_company_admin=True)
        self.client.force_login(self.boss)
        self.prop = make_assoc('Budget Condos')
        self.bank = bank_for(self.prop)
        self.fill(self.prop)

    def fill(self, prop, extra=0):
        """Every unit gets an owner and an open assessment; `extra` more units are added first."""
        for i in range(extra):
            Unit.objects.create(property=prop, label=f'2{i:02d}')
        for n, unit in enumerate(Unit.objects.filter(property=prop)):
            if unit.ownerships.exists():
                continue
            owner = owner_of(prop, unit, name=f'Owner {unit.label}')
            charges.post_charge(prop, unit, owner, Charge.Kind.ASSESSMENT, 'Monthly assessment', date(2026, 10, 1), D('300'))

    def count(self, url):
        cache.delete('nudges:nav')                  # the menu's counts are kept for two minutes; count a page that has to work them out
        with CaptureQueriesContext(connection) as ctx:
            r = self.client.get(url)
        self.assertEqual(r.status_code, 200, url)
        return len(ctx)

    def test_every_screen_stays_under_its_ceiling(self):
        for name, url in screens(self.prop, self.bank).items():
            n = self.count(url)
            self.assertLessEqual(n, CEILINGS[name], f'{name} took {n} queries (ceiling {CEILINGS[name]})')

    def test_more_units_do_not_mean_more_queries(self):
        before = {name: self.count(url) for name, url in screens(self.prop, self.bank).items()}
        self.fill(self.prop, extra=12)
        grew = []
        for name, url in screens(self.prop, self.bank).items():
            after = self.count(url)
            if after > before[name] + 6:
                grew.append(f'{name}: {before[name]} queries with 4 units, {after} with 16')
        self.assertEqual(grew, [])


# ---------------------------------------------------------------- the newspaper screens and the rest of the staff screens

NEW_CEILINGS = {          # screen -> ceiling in queries (about 1.5x what it took when this was written, menu counts worked out cold, a small portfolio)
    'contacts': 56,
    'contacts owner': 53,
    'contacts vendor': 53,
    'tickets now': 47,
    'tickets later': 47,
    'compliance items': 30,
    'compliance rules': 30,
    'projects active': 30,
    'projects templates': 30,
    'turnovers week': 50,
    'turnovers day': 50,
    'onsite visits': 50,
    'reservations': 50,
    'calendar': 40,
    'performance': 60,
    'phone menu': 30,
    'properties list': 60,
    'month-end close home': 104,
    'close overview': 40,
    'tourist tax': 40,
    'payables': 50,
    'budgets': 50,
}


SLACK = {'budgets': 12}          # the budgets list reads a couple of small rows for each association it lists, and the extra properties here are listed too


def new_screens():
    from django.utils import timezone as tz
    return {
        'contacts': reverse('contact_list'),
        'contacts owner': reverse('contact_list') + '?type=owner',
        'contacts vendor': reverse('contact_list') + '?type=vendor',
        'tickets now': reverse('ticket_list') + '?horizon=now',
        'tickets later': reverse('ticket_list') + '?horizon=later',
        'compliance items': reverse('ws_compliance') + '?tab=items',
        'compliance rules': reverse('ws_compliance') + '?tab=rules',
        'projects active': reverse('ws_projects') + '?tab=active',
        'projects templates': reverse('ws_projects') + '?tab=templates',
        'turnovers week': reverse('onsite_turnovers'),
        'turnovers day': reverse('onsite_turnovers') + '?day=' + tz.localdate().isoformat(),
        'onsite visits': reverse('onsite_dashboard'),
        'reservations': reverse('onsite_reservation_list'),
        'calendar': reverse('onsite_calendar'),
        'performance': reverse('onsite_performance'),
        'phone menu': '/m/',
        'properties list': reverse('property_list'),
        'month-end close home': reverse('close_home'),
        'close overview': reverse('close_overview'),
        'tourist tax': reverse('tourist_tax'),
        'payables': reverse('accounting:payables_home'),
        'budgets': reverse('accounting:budgets'),
    }


@override_settings(STORAGES=STORAGES)
class NewScreenBudgetTests(TestCase):
    def setUp(self):
        from core.models import Contact, Property
        self.boss = get_user_model().objects.create_superuser('boss2', 'b2@example.com', 'pw12345678')
        StaffProfile.objects.create(user=self.boss, role='admin', is_company_admin=True, new_face=True)
        self.client.force_login(self.boss)
        self.props = [Property.objects.create(name=f'Place {i}', property_type=Property.Type.ASSOCIATION, city='Boca Raton', state='FL', street='1 Main St', zip_code='33432') for i in range(2)]
        self.grow(4)

    def grow(self, n):
        """n more people (owners, vendors with different trades, tenants), each tied to a property, and n more properties of different kinds with their own units."""
        from core.models import Contact, Property
        base = Contact.objects.count()
        for i in range(n):
            for kind, trade in (('owner', ''), ('vendor', f'trade{(base + i) % 5}'), ('tenant', ''), ('board_member', '')):
                c = Contact.objects.create(name=f'{kind} {base + i}', contact_type=kind, trade=trade, phone=f'561555{(base + i) % 10000:04d}', email=f'p{base + i}{kind}@example.com')
                c.properties.add(self.props[(base + i) % len(self.props)])
        for i in range(n):
            kind = (Property.Type.COMMERCIAL, Property.Type.LONG_TERM_RENTAL, Property.Type.SHORT_TERM_RENTAL)[i % 3]
            p = Property.objects.create(name=f'Extra {base + i}', property_type=kind, city='Boca Raton', state='FL', street='2 Main St', zip_code='33432')
            Unit.objects.create(property=p, label='A')

    def count(self, url):
        cache.delete('nudges:nav')
        cache.delete('contacts:duplicate-group-count')
        with CaptureQueriesContext(connection) as ctx:
            r = self.client.get(url)
        self.assertIn(r.status_code, (200, 302), url)          # a few old addresses forward to the screen that replaced them
        return len(ctx)

    def test_every_screen_stays_under_its_ceiling(self):
        over = []
        for name, url in new_screens().items():
            n = self.count(url)
            if n > NEW_CEILINGS[name]:
                over.append(f'{name} took {n} queries (ceiling {NEW_CEILINGS[name]})')
        self.assertEqual(over, [])

    def test_more_people_and_properties_do_not_mean_more_queries(self):
        before = {name: self.count(url) for name, url in new_screens().items()}
        self.grow(10)
        grew = []
        for name, url in new_screens().items():
            after = self.count(url)
            if after > before[name] + SLACK.get(name, 4):
                grew.append(f'{name}: {before[name]} queries with 4 of each, {after} with 14')
        self.assertEqual(grew, [])
