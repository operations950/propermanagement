"""Commercial buildings: the books switch, the lease and its rent, posting ahead of the due date, tenant invoices, the opening balances, and the screens."""
import io
import tempfile
from datetime import date
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from accounting import board_send, closing, labels, ledger, payables_hub
from accounting import standard_chart as sc
from accounting.models import Account, AssociationProfile, BankAccount, BoardTerm, Charge, JournalEntry, MonthLock, Ownership, Receipt
from core.models import Contact, Property, Unit

from . import invoices, opening, rent
from .models import CommercialSettings, Lease, LeaseDocument, RentStep, SeasonalRent, TenantInvoice

LOCMEM = 'django.core.mail.backends.locmem.EmailBackend'
STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}


def make_commercial(name='Del Park LLC'):
    prop = Property.objects.create(name=name, property_type=Property.Type.COMMERCIAL, address='325 NE 3rd Ave, Delray Beach, FL 33483')
    ledger.ensure_chart(prop)
    return prop


def bank_for(prop, last4='4465'):
    return BankAccount.objects.create(association=prop, name='Operating', bank_name='Truist', last4=last4, account=ledger.account(prop, '1110'))


def make_lease(prop, label='C', tenant='Sky Dining LLC', rent_amount='5000', start=date(2026, 1, 1), end=None, cam='0', **kw):
    unit = Unit.objects.get_or_create(property=prop, label=label)[0]
    person = Contact.objects.create(name=tenant, contact_type=Contact.ContactType.TENANT, email='pay@tenant.example')
    lease = Lease.objects.create(property=prop, unit=unit, tenant=person, start_date=start, end_date=end, base_rent=D(rent_amount), cam_estimate=D(cam), **kw)
    rent.ensure_ownership(lease)
    return lease


class BooksSwitchTests(TestCase):
    def test_a_new_commercial_property_starts_with_its_books_on(self):
        prop = Property.objects.create(name='New Plaza', property_type=Property.Type.COMMERCIAL)
        self.assertTrue(AssociationProfile.objects.get(property=prop).we_do_financials)

    def test_an_association_is_not_switched_on(self):
        prop = Property.objects.create(name='Some HOA', property_type=Property.Type.ASSOCIATION)
        self.assertFalse(ledger.profile_for(prop).we_do_financials)

    def test_the_command_turns_the_books_on_and_makes_the_chart_and_is_safe_to_repeat(self):
        prop = Property.objects.create(name='Old Plaza', property_type=Property.Type.COMMERCIAL)
        AssociationProfile.objects.filter(property=prop).update(we_do_financials=False)
        out = io.StringIO()
        call_command('enable_commercial_financials', stdout=out)
        self.assertTrue(ledger.profile_for(prop).we_do_financials)
        codes = set(Account.objects.filter(association=prop).values_list('code', flat=True))
        self.assertTrue({'4100', '4110', '4120', '2255'} <= codes)
        count = Account.objects.filter(association=prop).count()
        out = io.StringIO()
        call_command('enable_commercial_financials', stdout=out)
        self.assertEqual(Account.objects.filter(association=prop).count(), count)
        self.assertIn('already on', out.getvalue())

    def test_the_rent_accounts_are_only_in_a_commercial_chart(self):
        hoa = Property.objects.create(name='Some HOA', property_type=Property.Type.ASSOCIATION)
        ledger.ensure_chart(hoa)
        self.assertFalse(Account.objects.filter(association=hoa, code__in=['4100', '4110', '4120', '2255']).exists())

    def test_the_commercial_accounts_are_income_and_liability(self):
        prop = make_commercial()
        self.assertEqual(Account.objects.get(association=prop, code='4100').kind, 'income')
        self.assertEqual(Account.objects.get(association=prop, code='2255').kind, 'liability')


