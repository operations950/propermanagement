"""Tax numbers on short-term rentals, the tourist tax split of the county payment, and the owner portal calendar."""
import io
from datetime import date, datetime, time, timedelta
from decimal import Decimal as D
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import owner_calendar, tourist_tax
from .models import Contact, OwnerAccount, Property

NO_MANIFEST = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
               'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}


def _str(name='800 Tropic', **kw):
    return Property.objects.create(name=name, property_type=Property.Type.SHORT_TERM_RENTAL, financials_managed=True, **kw)


@override_settings(STORAGES=NO_MANIFEST)
class TaxNumbersOnTheRentalTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.prop = _str()
        self.url = reverse('property_detail', args=[self.prop.pk])

    def test_the_numbers_are_saved_from_the_rental_page(self):
        r = self.client.post(self.url, {'action': 'qb_set_tax_ids', 'tdt_account_number': '  1234  5678 ', 'business_tax_receipt_number': 'BTR-9', 'business_tax_receipt_renews_on': '2027-09-30'})
        self.assertEqual(r.status_code, 302)
        self.prop.refresh_from_db()
        self.assertEqual((self.prop.tdt_account_number, self.prop.business_tax_receipt_number, self.prop.business_tax_receipt_renews_on), ('1234 5678', 'BTR-9', date(2027, 9, 30)))
        page = self.client.get(self.url).content.decode()
        self.assertIn('id="tax-ids"', page)
        self.assertIn('value="1234 5678"', page)
        self.assertIn('value="2027-09-30"', page)

    def test_blank_boxes_clear_them(self):
        self.prop.tdt_account_number = '1'
        self.prop.save()
        self.client.post(self.url, {'action': 'qb_set_tax_ids', 'tdt_account_number': '', 'business_tax_receipt_number': '', 'business_tax_receipt_renews_on': ''})
        self.prop.refresh_from_db()
        self.assertEqual((self.prop.tdt_account_number, self.prop.business_tax_receipt_renews_on), ('', None))

    def test_a_bad_date_changes_nothing_and_says_so(self):
        self.prop.tdt_account_number = 'keep'
        self.prop.save()
        r = self.client.post(self.url, {'action': 'qb_set_tax_ids', 'tdt_account_number': 'new', 'business_tax_receipt_renews_on': 'not a date'}, follow=True)
        self.assertContains(r, 'Type the renewal date as a date.')
        self.prop.refresh_from_db()
        self.assertEqual(self.prop.tdt_account_number, 'keep')

    def test_only_a_short_term_rental_has_them(self):
        ltr = Property.objects.create(name='12 Oak', property_type=Property.Type.LONG_TERM_RENTAL, financials_managed=True)
        r = self.client.post(reverse('property_detail', args=[ltr.pk]), {'action': 'qb_set_tax_ids', 'tdt_account_number': 'x'}, follow=True)
        self.assertContains(r, 'Only a short-term rental has a tourist tax account')
        ltr.refresh_from_db()
        self.assertEqual(ltr.tdt_account_number, '')
        self.assertNotContains(self.client.get(reverse('property_detail', args=[ltr.pk])), 'id="tax-ids"')

    def test_a_staff_member_who_is_not_an_admin_cannot_change_them(self):
        staff = get_user_model().objects.create_user('helper', 'h@example.com', 'pw12345678')
        self.client.force_login(staff)
        self.client.post(self.url, {'action': 'qb_set_tax_ids', 'tdt_account_number': 'sneaky'})
        self.prop.refresh_from_db()
        self.assertEqual(self.prop.tdt_account_number, '')

    def test_the_renewal_date_turns_amber_then_red(self):
        today = date(2026, 11, 14)
        p = Property(business_tax_receipt_renews_on=None)
        self.assertEqual(p.business_tax_receipt_state(today), ('', None))
        p.business_tax_receipt_renews_on = today + timedelta(days=200)
        self.assertEqual(p.business_tax_receipt_state(today), ('ok', 200))
        p.business_tax_receipt_renews_on = today + timedelta(days=60)
        self.assertEqual(p.business_tax_receipt_state(today), ('soon', 60))
        p.business_tax_receipt_renews_on = today
        self.assertEqual(p.business_tax_receipt_state(today), ('soon', 0))
        p.business_tax_receipt_renews_on = today - timedelta(days=3)
        self.assertEqual(p.business_tax_receipt_state(today), ('overdue', -3))

    def test_a_passed_date_is_flagged_on_the_page(self):
        self.prop.business_tax_receipt_renews_on = timezone.localdate() - timedelta(days=5)
        self.prop.save()
        self.assertContains(self.client.get(self.url), 'Renewal date passed 5 days ago')