class RentForMonthTests(TestCase):
    def setUp(self):
        self.prop = make_commercial()

    def test_base_rent_every_month(self):
        lease = make_lease(self.prop, rent_amount='5000', cam='400')
        r = rent.rent_for_month(lease, date(2026, 6, 1))
        self.assertEqual((r['rent'], r['cam'], r['source'], r['prorated']), (D('5000.00'), D('400.00'), 'base', False))

    def test_a_rent_step_on_the_first_starts_that_month(self):
        lease = make_lease(self.prop, rent_amount='5000')
        RentStep.objects.create(lease=lease, effective_date=date(2026, 7, 1), monthly_rent=D('5250'))
        self.assertEqual(rent.rent_for_month(lease, date(2026, 6, 1))['rent'], D('5000.00'))
        self.assertEqual(rent.rent_for_month(lease, date(2026, 7, 1))['rent'], D('5250.00'))
        self.assertEqual(rent.rent_for_month(lease, date(2026, 12, 1))['source'], 'step')

    def test_a_rent_step_in_the_middle_of_a_month_starts_the_next_month(self):
        lease = make_lease(self.prop, rent_amount='5000')
        RentStep.objects.create(lease=lease, effective_date=date(2026, 7, 15), monthly_rent=D('5250'))
        self.assertEqual(rent.rent_for_month(lease, date(2026, 7, 1))['rent'], D('5000.00'))
        self.assertEqual(rent.rent_for_month(lease, date(2026, 8, 1))['rent'], D('5250.00'))

    def test_the_latest_step_wins(self):
        lease = make_lease(self.prop, rent_amount='5000')
        RentStep.objects.create(lease=lease, effective_date=date(2026, 3, 1), monthly_rent=D('5100'))
        RentStep.objects.create(lease=lease, effective_date=date(2026, 9, 1), monthly_rent=D('5300'))
        self.assertEqual(rent.rent_for_month(lease, date(2026, 5, 1))['rent'], D('5100.00'))
        self.assertEqual(rent.rent_for_month(lease, date(2026, 10, 1))['rent'], D('5300.00'))

    def test_a_seasonal_rent_replaces_the_base_in_its_months_every_year(self):
        lease = make_lease(self.prop, rent_amount='5000')
        SeasonalRent.objects.create(lease=lease, name='Winter', start_month=11, end_month=4, monthly_rent=D('7000'))
        self.assertEqual(rent.rent_for_month(lease, date(2026, 12, 1))['rent'], D('7000.00'))
        self.assertEqual(rent.rent_for_month(lease, date(2027, 2, 1))['rent'], D('7000.00'))
        self.assertEqual(rent.rent_for_month(lease, date(2026, 6, 1))['rent'], D('5000.00'))
        self.assertEqual(rent.rent_for_month(lease, date(2026, 12, 1))['label'], 'Winter')

    def test_a_season_beats_a_step_in_its_months_and_the_step_holds_the_other_months(self):
        lease = make_lease(self.prop, rent_amount='5000')
        RentStep.objects.create(lease=lease, effective_date=date(2026, 1, 1), monthly_rent=D('5500'))
        SeasonalRent.objects.create(lease=lease, start_month=1, end_month=3, monthly_rent=D('8000'))
        self.assertEqual(rent.rent_for_month(lease, date(2026, 2, 1))['rent'], D('8000.00'))
        self.assertEqual(rent.rent_for_month(lease, date(2026, 5, 1))['rent'], D('5500.00'))

    def test_a_first_month_that_is_only_part_of_a_month_is_charged_by_the_day(self):
        lease = make_lease(self.prop, rent_amount='3100', start=date(2026, 7, 16), cam='310')          # July: 16th to 31st is 16 of 31 days
        r = rent.rent_for_month(lease, date(2026, 7, 1))
        self.assertEqual((r['rent'], r['cam'], r['prorated'], r['days'], r['of']), (D('1600.00'), D('160.00'), True, 16, 31))
        self.assertEqual(rent.rent_for_month(lease, date(2026, 6, 1))['rent'], D('0.00'))          # before the lease

    def test_a_last_month_is_charged_by_the_day_and_the_month_after_is_nothing(self):
        lease = make_lease(self.prop, rent_amount='3000', end=date(2026, 9, 15))                  # September: 15 of 30 days
        self.assertEqual(rent.rent_for_month(lease, date(2026, 9, 1))['rent'], D('1500.00'))
        self.assertEqual(rent.rent_for_month(lease, date(2026, 10, 1))['rent'], D('0.00'))

    def test_proration_can_be_switched_off(self):
        lease = make_lease(self.prop, rent_amount='3100', start=date(2026, 7, 16), prorate=False)
        r = rent.rent_for_month(lease, date(2026, 7, 1))
        self.assertEqual((r['rent'], r['prorated']), (D('3100.00'), False))

    def test_a_lease_marked_ended_with_no_end_date_bills_nothing(self):
        lease = make_lease(self.prop, status=Lease.Status.ENDED)
        self.assertEqual(rent.rent_for_month(lease, date(2026, 6, 1))['rent'], D('0.00'))

    def test_yearly_increases_by_percent_and_by_dollars(self):
        lease = make_lease(self.prop, rent_amount='1000')
        made = rent.generate_steps(lease, date(2027, 1, 1), 3, percent=D('3'))
        self.assertEqual([s.monthly_rent for s in made], [D('1030.00'), D('1060.90'), D('1092.73')])
        lease2 = make_lease(self.prop, label='A', tenant='Other', rent_amount='2000')
        rent.generate_steps(lease2, date(2027, 6, 1), 2, amount=D('100'))
        self.assertEqual(rent.rent_for_month(lease2, date(2028, 7, 1))['rent'], D('2200.00'))
        with self.assertRaises(ValueError):
            rent.generate_steps(lease2, date(2027, 6, 1), 2)

    def test_the_schedule_shows_twelve_months(self):
        lease = make_lease(self.prop, rent_amount='5000')
        RentStep.objects.create(lease=lease, effective_date=date(2027, 1, 1), monthly_rent=D('5200'))
        s = rent.schedule(lease, date(2026, 11, 1), 12)
        self.assertEqual((len(s), s[0]['month'], s[0]['rent'], s[2]['rent'], s[-1]['month']), (12, date(2026, 11, 1), D('5000.00'), D('5200.00'), date(2027, 10, 1)))


class TenantRowTests(TestCase):
    def setUp(self):
        self.prop = make_commercial()

    def test_a_lease_makes_the_tenant_the_billed_person_on_the_unit(self):
        lease = make_lease(self.prop)
        own = Ownership.objects.get(unit=lease.unit)
        self.assertEqual((own.person, own.is_billed, own.end_date), (lease.tenant, True, None))
        self.assertEqual(lease.ownership, own)
        lease.tenant.refresh_from_db()
        self.assertIn(self.prop, lease.tenant.properties.all())

    def test_saving_again_does_not_make_a_second_row(self):
        lease = make_lease(self.prop)
        rent.ensure_ownership(lease)
        rent.ensure_ownership(lease)
        self.assertEqual(Ownership.objects.filter(unit=lease.unit).count(), 1)

    def test_an_ended_lease_ends_the_tenant_row(self):
        lease = make_lease(self.prop)
        lease.status, lease.end_date = Lease.Status.ENDED, date(2026, 8, 31)
        lease.save()
        rent.ensure_ownership(lease)
        own = Ownership.objects.get(unit=lease.unit)
        self.assertEqual((own.end_date, own.is_billed), (date(2026, 8, 31), False))

    def test_a_contact_of_another_type_becomes_a_tenant_too(self):
        owner = Contact.objects.create(name='Pat', contact_type=Contact.ContactType.OWNER)
        unit = Unit.objects.create(property=self.prop, label='B')
        lease = Lease.objects.create(property=self.prop, unit=unit, tenant=owner, start_date=date(2026, 1, 1), base_rent=D('900'))
        rent.ensure_ownership(lease)
        owner.refresh_from_db()
        self.assertIn(Contact.ContactType.TENANT, owner.secondary_types)

    def test_billing_addresses(self):
        lease = make_lease(self.prop)
        self.assertEqual(lease.billing_list(), ['pay@tenant.example'])
        lease.billing_emails = 'a@x.com, b@x.com;a@x.com\nc@x.com'
        self.assertEqual(lease.billing_list(), ['a@x.com', 'b@x.com', 'c@x.com'])