def _rep(props, month=date(2026, 10, 1)):
    """A tourist tax report for the screen, built by hand: (property, taxable) pairs, each its own return."""
    rows = []
    for prop, taxable in props:
        tax = tourist_tax.tax_for(taxable)
        allowance = tourist_tax.allowance_for(tax)
        rows.append({'property': prop, 'lines': [], 'deposited': taxable, 'fees': D('0.00'), 'taxable': taxable, 'unmatched': D('0.00'), 'missing_fee': 0, 'days': 10,
                     'tax': tax, 'allowance': allowance, 'due': tax - allowance})
    rep = {'month': month, 'rows': rows, 'deposited': sum((r['deposited'] for r in rows), D('0')), 'fees': D('0.00'), 'taxable': sum((r['taxable'] for r in rows), D('0')),
           'tax': sum((r['tax'] for r in rows), D('0')), 'allowance': sum((r['allowance'] for r in rows), D('0')), 'due': sum((r['due'] for r in rows), D('0')),
           'days': sum(r['days'] for r in rows), 'flags': [], 'due_on': date(2026, 11, 1), 'late_after': date(2026, 11, 20), 'in_progress': False, 'late': False, 'rate': D('0.06')}
    return tourist_tax.attach_filings(rep)


@override_settings(STORAGES=NO_MANIFEST)
class TouristTaxSplitTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.a = _str('800 Tropic', tdt_account_number='TDT-111')
        self.b = _str('702 SW 1st')
        self.c = _str('323 Decarie', tdt_account_number='TDT-333')
        self.rep = _rep([(self.a, D('1192.70')), (self.b, D('2500.00')), (self.c, D('310.10'))])
        self.url = reverse('tourist_tax') + '?month=2026-10'

    def get(self, extra=''):
        with mock.patch.object(tourist_tax, 'report', return_value=self.rep):
            return self.client.get(self.url + extra)

    def test_the_shares_add_up_to_the_county_payment(self):
        rows = tourist_tax.split_rows(self.rep)
        self.assertEqual([r['property'].name for r in rows], ['800 Tropic', '702 SW 1st', '323 Decarie'])
        self.assertEqual(sum((r['due'] for r in rows), D('0')), self.rep['due'])
        self.assertEqual(rows[0]['due'], D('69.79'))                      # the county's own sample return: $1,192.70 -> tax 71.57, allowance 1.78, due 69.79
        self.assertEqual(rows[0]['tdt'], 'TDT-111')

    def test_the_screen_shows_the_split_with_account_numbers_and_the_total(self):
        page = self.get().content.decode()
        self.assertIn('id="payment-split"', page)
        self.assertIn('Split of the county payment', page)
        self.assertIn('TDT-111', page)
        self.assertIn('TDT-333', page)
        self.assertIn(f'${self.rep["due"]:,.2f}', page)
        self.assertIn('split=print', page)
        self.assertIn('split=csv', page)
        self.assertIn('split=xlsx', page)

    def test_every_property_return_shows_its_tdt_number_or_asks_for_it(self):
        page = self.get().content.decode()
        self.assertIn('TDT TDT-111', page)
        self.assertIn('Not entered', page)                                # 702 SW 1st has none yet

    def test_the_split_downloads_as_csv_with_a_total_row(self):
        r = self.get('&split=csv')
        self.assertEqual(r['Content-Type'], 'text/csv')
        self.assertIn('tourist-tax-split-2026-10.csv', r['Content-Disposition'])
        lines = r.content.decode().splitlines()
        self.assertIn('800 Tropic,TDT-111,1192.70,71.57,1.78,69.79,no,', lines[2] + ',')
        self.assertTrue(lines[-1].startswith('TOTAL: the county payment,'))
        self.assertIn(f'{self.rep["due"]:.2f}', lines[-1])

    def test_the_split_downloads_as_excel_with_numbers_that_add_up(self):
        import openpyxl
        r = self.get('&split=xlsx')
        self.assertIn('spreadsheetml', r['Content-Type'])
        ws = openpyxl.load_workbook(io.BytesIO(r.content)).active
        data = [[c.value for c in row] for row in ws.iter_rows(min_row=3)]
        shares = [row[5] for row in data[:-1]]
        self.assertEqual(sum(D(str(x)) for x in shares), D(str(data[-1][5])))
        self.assertEqual(D(str(data[-1][5])), self.rep['due'])
        self.assertEqual(data[0][1], 'TDT-111')

    def test_the_printable_view_has_the_table_and_the_total_only(self):
        r = self.get('&split=print')
        self.assertTemplateUsed(r, 'core/tourist_tax_split.html')
        self.assertContains(r, 'The county payment')
        self.assertContains(r, 'Print / save as PDF')
        self.assertNotContains(r, 'Tax numbers')

    def test_a_property_marked_filed_says_so_in_the_split(self):
        from .models import TouristTaxFiling
        TouristTaxFiling.objects.create(month=date(2026, 10, 1), property=self.a, amount=D('69.79'), filed_on=date(2026, 11, 3))
        self.rep = tourist_tax.attach_filings(self.rep)
        self.assertEqual(self.rep['unfiled'], 2)
        page = self.get().content.decode()
        self.assertIn('2 of the 3 returns are not marked as filed yet', page)

    def test_the_tax_numbers_table_saves_every_rental_in_one_go(self):
        r = self.client.post(self.url, {'action': 'save_tax_ids', f'tdt_{self.a.pk}': 'A-1', f'btr_{self.a.pk}': 'R-1', f'renews_{self.a.pk}': '2027-01-31',
                                        f'tdt_{self.b.pk}': ' B 2 ', f'btr_{self.b.pk}': '', f'renews_{self.b.pk}': '',
                                        f'tdt_{self.c.pk}': 'TDT-333', f'btr_{self.c.pk}': '', f'renews_{self.c.pk}': ''})
        self.assertEqual(r.status_code, 302)
        for p in (self.a, self.b, self.c):
            p.refresh_from_db()
        self.assertEqual((self.a.tdt_account_number, self.a.business_tax_receipt_number, self.a.business_tax_receipt_renews_on), ('A-1', 'R-1', date(2027, 1, 31)))
        self.assertEqual(self.b.tdt_account_number, 'B 2')
        self.assertEqual(self.c.tdt_account_number, 'TDT-333')

    def test_a_bad_date_in_the_table_skips_that_rental_only(self):
        r = self.client.post(self.url, {'action': 'save_tax_ids', f'tdt_{self.a.pk}': 'A-1', f'renews_{self.a.pk}': 'soon', f'tdt_{self.b.pk}': 'B-2', f'renews_{self.b.pk}': ''}, follow=True)
        self.assertContains(r, 'The renewal date is not a date for: 800 Tropic')
        self.a.refresh_from_db()
        self.b.refresh_from_db()
        self.assertEqual((self.a.tdt_account_number, self.b.tdt_account_number), ('TDT-111', 'B-2'))

    def test_only_an_admin_gets_the_screen(self):
        self.client.force_login(get_user_model().objects.create_user('helper', 'h@example.com', 'pw12345678'))
        self.assertNotEqual(self.get().status_code, 200)