class PostingTests(TestCase):
    def setUp(self):
        self.prop = make_commercial()
        self.bank = bank_for(self.prop)

    def rent_charges(self, lease):
        return list(Charge.objects.filter(unit=lease.unit, is_void=False).order_by('due_date', 'description'))

    def test_posting_a_month_makes_the_rent_and_cam_charges_and_balanced_entries(self):
        lease = make_lease(self.prop, rent_amount='5000', cam='400')
        made = rent.post_lease_month(lease, date(2026, 12, 1))
        self.assertEqual([(c.description, c.amount, c.due_date, c.owner) for c in made], [('Rent Dec 2026', D('5000.00'), date(2026, 12, 1), lease.tenant), ('CAM estimate Dec 2026', D('400.00'), date(2026, 12, 1), lease.tenant)])
        self.assertEqual({c.income_account.code for c in made}, {'4100', '4110'})
        for e in JournalEntry.objects.filter(association=self.prop, source='charge'):
            self.assertEqual(sum(l.debit for l in e.lines.all()), sum(l.credit for l in e.lines.all()))
        self.assertEqual(sum(a['balance'] for a in __import__('accounting.charges', fromlist=['x']).accounts(self.prop)), D('5400.00'))

    def test_posting_twice_posts_once(self):
        lease = make_lease(self.prop, cam='100')
        self.assertEqual(len(rent.post_lease_month(lease, date(2026, 12, 1))), 2)
        self.assertEqual(rent.post_lease_month(lease, date(2026, 12, 1)), [])
        self.assertEqual(Charge.objects.filter(unit=lease.unit).count(), 2)

    def test_a_prorated_month_says_so(self):
        lease = make_lease(self.prop, rent_amount='3100', start=date(2026, 7, 16))
        made = rent.post_lease_month(lease, date(2026, 7, 1))
        self.assertEqual((made[0].amount, made[0].description), (D('1600.00'), 'Rent Jul 2026 (prorated)'))

    def test_nothing_posts_before_the_books_begin(self):
        lease = make_lease(self.prop)
        profile = ledger.profile_for(self.prop)
        profile.books_start = date(2026, 9, 30)
        profile.save()
        self.assertEqual(rent.post_lease_month(lease, date(2026, 9, 1)), [])
        self.assertEqual(len(rent.post_lease_month(lease, date(2026, 10, 1))), 1)

    def test_a_closed_month_refuses(self):
        lease = make_lease(self.prop)
        MonthLock.objects.create(association=self.prop, month=date(2026, 12, 1))
        with self.assertRaises(ledger.LedgerError):
            rent.post_lease_month(lease, date(2026, 12, 1))

    def test_a_lease_that_ended_or_has_not_started_posts_nothing(self):
        ended = make_lease(self.prop, label='A', tenant='Gone', end=date(2026, 8, 31))
        future = make_lease(self.prop, label='B', tenant='Later', start=date(2027, 3, 1))
        self.assertEqual(rent.post_lease_month(ended, date(2026, 12, 1)), [])
        self.assertEqual(rent.post_lease_month(future, date(2026, 12, 1)), [])

    def test_rent_posts_ten_days_ahead_and_not_before(self):
        lease = make_lease(self.prop, start=date(2026, 12, 1))
        self.assertEqual(rent.post_rent_due(self.prop, date(2026, 11, 20))['posted'], 0)           # December 1 less 10 days is November 21
        self.assertFalse(Charge.objects.filter(unit=lease.unit, due_date=date(2026, 12, 1)).exists())
        got = rent.post_rent_due(self.prop, date(2026, 11, 21))
        self.assertEqual(got['posted'], 1)
        self.assertTrue(Charge.objects.filter(unit=lease.unit, due_date=date(2026, 12, 1)).exists())

    def test_running_it_again_posts_nothing_more(self):
        make_lease(self.prop, start=date(2026, 12, 1))
        rent.post_rent_due(self.prop, date(2026, 11, 25))
        again = rent.post_rent_due(self.prop, date(2026, 11, 26))
        self.assertEqual((again['posted'], again['invoices']), (0, 0))

    def test_the_lead_time_is_a_setting(self):
        make_lease(self.prop, start=date(2026, 12, 1))
        cfg = rent.settings_for(self.prop)
        cfg.rent_post_days_ahead = 20
        cfg.save()
        self.assertEqual(rent.post_rent_due(self.prop, date(2026, 11, 11))['posted'], 1)            # December 1 less 20 days

    def test_posting_makes_the_tenants_invoice(self):
        lease = make_lease(self.prop, rent_amount='5000', cam='250', start=date(2026, 12, 1))
        got = rent.post_rent_due(self.prop, date(2026, 11, 25))
        self.assertEqual(got['invoices'], 1)
        inv = TenantInvoice.objects.get(lease=lease)
        self.assertEqual((inv.month, inv.status, inv.total, [l['description'] for l in inv.lines]), (date(2026, 12, 1), 'ready', D('5250.00'), ['Rent Dec 2026', 'CAM estimate Dec 2026']))

    def test_an_unpaid_earlier_month_shows_as_the_previous_balance(self):
        lease = make_lease(self.prop, rent_amount='1000')
        rent.post_lease_month(lease, date(2026, 11, 1))
        rent.post_lease_month(lease, date(2026, 12, 1))
        inv, made = invoices.ensure_invoice(lease, date(2026, 12, 1))
        self.assertEqual((inv.previous_balance, inv.total), (D('1000.00'), D('2000.00')))

    def test_a_payment_settles_the_oldest_rent(self):
        from accounting import charges
        lease = make_lease(self.prop, rent_amount='1000')
        rent.post_lease_month(lease, date(2026, 11, 1))
        rent.post_lease_month(lease, date(2026, 12, 1))
        charges.receive_payment(self.prop, lease.unit, lease.tenant, date(2026, 11, 3), D('1000'), Receipt.Method.CHECK, bank_account=self.bank)
        self.assertEqual(Charge.objects.get(unit=lease.unit, due_date=date(2026, 11, 1)).balance, D('0.00'))
        self.assertEqual(Charge.objects.get(unit=lease.unit, due_date=date(2026, 12, 1)).balance, D('1000.00'))

    def test_auto_send_sends_by_itself_only_for_a_month_still_to_come(self):
        cfg = rent.settings_for(self.prop)
        cfg.auto_send_invoices = True
        cfg.save()
        make_lease(self.prop, start=date(2026, 12, 1))
        with override_settings(EMAIL_BACKEND=LOCMEM):
            got = rent.post_rent_due(self.prop, date(2026, 11, 25))
            self.assertEqual((got['invoices'], got['sent']), (1, 1))
            self.assertEqual(len(mail.outbox), 1)

    def test_a_late_fee_can_be_set_on_the_lease(self):
        from accounting import charges
        lease = make_lease(self.prop, rent_amount='1000', late_fee_flat=D('75'))
        profile = ledger.profile_for(self.prop)
        profile.late_fee_kind, profile.late_fee_flat, profile.late_fee_confirmed, profile.late_fee_grace_days = 'flat', D('25'), True, 5
        profile.save()
        charge = rent.post_lease_month(lease, date(2026, 11, 1))[0]
        self.assertEqual(charges.late_fee_amount(profile, charge), D('75.00'))
        other = make_lease(self.prop, label='A', tenant='Other', rent_amount='1000')
        c2 = rent.post_lease_month(other, date(2026, 11, 1))[0]
        self.assertEqual(charges.late_fee_amount(profile, c2), D('25.00'))