def _at(day, hour=15):
    return timezone.make_aware(datetime.combine(day, time(hour, 0)))


@override_settings(STORAGES=NO_MANIFEST)
class OwnerPortalCalendarTests(TestCase):
    def setUp(self):
        from onsite.models import Booking, BookingFeed, CalendarBlock
        self.Booking, self.BookingFeed, self.CalendarBlock = Booking, BookingFeed, CalendarBlock
        self.today = timezone.localdate()
        self.nxt = owner_calendar.add_months(owner_calendar.first_of(self.today), 1)
        self.prop = _str('800 Tropic', owner_portal_open=True)
        self.other = _str('Not Mine', owner_portal_open=True)
        owner = Contact.objects.create(name='Olive Owner', contact_type=Contact.ContactType.OWNER, email='olive@example.com')
        owner.properties.add(self.prop)
        user = get_user_model().objects.create_user('ownerportal-test1', password='pw12345678')
        OwnerAccount.objects.create(user=user, email='olive@example.com')
        self.client.force_login(user)
        self.url = reverse('owner_calendar', args=[self.prop.pk])

    def stay(self, first_night, nights, source='airbnb', on_calendar=True, guest='Zebediah Guest', uid=None):
        return self.Booking.objects.create(property=self.prop, source=source, external_uid=uid or f'{source}-{first_night:%m%d}-{nights}', guest_name=guest,
                                           check_in=_at(first_night), check_out=_at(first_night + timedelta(days=nights), 11), on_calendar=on_calendar)

    def cells(self, month=None):
        r = self.client.get(self.url + (f'?month={month:%Y-%m}' if month else ''))
        self.assertEqual(r.status_code, 200)
        grid = r.context['cal']['grids'][0]
        return r, {c['date']: c for week in grid['weeks'] for c in week if c['in_month']}

    def test_booked_nights_are_drawn_as_booked_and_the_rest_are_open(self):
        d = self.nxt + timedelta(days=9)
        self.stay(d, 3)
        r, cells = self.cells(self.nxt)
        self.assertEqual([cells[d + timedelta(days=i)]['status'] for i in range(3)], ['booked'] * 3)
        self.assertEqual(cells[d + timedelta(days=3)]['status'], '')              # the check-out day is not a night
        self.assertEqual(cells[d - timedelta(days=1)]['status'], '')
        self.assertContains(r, 'Booked')

    def test_no_guest_name_and_no_owner_name_is_ever_on_the_page(self):
        self.stay(self.nxt + timedelta(days=2), 4, guest='Zebediah Guest')
        r = self.client.get(self.url + f'?month={self.nxt:%Y-%m}')
        self.assertNotContains(r, 'Zebediah')
        self.assertNotContains(r, 'Olive Owner')

    def test_an_in_house_stay_is_an_owner_stay(self):
        d = self.nxt + timedelta(days=4)
        self.stay(d, 2, source='manual', on_calendar=False, guest='Olive Owner', uid='m1')
        r, cells = self.cells(self.nxt)
        self.assertEqual(cells[d]['status'], 'owner')
        self.assertContains(r, 'Owner stay')

    def test_blocked_nights_nothing_explains_are_blocked(self):
        feed = self.BookingFeed.objects.create(property=self.prop, source='airbnb', is_active=True)
        d = self.nxt + timedelta(days=14)
        self.CalendarBlock.objects.create(feed=feed, property=self.prop, source='airbnb', start=d, end=d + timedelta(days=2))
        r, cells = self.cells(self.nxt)
        self.assertEqual([cells[d + timedelta(days=i)]['status'] for i in range(2)], ['blocked', 'blocked'])
        self.assertEqual(cells[d + timedelta(days=2)]['status'], '')

    def test_a_block_under_a_stay_stays_booked(self):
        feed = self.BookingFeed.objects.create(property=self.prop, source='airbnb', is_active=True)
        d = self.nxt + timedelta(days=6)
        self.stay(d, 3)
        self.CalendarBlock.objects.create(feed=feed, property=self.prop, source='airbnb', start=d, end=d + timedelta(days=3))
        _r, cells = self.cells(self.nxt)
        self.assertEqual([cells[d + timedelta(days=i)]['status'] for i in range(3)], ['booked'] * 3)

    def test_a_stay_only_a_payment_report_knows_is_not_drawn_ahead(self):
        d = self.nxt + timedelta(days=8)
        self.stay(d, 3, source='vrbo', on_calendar=False)
        _r, cells = self.cells(self.nxt)
        self.assertEqual(cells[d]['status'], '')

    def test_a_cancelled_reservation_is_not_drawn(self):
        d = self.nxt + timedelta(days=8)
        b = self.stay(d, 3)
        b.status = self.Booking.Status.CANCELLED
        b.save()
        _r, cells = self.cells(self.nxt)
        self.assertEqual(cells[d]['status'], '')

    def test_a_building_gets_a_calendar_for_each_unit(self):
        from .models import Unit
        a = Unit.objects.create(property=self.prop, label='A')
        Unit.objects.create(property=self.prop, label='B')
        d = self.nxt + timedelta(days=5)
        self.Booking.objects.create(property=self.prop, unit=a, source='airbnb', external_uid='u-a', guest_name='G', check_in=_at(d), check_out=_at(d + timedelta(days=2), 11), on_calendar=True)
        r = self.client.get(self.url + f'?month={self.nxt:%Y-%m}')
        grids = r.context['cal']['grids']
        self.assertEqual([g['label'] for g in grids], ['A', 'B'])
        self.assertEqual(grids[0]['counts']['booked'], 2)
        self.assertEqual(grids[1]['counts']['booked'], 0)
        self.assertContains(r, 'Unit A')

    def test_the_month_is_held_to_the_current_month_and_the_eleven_after(self):
        first = owner_calendar.first_of(self.today)
        self.assertEqual(self.client.get(self.url + '?month=2001-01').context['cal']['month'], first)
        self.assertEqual(self.client.get(self.url + '?month=2099-12').context['cal']['month'], owner_calendar.add_months(first, 11))
        self.assertEqual(self.client.get(self.url + '?month=garbage').context['cal']['month'], first)
        cal = self.client.get(self.url).context['cal']
        self.assertIsNone(cal['previous'])
        self.assertEqual(len(cal['months']), 12)
        self.assertEqual(self.client.get(self.url + f'?month={owner_calendar.add_months(first, 11):%Y-%m}').context['cal']['next'], None)

    def test_a_rental_that_is_not_theirs_or_whose_portal_is_closed_is_not_found(self):
        self.assertEqual(self.client.get(reverse('owner_calendar', args=[self.other.pk])).status_code, 404)
        self.prop.owner_portal_open = False
        self.prop.save()
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_the_portal_home_links_to_the_calendar(self):
        r = self.client.get(reverse('owner_home'))
        self.assertContains(r, self.url)
        self.assertContains(r, 'Open the calendar')

    def test_it_works_on_a_phone(self):
        r = self.client.get(self.url)
        self.assertContains(r, 'name="viewport"')
        self.assertContains(r, 'repeat(7, minmax(0, 1fr))')
        self.assertContains(r, '@media (max-width: 575.98px)')

    def test_a_signed_out_visitor_is_sent_to_the_login(self):
        self.client.logout()
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 302)
        self.assertIn(reverse('owner_login'), r['Location'])