class CloseSheetTests(TestCase):
    def setUp(self):
        self.prop = make_commercial()
        bank_for(self.prop)

    def items(self, month=date(2026, 10, 1)):
        return {i['key']: i for i in closing.close_sheet(self.prop, month, today=date(2026, 11, 10))['items']}

    def test_a_commercial_close_has_no_collections_and_no_reserve_step(self):
        items = self.items()
        self.assertNotIn('collections', items)
        self.assertNotIn('reserve_tie', items)
        self.assertIn('send_board', items)
        self.assertEqual(items['send_board']['title'], 'Send financials to the owners')
        self.assertEqual(items['charges']['title'], 'Rent and tenant charges posted')

    def test_an_association_keeps_its_collections_step(self):
        hoa = Property.objects.create(name='Some HOA', property_type=Property.Type.ASSOCIATION)
        ledger.ensure_chart(hoa)
        keys = [i['key'] for i in closing.close_sheet(hoa, date(2026, 10, 1), today=date(2026, 11, 10))['items']]
        self.assertIn('collections', keys)
        self.assertEqual(closing.close_sheet(hoa, date(2026, 10, 1))['items'][0]['title'], 'Owner charges posted')

    def test_the_late_fee_step_only_shows_when_the_property_has_terms(self):
        self.assertNotIn('late_fees', self.items())
        profile = ledger.profile_for(self.prop)
        profile.late_fee_kind, profile.late_fee_flat, profile.late_fee_confirmed = 'flat', D('50'), True
        profile.save()
        self.assertIn('late_fees', self.items())

    def test_the_rent_step_counts_leases_with_and_without_their_rent_posted(self):
        make_lease(self.prop, label='A', tenant='One')
        two = make_lease(self.prop, label='B', tenant='Two')
        self.assertEqual(rent.month_status(self.prop, date(2026, 10, 1)), {'leases': 2, 'posted': 0, 'missing': 2})
        item = self.items()['charges']
        self.assertEqual(item['status'], 'attention')
        self.assertIn('Rent posted for 0 of 2 leases', item['detail'])
        rent.post_lease_month(two, date(2026, 10, 1))
        self.assertEqual(rent.month_status(self.prop, date(2026, 10, 1))['posted'], 1)
        for lease in Lease.objects.filter(property=self.prop):
            rent.post_lease_month(lease, date(2026, 10, 1))
        self.assertEqual(self.items()['charges']['status'], 'attention')                      # all posted: it waits for your word that you checked
        from accounting.models import CloseStepDone
        CloseStepDone.objects.create(association=self.prop, month=date(2026, 10, 1), key='charges')
        self.assertEqual(self.items()['charges']['status'], 'done')

    def test_the_send_step_says_owners(self):
        ledger.lock_month(self.prop, date(2026, 10, 1))
        self.assertEqual(board_send.status(self.prop, date(2026, 10, 1)), ('attention', 'Review the package and send it to the owners.'))
        self.assertEqual(closing.association_status(self.prop, date(2026, 10, 1)), ('attention', 'Closed, financials not sent'))

    def test_the_company_checklist_counts_both_kinds(self):
        hoa = Property.objects.create(name='Some HOA', property_type=Property.Type.ASSOCIATION)
        profile = ledger.profile_for(hoa)
        profile.we_do_financials = True
        profile.save()
        self.assertEqual(len(closing.associations()), 2)
        self.assertEqual([p.property.name for p in closing.associations(commercial=False)], ['Some HOA'])


class SendToOwnersTests(TestCase):
    def setUp(self):
        self.prop = make_commercial()
        bank_for(self.prop)
        ledger.lock_month(self.prop, date(2026, 10, 1))
        person = Contact.objects.create(name='Landlord One', contact_type=Contact.ContactType.OWNER, email='land@example.com')
        BoardTerm.objects.create(association=self.prop, person=person, position='Owner')

    @override_settings(EMAIL_BACKEND=LOCMEM, STORAGES=STORAGES)
    def test_the_package_goes_to_the_owners_with_ana_and_justin_copied(self):
        from unittest import mock
        rows = board_send.board_rows(self.prop)
        self.assertEqual([(r['name'], r['position']) for r in rows], [('Landlord One', 'Owner')])
        with mock.patch('accounting.board_send.package.build', return_value=b'%PDF-fake'):
            board_send.send(self.prop, date(2026, 10, 1), rows, board_send.default_subject(self.prop, date(2026, 10, 1)), board_send.default_body(self.prop, date(2026, 10, 1)))
        self.assertEqual(mail.outbox[0].to, ['land@example.com'])
        self.assertIn('tenant aging', mail.outbox[0].body)
        self.assertEqual(closing.association_status(self.prop, date(2026, 10, 1)), ('done', 'Closed and sent to the owners'))

    @override_settings(EMAIL_BACKEND=LOCMEM)
    def test_no_owner_with_an_email_means_nothing_is_sent(self):
        with self.assertRaises(board_send.BoardSendError) as cm:
            board_send.send(self.prop, date(2026, 10, 1), [], 'Subject', 'Body')
        self.assertIn('owner', str(cm.exception))


class LabelTests(TestCase):
    def test_commercial_words_and_association_words(self):
        prop = make_commercial()
        hoa = Property.objects.create(name='Some HOA', property_type=Property.Type.ASSOCIATION)
        self.assertEqual((labels.label(prop, 'holders'), labels.label(prop, 'units_title'), labels.label(prop, 'charge')), ('Tenants', 'Units & tenants', 'Rent'))
        self.assertEqual((labels.label(hoa, 'holders'), labels.label(hoa, 'units_title'), labels.label(hoa, 'charge')), ('Owners', 'Units & owners', 'Assessment'))

    def test_the_template_filters(self):
        from accounting.templatetags.acc_extras import is_commercial, lab
        prop = make_commercial()
        self.assertEqual(lab(prop, 'aging'), 'Tenant aging')
        self.assertTrue(is_commercial(prop))
        self.assertFalse(is_commercial(Property(name='x', property_type=Property.Type.ASSOCIATION)))


class PayablesEligibilityTests(TestCase):
    def test_a_commercial_property_with_books_and_a_bank_account_can_have_payables(self):
        prop = make_commercial()
        self.assertEqual(payables_hub.eligibility(prop)[0], 'bank')
        bank_for(prop)
        self.assertEqual(payables_hub.eligibility(prop)[0], 'ok')
        self.assertIn(prop, [p for p, _ in payables_hub.candidate_properties()])

    def test_two_bank_accounts_are_fine(self):
        prop = make_commercial()
        bank_for(prop)
        BankAccount.objects.create(association=prop, name='Second', bank_name='Truist', last4='7788', account=ledger.account(prop, '1111'))
        self.assertEqual(payables_hub.eligibility(prop)[0], 'ok')

    def test_a_commercial_property_without_books_says_so(self):
        prop = make_commercial()
        AssociationProfile.objects.filter(property=prop).update(we_do_financials=False)
        self.assertEqual(payables_hub.eligibility(prop)[0], 'books')


class InvoiceTests(TestCase):
    def setUp(self):
        self.prop = make_commercial()
        bank_for(self.prop)
        self.lease = make_lease(self.prop, rent_amount='5000', cam='250', start=date(2026, 12, 1))
        rent.post_rent_due(self.prop, date(2026, 11, 25))
        self.inv = TenantInvoice.objects.get(lease=self.lease)

    def test_the_pdf_is_a_pdf_with_the_lines(self):
        data = invoices.pdf_bytes(self.inv)
        self.assertTrue(data.startswith(b'%PDF'))
        self.assertGreater(len(data), 1000)

    @override_settings(EMAIL_BACKEND=LOCMEM)
    def test_sending_emails_the_billing_address_from_admin_with_the_pdf_and_records_it(self):
        invoices.send_invoice(self.inv)
        self.inv.refresh_from_db()
        msg = mail.outbox[0]
        self.assertEqual((msg.to, msg.from_email, self.inv.status, self.inv.sent_to, self.inv.send_count), (['pay@tenant.example'], 'admin@proper-realty.com', 'sent', ['pay@tenant.example'], 1))
        self.assertEqual(msg.attachments[0][2], 'application/pdf')
        self.assertIn('Hello', msg.body)

    @override_settings(EMAIL_BACKEND=LOCMEM)
    def test_a_second_send_is_refused_unless_asked_for(self):
        invoices.send_invoice(self.inv)
        with self.assertRaises(invoices.InvoiceError):
            invoices.send_invoice(self.inv)
        self.assertEqual(len(mail.outbox), 1)
        invoices.send_invoice(self.inv, again=True)
        self.assertEqual((len(mail.outbox), TenantInvoice.objects.get(pk=self.inv.pk).send_count), (2, 2))

    @override_settings(EMAIL_BACKEND=LOCMEM)
    def test_no_address_means_nothing_is_sent_or_recorded(self):
        self.lease.tenant.email = ''
        self.lease.tenant.save()
        with self.assertRaises(invoices.InvoiceError):
            invoices.send_invoice(self.inv)
        self.assertEqual((len(mail.outbox), TenantInvoice.objects.get(pk=self.inv.pk).status), (0, 'ready'))

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.console.EmailBackend')
    def test_no_mail_delivery_means_nothing_is_recorded(self):
        with self.assertRaises(invoices.InvoiceError):
            invoices.send_invoice(self.inv)
        self.assertEqual(TenantInvoice.objects.get(pk=self.inv.pk).status, 'ready')

    def test_a_sent_invoice_cannot_be_refreshed_and_a_ready_one_can(self):
        invoices.refresh_invoice(self.inv)
        self.inv.status = 'sent'
        with self.assertRaises(invoices.InvoiceError):
            invoices.refresh_invoice(self.inv)

    def test_the_invoice_is_made_once_per_lease_and_month(self):
        inv, made = invoices.ensure_invoice(self.lease, date(2026, 12, 1))
        self.assertEqual((inv.pk, made), (self.inv.pk, False))
        self.assertEqual(TenantInvoice.objects.filter(lease=self.lease).count(), 1)

    def test_nothing_posted_means_no_invoice(self):
        with self.assertRaises(invoices.InvoiceError):
            invoices.ensure_invoice(self.lease, date(2027, 5, 1))


@override_settings(STORAGES=STORAGES)
class ScreenTests(TestCase):
    def setUp(self):
        self.prop = make_commercial()
        bank_for(self.prop)
        self.user = get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678')
        self.client.force_login(self.user)

    def test_the_leases_screen_lists_tenants_vacant_units_and_endings(self):
        make_lease(self.prop, label='C', tenant='Sky Dining LLC', end=None)
        Unit.objects.create(property=self.prop, label='B')
        r = self.client.get(reverse('commercial:leases', args=[self.prop.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Sky Dining LLC')
        self.assertContains(r, 'No tenant')

    def test_the_screens_refuse_an_association(self):
        hoa = Property.objects.create(name='Some HOA', property_type=Property.Type.ASSOCIATION)
        self.assertEqual(self.client.get(reverse('commercial:leases', args=[hoa.pk])).status_code, 404)

    def test_a_lease_is_made_from_the_form_with_a_new_unit_and_a_new_tenant(self):
        r = self.client.post(reverse('commercial:lease_new', args=[self.prop.pk]), {
            'unit': '', 'new_unit_label': 'First floor', 'new_unit_sqft': '2400', 'status': 'active', 'tenant_name': 'Floor One Cafe', 'tenant_email': 'cafe@example.com', 'tenant_phone': '',
            'start_date': '2026-11-01', 'end_date': '', 'base_rent': '4200.00', 'prorate': 'on', 'cam_estimate': '300.00', 'security_deposit': '8400', 'deposit_received_on': '', 'deposit_note': 'unknown',
            'billing_emails': 'ap@cafe.example', 'renewal_notes': '', 'notes': '', 'late_fee_flat': '', 'late_fee_percent': ''})
        lease = Lease.objects.get(property=self.prop)
        self.assertRedirects(r, reverse('commercial:lease', args=[self.prop.pk, lease.pk]))
        self.assertEqual((lease.unit.label, lease.unit.square_footage, lease.tenant.name, lease.tenant.contact_type, lease.base_rent, lease.billing_list()), ('First floor', 2400, 'Floor One Cafe', 'tenant', D('4200.00'), ['ap@cafe.example']))
        self.assertTrue(Ownership.objects.filter(unit=lease.unit, person=lease.tenant, is_billed=True).exists())

    def test_the_form_needs_a_unit_and_a_sensible_end_date(self):
        r = self.client.post(reverse('commercial:lease_new', args=[self.prop.pk]), {'unit': '', 'new_unit_label': '', 'status': 'active', 'tenant_name': 'X', 'start_date': '2026-11-01', 'end_date': '2026-01-01', 'base_rent': '100'})
        self.assertEqual((r.status_code, Lease.objects.count()), (200, 0))
        self.assertContains(r, 'Choose the unit')

    def test_an_existing_tenant_is_reused_by_name(self):
        lease = make_lease(self.prop, label='C', tenant='Sky Dining LLC')
        other = Unit.objects.create(property=self.prop, label='D')
        self.client.post(reverse('commercial:lease_new', args=[self.prop.pk]), {'unit': other.pk, 'status': 'active', 'tenant_name': 'sky dining llc', 'start_date': '2026-11-01', 'base_rent': '100', 'cam_estimate': '0', 'security_deposit': '0'})
        self.assertEqual(Contact.objects.filter(name__iexact='sky dining llc').count(), 1)
        self.assertEqual(Lease.objects.filter(tenant=lease.tenant).count(), 2)

    def test_the_tenant_page_adds_steps_seasons_and_ends_the_lease(self):
        lease = make_lease(self.prop)
        url = reverse('commercial:lease', args=[self.prop.pk, lease.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.post(url, {'action': 'add_step', 'effective_date': '2027-01-01', 'monthly_rent': '5200', 'note': 'yearly'})
        self.client.post(url, {'action': 'add_season', 'name': 'Winter', 'start_month': '11', 'end_month': '4', 'monthly_rent': '6500'})
        self.assertEqual((lease.steps.count(), lease.seasons.count()), (1, 1))
        self.assertContains(self.client.get(url), 'Winter')
        self.client.post(url, {'action': 'generate_steps', 'first_effective': '2028-01-01', 'years': '2', 'percent': '3'})
        self.assertEqual(lease.steps.count(), 3)
        self.client.post(url, {'action': 'end_lease', 'end_date': '2026-12-31'})
        lease.refresh_from_db()
        self.assertEqual((lease.status, lease.end_date), ('ended', date(2026, 12, 31)))
        self.assertEqual(Ownership.objects.get(unit=lease.unit).end_date, date(2026, 12, 31))

    def test_a_bad_amount_is_a_message_not_an_error(self):
        lease = make_lease(self.prop)
        r = self.client.post(reverse('commercial:lease', args=[self.prop.pk, lease.pk]), {'action': 'add_step', 'effective_date': '2027-01-01', 'monthly_rent': 'lots'}, follow=True)
        self.assertEqual(lease.steps.count(), 0)
        self.assertContains(r, 'is not an amount')

    def test_the_lease_copy_is_kept_and_removed(self):
        lease = make_lease(self.prop)
        url = reverse('commercial:lease', args=[self.prop.pk, lease.pk])
        with tempfile.TemporaryDirectory() as tmp, override_settings(MEDIA_ROOT=tmp):
            self.client.post(url, {'action': 'upload_doc', 'kind': 'lease', 'name': 'Signed lease', 'file': SimpleUploadedFile('lease.pdf', b'%PDF-1.4 test', content_type='application/pdf')})
            doc = LeaseDocument.objects.get(lease=lease)
            self.assertEqual((doc.name, doc.kind), ('Signed lease', 'lease'))
            self.assertContains(self.client.get(url), 'Signed lease')
            self.client.post(url, {'action': 'delete_doc', 'doc': doc.pk})
            self.assertEqual(LeaseDocument.objects.count(), 0)

    def test_the_invoice_screen_posts_rent_lists_invoices_and_sends(self):
        lease = make_lease(self.prop, rent_amount='5000')
        url = reverse('commercial:invoices', args=[self.prop.pk]) + '?month=2026-12'
        r = self.client.get(url)
        self.assertContains(r, 'Rent not posted yet')
        self.client.post(url, {'action': 'post_rent', 'month': '2026-12'})
        inv = TenantInvoice.objects.get(lease=lease)
        self.assertEqual(inv.total, D('5000.00'))
        self.assertContains(self.client.get(url), 'Send all 1 ready')
        pdf = self.client.get(reverse('commercial:invoice_pdf', args=[self.prop.pk, inv.pk]))
        self.assertEqual((pdf.status_code, pdf['Content-Type']), (200, 'application/pdf'))
        with override_settings(EMAIL_BACKEND=LOCMEM):
            self.client.post(url, {'action': 'send_all', 'month': '2026-12'})
            self.assertEqual(len(mail.outbox), 1)
            self.client.post(url, {'action': 'send_all', 'month': '2026-12'})
            self.assertEqual(len(mail.outbox), 1)
        inv.refresh_from_db()
        self.assertEqual(inv.status, 'sent')

    def test_the_invoice_settings_save(self):
        url = reverse('commercial:invoices', args=[self.prop.pk])
        self.client.post(url, {'action': 'settings', 'days': '14', 'instructions': 'Pay by ACH to account 1234', 'auto_send': 'on', 'month': '2026-12'})
        cfg = CommercialSettings.objects.get(property=self.prop)
        self.assertEqual((cfg.rent_post_days_ahead, cfg.auto_send_invoices, cfg.payment_instructions), (14, True, 'Pay by ACH to account 1234'))

    def test_the_accounting_screens_open_for_a_commercial_property_with_tenant_words(self):
        lease = make_lease(self.prop)
        hub = self.client.get(reverse('accounting:hub', args=[self.prop.pk]))
        self.assertContains(hub, 'Units &amp; tenants')
        self.assertContains(hub, 'Tenant invoices')
        self.assertNotContains(hub, 'Owner collections')
        self.assertNotContains(hub, 'Delinquencies')
        units = self.client.get(reverse('accounting:units', args=[self.prop.pk]))
        self.assertContains(units, 'Units &amp; tenants')
        self.assertContains(units, 'Sky Dining LLC')
        unit = self.client.get(reverse('accounting:unit', args=[self.prop.pk, lease.unit.pk]))
        self.assertContains(unit, 'Lease: Sky Dining LLC')
        self.assertContains(self.client.get(reverse('accounting:charges', args=[self.prop.pk])), 'Charges to tenants')
        self.assertContains(self.client.get(reverse('accounting:report_aging', args=[self.prop.pk])), 'Tenant aging')
        self.assertEqual(self.client.get(reverse('accounting:close', args=[self.prop.pk, '2026-10'])).status_code, 200)

    def test_collections_and_delinquencies_are_not_there_for_commercial(self):
        self.assertEqual(self.client.get(reverse('accounting:collections', args=[self.prop.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse('accounting:delinquencies', args=[self.prop.pk])).status_code, 404)
        hoa = Property.objects.create(name='Some HOA', property_type=Property.Type.ASSOCIATION)
        profile = ledger.profile_for(hoa)
        profile.we_do_financials = True
        profile.save()
        self.assertEqual(self.client.get(reverse('accounting:collections_home')).status_code, 200)
        self.assertNotContains(self.client.get(reverse('accounting:collections_home')), self.prop.name)

    def test_the_close_lists_keep_associations_and_commercial_apart(self):
        hoa = Property.objects.create(name='Some HOA', property_type=Property.Type.ASSOCIATION)
        profile = ledger.profile_for(hoa)
        profile.we_do_financials = True
        profile.save()
        r = self.client.get(reverse('accounting:home') + '?month=2026-10')
        self.assertContains(r, 'Close associations')
        self.assertContains(r, 'Some HOA')
        self.assertNotContains(r, self.prop.name)
        r = self.client.get(reverse('accounting:home_commercial') + '?month=2026-10')
        self.assertContains(r, 'Close commercial properties')
        self.assertContains(r, self.prop.name)
        self.assertNotContains(r, 'Some HOA')

    def test_the_send_to_owners_screen_uses_owner_words(self):
        ledger.lock_month(self.prop, date(2026, 10, 1))
        r = self.client.get(reverse('accounting:send_board', args=[self.prop.pk, '2026-10']))
        self.assertContains(r, 'Send the financials to the owners')
        self.assertNotContains(r, 'board member')
        self.assertNotContains(r, 'to the board')

    def test_the_property_page_shows_the_tenants_card_and_an_association_page_does_not(self):
        make_lease(self.prop)
        r = self.client.get(reverse('property_detail', args=[self.prop.pk]))
        self.assertContains(r, 'Units &amp; tenants')
        self.assertContains(r, reverse('commercial:leases', args=[self.prop.pk]))
        hoa = Property.objects.create(name='Some HOA', property_type=Property.Type.ASSOCIATION)
        self.assertNotContains(self.client.get(reverse('property_detail', args=[hoa.pk])), reverse('commercial:leases', args=[hoa.pk]))

    def test_the_owners_screen_adds_and_removes_who_gets_the_financials(self):
        url = reverse('commercial:owners', args=[self.prop.pk])
        self.assertContains(self.client.get(url), 'No owner is listed yet')
        self.client.post(url, {'action': 'add', 'name': 'Landlord One', 'email': 'land@example.com', 'position': 'Owner'})
        term = BoardTerm.objects.get(association=self.prop)
        self.assertEqual((term.person.name, term.person.email, term.position, term.end_date), ('Landlord One', 'land@example.com', 'Owner', None))
        self.assertEqual([r['name'] for r in board_send.board_rows(self.prop)], ['Landlord One'])
        self.client.post(url, {'action': 'add', 'name': 'Landlord One', 'email': 'land@example.com'})
        self.assertEqual(BoardTerm.objects.filter(association=self.prop).count(), 1)
        r = self.client.post(url, {'action': 'add', 'name': 'Bad', 'email': 'not-an-email'}, follow=True)
        self.assertContains(r, 'is not an email address')
        self.client.post(url, {'action': 'remove', 'term': term.pk})
        self.assertEqual(board_send.board_rows(self.prop), [])

    def test_the_books_can_be_switched_on_without_a_management_contract_for_commercial(self):
        AssociationProfile.objects.filter(property=self.prop).update(we_do_financials=False)
        self.client.post(reverse('accounting:settings', args=[self.prop.pk]), {'action': 'switch', 'on': '1'})
        self.assertTrue(ledger.profile_for(self.prop).we_do_financials)

    def test_a_commercial_property_can_be_picked_for_a_payable(self):
        from accounting import payables_hub as hub
        self.assertIn(self.prop.pk, [p.pk for p, _ in hub.candidate_properties()])


class SchedulerCommandTests(TestCase):
    def test_the_recurring_command_posts_rent_for_a_commercial_property(self):
        prop = make_commercial()
        bank_for(prop)
        lease = make_lease(prop, start=date(2026, 12, 1))
        from django.utils import timezone
        from unittest import mock
        with mock.patch.object(timezone, 'localdate', return_value=date(2026, 11, 25)):
            call_command('post_accounting_recurring', stdout=io.StringIO())
        self.assertTrue(Charge.objects.filter(unit=lease.unit, due_date=date(2026, 12, 1)).exists())
        self.assertEqual(TenantInvoice.objects.filter(lease=lease).count(), 1)

    def test_it_leaves_alone_a_property_whose_automatic_posting_is_off(self):
        prop = make_commercial()
        lease = make_lease(prop)
        profile = ledger.profile_for(prop)
        profile.auto_post = False
        profile.save()
        from django.utils import timezone
        from unittest import mock
        with mock.patch.object(timezone, 'localdate', return_value=date(2026, 11, 25)):
            call_command('post_accounting_recurring', stdout=io.StringIO())
        self.assertFalse(Charge.objects.filter(unit=lease.unit).exists())


TB_CSV = """Property,Del Park LLC
Account,Account Name,Balance Forward,Debit,Credit,Ending Balance
1110,Operating Cash,0.00,0.00,0.00,"12,500.00"
1300,Accounts Receivable,0.00,0.00,0.00,"3,000.00"
2200,Accounts Payable,0.00,0.00,0.00,"(2,000.00)"
3070,Fund Balance,0.00,0.00,0.00,"(13,500.00)"
4100,Base Rent,0.00,0.00,0.00,0.00
"""


class OpeningBalanceTests(TestCase):
    def setUp(self):
        self.prop = make_commercial()

    def test_a_trial_balance_is_read_with_signs_and_blank_accounts_left_out(self):
        rows = opening.parse_trial_balance(TB_CSV.encode(), 'tb.csv')
        self.assertEqual(rows, [('1110', 'Operating Cash', D('12500.00')), ('1300', 'Accounts Receivable', D('3000.00')), ('2200', 'Accounts Payable', D('-2000.00')), ('3070', 'Fund Balance', D('-13500.00'))])

    def test_debit_and_credit_columns_work_too(self):
        text = 'Account,Description,Debit,Credit\n1110 - Cash,Cash,500.00,\n3070 - Fund,Fund,,500.00\n'
        self.assertEqual([r[2] for r in opening.parse_trial_balance(text.encode(), 'x.csv')], [D('500.00'), D('-500.00')])

    def test_a_file_that_does_not_add_up_is_refused(self):
        text = 'Account,Name,Ending Balance\n1110,Cash,500.00\n3070,Fund,-400.00\n'
        with self.assertRaises(opening.OpeningError) as cm:
            opening.parse_trial_balance(text.encode(), 'x.csv')
        self.assertIn('does not add up', str(cm.exception))

    def test_something_that_is_not_a_trial_balance_is_refused(self):
        with self.assertRaises(opening.OpeningError):
            opening.parse_trial_balance(b'hello,world\n1,2\n', 'x.csv')

    def test_loading_posts_the_balances_sets_the_day_and_makes_unknown_accounts(self):
        rows = opening.parse_trial_balance(TB_CSV.encode() + b'5555,Odd Account,0.00,0.00,0.00,0.00\n', 'tb.csv')
        result = opening.load_opening(self.prop, rows, as_of=date(2026, 9, 30))
        self.assertEqual(result['entries'], 1)
        self.assertEqual(ledger.profile_for(self.prop).books_start, date(2026, 9, 30))
        tb = {r['account'].code: r['ending'] for r in ledger.trial_balance(self.prop, date(2026, 10, 1), date(2026, 10, 31))['rows']}
        self.assertEqual((tb['1110'], tb['1300'], tb['2200']), (D('12500.00'), D('3000.00'), D('-2000.00')))
        self.assertEqual(JournalEntry.objects.get(association=self.prop).source, 'opening')

    def test_accrual_and_cash_files_make_one_entry_each(self):
        rows = opening.parse_trial_balance(TB_CSV.encode(), 'tb.csv')
        self.assertEqual(opening.load_opening(self.prop, rows, rows, as_of=date(2026, 9, 30))['entries'], 2)
        self.assertEqual(sorted(JournalEntry.objects.filter(association=self.prop).values_list('basis', flat=True)), ['accrual', 'cash'])

    def test_books_that_have_entries_are_not_loaded_over(self):
        rows = opening.parse_trial_balance(TB_CSV.encode(), 'tb.csv')
        opening.load_opening(self.prop, rows, as_of=date(2026, 9, 30))
        with self.assertRaises(opening.OpeningError):
            opening.load_opening(self.prop, rows, as_of=date(2026, 9, 30))

    def test_the_command_is_a_dry_run_unless_asked(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = f'{tmp}/tb.csv'
            open(path, 'w').write(TB_CSV)
            out = io.StringIO()
            call_command('load_commercial_opening', property='Del Park LLC', as_of='2026-09-30', accrual=path, stdout=out)
            self.assertIn('Dry run', out.getvalue())
            self.assertFalse(JournalEntry.objects.filter(association=self.prop).exists())
            call_command('load_commercial_opening', property='Del Park LLC', as_of='2026-09-30', accrual=path, apply=True, stdout=io.StringIO())
            self.assertTrue(JournalEntry.objects.filter(association=self.prop).exists())

    def test_the_general_ledger_loader_finds_commercial_properties(self):
        from accounting import gl_detail
        self.assertEqual(gl_detail.association_for('Del Park LLC'), self.prop)


@override_settings(STORAGES=STORAGES)
class EveryScreenTests(TestCase):
    """The accounting screens were written for associations: each of them must open for a commercial building."""

    def test_every_accounting_screen_opens_for_a_commercial_property(self):
        prop = make_commercial()
        bank = bank_for(prop)
        lease = make_lease(prop, cam='100')
        rent.post_lease_month(lease, date(2026, 10, 1))
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        by_property = ['hub', 'units', 'charges', 'charge_new', 'charge_one', 'receipt_new', 'bills', 'checks', 'recurring', 'journal', 'reports', 'report_tb', 'report_bs', 'report_is', 'report_pl', 'report_bva',
                       'report_gl', 'report_aging', 'report_payables', 'report_checks', 'budget', 'chart', 'settings', 'contracts', 'late_fees', 'shares', 'pay']
        urls = [reverse('accounting:' + n, args=[prop.pk]) for n in by_property]
        urls += [reverse('accounting:close', args=[prop.pk, '2026-10']), reverse('accounting:reserve_check', args=[prop.pk, '2026-10']), reverse('accounting:send_board', args=[prop.pk, '2026-10']),
                 reverse('accounting:bank', args=[prop.pk, bank.pk, '2026-10']), reverse('accounting:deposits', args=[prop.pk, bank.pk, '2026-10']),
                 reverse('accounting:report_package', args=[prop.pk]) + '?month=2026-10', reverse('commercial:leases', args=[prop.pk]), reverse('commercial:lease', args=[prop.pk, lease.pk]),
                 reverse('commercial:invoices', args=[prop.pk]), reverse('commercial:owners', args=[prop.pk]), reverse('accounting:payables_home')]
        bad = [(u, self.client.get(u).status_code) for u in urls if self.client.get(u).status_code >= 400]
        self.assertEqual(bad, [])
