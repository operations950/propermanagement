"""The Yardi-info build: a lease's bank account and rate date, long-term leases, the lease read, the month-end lease renewals step, the commercial close step, required documents."""
from datetime import date
from decimal import Decimal as D
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from accounting import closing, ledger
from accounting.models import AssociationDocument, AssociationProfile, BankAccount, DocumentPage
from core import close_ltr_tax, required_docs
from core.models import Contact, Property, Unit

from . import leasereader, renewals
from .models import Lease, LeaseDocument, LeaseProposal, LongTermLease, LongTermRentStep, RentStep
from .tests import STORAGES, make_commercial, make_lease

OCT = date(2026, 10, 1)


def ltr_lease(prop=None, tenant='Kershyra Myrick', start=date(2026, 4, 1), end=date(2027, 3, 31), rent='2600', **kw):
    prop = prop or Property.objects.create(name='2036 Alta Meadows Ln', property_type=Property.Type.LONG_TERM_RENTAL)
    person = Contact.objects.create(name=tenant, contact_type=Contact.ContactType.TENANT)
    return LongTermLease.objects.create(property=prop, tenant=person, start_date=start, end_date=end, monthly_rent=D(rent), **kw)


def boss(client):
    client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))


class LongTermLeaseTests(TestCase):
    def test_the_rent_in_force_follows_the_increases(self):
        lease = ltr_lease()
        LongTermRentStep.objects.create(lease=lease, effective_date=date(2026, 9, 1), monthly_rent=D('2700'))
        LongTermRentStep.objects.create(lease=lease, effective_date=date(2027, 1, 15), monthly_rent=D('2800'))
        self.assertEqual(lease.rent_for_month(date(2026, 8, 1)), D('2600'))
        self.assertEqual(lease.rent_for_month(date(2026, 9, 1)), D('2700'))
        self.assertEqual(lease.rent_for_month(date(2027, 1, 1)), D('2700'))     # a step not on the 1st starts next month
        self.assertEqual(lease.rent_for_month(date(2027, 2, 1)), D('2800'))

    def test_the_month_end_rent_check_expects_the_lease_rent(self):
        lease = ltr_lease()
        lease.property.monthly_rent = D('1000')
        lease.property.save()
        LongTermRentStep.objects.create(lease=lease, effective_date=date(2026, 11, 1), monthly_rent=D('2700'))
        self.assertEqual(close_ltr_tax.lease_rent(lease.property, date(2026, 10, 1)), D('2600'))
        self.assertEqual(close_ltr_tax.lease_rent(lease.property, date(2026, 11, 1)), D('2700'))

    def test_a_rental_without_a_lease_keeps_the_rent_set_on_the_property(self):
        prop = Property.objects.create(name='No Lease House', property_type=Property.Type.LONG_TERM_RENTAL, monthly_rent=D('1800'))
        self.assertEqual(close_ltr_tax.lease_rent(prop, date(2026, 11, 1)), D('1800'))

    def test_the_screens_add_a_lease_and_a_step(self):
        boss(self.client)
        prop = Property.objects.create(name='300 Ross Drive', property_type=Property.Type.LONG_TERM_RENTAL)
        r = self.client.post(reverse('commercial:ltr_leases', args=[prop.pk]), {'tenant_name': 'Padgett', 'start_date': '2026-07-01', 'end_date': '2027-06-30', 'monthly_rent': '3600'})
        lease = LongTermLease.objects.get(property=prop)
        self.assertRedirects(r, reverse('commercial:ltr_lease', args=[prop.pk, lease.pk]))
        self.client.post(reverse('commercial:ltr_lease', args=[prop.pk, lease.pk]), {'action': 'add_step', 'effective_date': '2027-01-01', 'monthly_rent': '3700'})
        self.assertEqual(lease.steps.get().monthly_rent, D('3700.00'))
        html = self.client.get(reverse('commercial:ltr_lease', args=[prop.pk, lease.pk])).content.decode()
        self.assertIn('Padgett', html)
        self.assertIn('Rent increases', html)


class RenewalsTests(TestCase):
    def test_leases_ending_or_changing_rate_within_120_days_are_listed_with_their_dates(self):
        prop = make_commercial()
        make_lease(prop, 'A', 'Ends Soon LLC', end=date(2026, 12, 31))
        make_lease(prop, 'B', 'Far Away LLC', end=date(2030, 1, 1))
        c = make_lease(prop, 'C', 'Rate LLC', end=date(2031, 1, 1))
        c.rate_expires_on = date(2027, 1, 31)
        c.save()
        ltr_lease(end=date(2027, 1, 20))
        rows = renewals.rows(OCT)       # month end 10/31: the window runs to 2/28/2027
        got = {(r['tenant'], r['what']): r for r in rows}
        self.assertEqual(set(got), {('Ends Soon LLC', 'end'), ('Rate LLC', 'rate'), ('Kershyra Myrick', 'end')})
        self.assertEqual(got[('Ends Soon LLC', 'end')]['days'], 61)
        self.assertEqual(got[('Kershyra Myrick', 'end')]['kind'], 'ltr')
        self.assertEqual([r['date'] for r in rows], sorted(r['date'] for r in rows))

    def test_the_step_finishes_when_every_row_is_confirmed_and_asks_again_when_a_date_changes(self):
        prop = make_commercial()
        lease = make_lease(prop, 'A', 'Ends Soon LLC', end=date(2026, 12, 31))
        self.assertEqual(renewals.summary(OCT)[0], 'not_started')
        renewals.set_confirmed('commercial', lease.pk, 'end', True)
        self.assertEqual(renewals.summary(OCT)[0], 'done')
        lease.end_date = date(2027, 1, 31)
        lease.save()
        self.assertEqual(renewals.summary(OCT)[0], 'not_started')       # a new end date has to be looked at again
        renewals.set_confirmed('commercial', lease.pk, 'end', True)
        renewals.set_confirmed('commercial', lease.pk, 'end', False)
        self.assertEqual(renewals.summary(OCT)[0], 'not_started')

    def test_with_nothing_due_the_step_is_done(self):
        make_lease(make_commercial(), 'A', 'Far Away LLC', end=date(2035, 1, 1))
        self.assertEqual(renewals.summary(OCT)[0], 'done')

    def test_an_ended_lease_is_not_listed(self):
        make_lease(make_commercial(), 'A', 'Gone LLC', end=date(2026, 12, 1), status='ended')
        self.assertEqual(renewals.rows(OCT), [])

    def test_the_screen_confirms_one_and_all(self):
        boss(self.client)
        prop = make_commercial()
        a = make_lease(prop, 'A', 'One LLC', end=date(2026, 12, 1))
        b = make_lease(prop, 'B', 'Two LLC', end=date(2026, 12, 15))
        url = reverse('commercial:renewals') + '?month=2026-10'
        self.assertIn('One LLC', self.client.get(url).content.decode())
        self.client.post(url, {'month': '2026-10', 'kind': 'commercial', 'pk': a.pk, 'what': 'end', 'ok': '1'})
        a.refresh_from_db()
        self.assertEqual(a.renewal_ok_for, date(2026, 12, 1))
        self.client.post(url, {'month': '2026-10', 'action': 'confirm_all'})
        b.refresh_from_db()
        self.assertEqual(b.renewal_ok_for, date(2026, 12, 15))
        self.assertEqual(renewals.summary(OCT)[0], 'done')


class ChecklistTests(TestCase):
    def _books_on(self, prop):
        ledger.profile_for(prop)
        AssociationProfile.objects.filter(property=prop).update(we_do_financials=True)

    def test_the_commercial_close_is_its_own_step_apart_from_the_associations(self):
        make_commercial()
        make_commercial('Red Barn 381')
        hoa = Property.objects.create(name='Some HOA', property_type=Property.Type.ASSOCIATION)
        self._books_on(hoa)
        self.assertEqual(closing.checklist_step(OCT, kind='commercial')[1], '0 of 2 closed')
        self.assertEqual(closing.checklist_step(OCT)[1], '0 of 1 closed')

    def test_the_lists_are_separate(self):
        boss(self.client)
        make_commercial('Del Park LLC')
        hoa = Property.objects.create(name='Some HOA', property_type=Property.Type.ASSOCIATION)
        self._books_on(hoa)
        assoc = self.client.get(reverse('accounting:home') + '?month=2026-10').content.decode()
        comm = self.client.get(reverse('accounting:home_commercial') + '?month=2026-10').content.decode()
        self.assertIn('Some HOA', assoc)
        self.assertNotIn('Del Park LLC', assoc)
        self.assertIn('Del Park LLC', comm)
        self.assertNotIn('Some HOA', comm)
        self.assertIn('Close commercial properties', comm)


class LeaseReaderTests(TestCase):
    FOUND = {'term_start': '2026-05-01', 'term_end': '2028-06-30', 'monthly_rent': 1100, 'cam_monthly': None, 'security_deposit': 2200,
             'steps': [{'date': '2027-05-01', 'monthly_rent': 1133, 'basis': '3% over the prior year', 'page': 4, 'quote': 'rent shall increase three percent'},
                       {'date': '2028-05-01', 'monthly_rent': 1166.99, 'basis': '3% over the prior year', 'page': 4, 'quote': 'rent shall increase three percent'},
                       {'date': 'not a date', 'monthly_rent': 5}, {'date': '2027-05-01', 'monthly_rent': 9999}],
             'renewal': 'One 2-year option, 120 days notice.', 'notes': ''}

    def _doc(self, prop):
        doc = AssociationDocument.objects.create(property=prop, kind='lease', name='Rooftop lease', file='x.pdf', text_status='ready', page_count=1)
        DocumentPage.objects.create(document=doc, number=1, text='Rent is $1,100 a month.')
        return doc

    def _read(self, doc, lease):
        block = mock.Mock(type='tool_use', input=self.FOUND)
        client = mock.Mock()
        client.messages.create.return_value = mock.Mock(content=[block])
        with mock.patch('accounting.documents._client', return_value=client):
            return leasereader.read_lease(doc, lease)

    def test_the_reading_is_cleaned_and_saved_as_a_proposal_that_changes_nothing(self):
        prop = make_commercial()
        lease = make_lease(prop, 'ROOFTOP', 'Dandelight LLC', rent_amount='0', start=date(2026, 5, 1))
        proposal = self._read(self._doc(prop), lease)
        self.assertEqual([s['date'] for s in proposal.data['steps']], ['2027-05-01', '2028-05-01'])       # the unreadable date and the repeat are dropped
        self.assertEqual(proposal.data['steps'][1]['monthly_rent'], '1166.99')
        lease.refresh_from_db()
        self.assertEqual(lease.steps.count(), 0)
        self.assertEqual(lease.base_rent, D('0.00'))

    def test_applying_makes_the_steps_and_fills_only_what_is_empty(self):
        prop = make_commercial()
        lease = make_lease(prop, 'ROOFTOP', 'Dandelight LLC', rent_amount='0', start=date(2026, 5, 1))
        proposal = self._read(self._doc(prop), lease)
        made = leasereader.apply_proposal(proposal, step_dates=['2027-05-01', '2028-05-01'])
        lease.refresh_from_db()
        self.assertEqual(made, 2)
        self.assertEqual(lease.base_rent, D('1100.00'))
        self.assertEqual(lease.end_date, date(2028, 6, 30))
        self.assertEqual(lease.security_deposit, D('2200.00'))
        self.assertEqual(list(RentStep.objects.filter(lease=lease).values_list('monthly_rent', flat=True)), [D('1133.00'), D('1166.99')])
        self.assertIn('120 days', lease.renewal_notes)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, LeaseProposal.Status.APPLIED)

    def test_an_unticked_step_is_not_made_and_existing_rent_is_not_overwritten(self):
        prop = make_commercial()
        lease = make_lease(prop, 'ROOFTOP', 'Dandelight LLC', rent_amount='900', start=date(2026, 5, 1))
        proposal = self._read(self._doc(prop), lease)
        leasereader.apply_proposal(proposal, step_dates=['2028-05-01'])
        lease.refresh_from_db()
        self.assertEqual(lease.base_rent, D('900.00'))
        self.assertEqual(lease.steps.count(), 1)

    def test_a_long_term_lease_gets_its_steps_too(self):
        lease = ltr_lease(rent='0', start=None, end=None)
        proposal = self._read(self._doc(lease.property), lease)
        leasereader.apply_proposal(proposal)
        lease.refresh_from_db()
        self.assertEqual((lease.monthly_rent, lease.start_date, lease.steps.count()), (D('1100.00'), date(2026, 5, 1), 2))

    def test_without_the_claude_connection_it_says_so(self):
        from accounting.documents import DocumentError
        prop = make_commercial()
        lease = make_lease(prop)
        with override_settings(ANTHROPIC_API_KEY=''):
            with self.assertRaises(DocumentError):
                leasereader.read_lease(self._doc(prop), lease)


class RequiredDocumentTests(TestCase):
    def test_every_property_needs_a_management_agreement(self):
        prop = Property.objects.create(name='Beach House', property_type=Property.Type.SHORT_TERM_RENTAL)
        self.assertEqual([i['label'] for i in required_docs.missing_for(prop)], ['Management agreement'])
        AssociationDocument.objects.create(property=prop, kind='contract', name='Agreement', file='a.pdf')
        self.assertEqual(required_docs.missing_for(prop), [])

    def test_a_document_in_the_general_list_called_a_management_agreement_counts(self):
        from core.models import PropertyDocument
        prop = Property.objects.create(name='Beach House', property_type=Property.Type.SHORT_TERM_RENTAL)
        PropertyDocument.objects.create(property=prop, name='Signed management agreement 2026', file='a.pdf')
        self.assertEqual(required_docs.missing_for(prop), [])

    def test_a_commercial_property_needs_a_lease_for_each_tenant(self):
        prop = make_commercial()
        AssociationDocument.objects.create(property=prop, kind='contract', name='Mgmt', file='m.pdf')
        lease = make_lease(prop, 'A', 'Sky Dining LLC')
        self.assertEqual([i['label'] for i in required_docs.missing_for(prop)], ['Lease: Sky Dining LLC (A)'])
        LeaseDocument.objects.create(lease=lease, kind='lease', name='lease', file='l.pdf')
        self.assertEqual(required_docs.missing_for(prop), [])

    def test_a_long_term_rental_needs_a_lease(self):
        lease = ltr_lease()
        AssociationDocument.objects.create(property=lease.property, kind='contract', name='Mgmt', file='m.pdf')
        self.assertEqual([i['label'] for i in required_docs.missing_for(lease.property)], ['Lease: Kershyra Myrick'])
        AssociationDocument.objects.create(property=lease.property, kind='lease', name='Myrick lease', file='l.pdf')
        self.assertEqual(required_docs.missing_for(lease.property), [])

    def test_a_placeholder_or_inactive_property_needs_nothing(self):
        gen = Property.objects.create(name='Commercial (general)', property_type=Property.Type.COMMERCIAL, is_general=True)
        off = Property.objects.create(name='Old', property_type=Property.Type.SHORT_TERM_RENTAL, is_active=False)
        self.assertEqual(required_docs.required_for(gen), [])
        self.assertEqual(required_docs.required_for(off), [])

    @override_settings(STORAGES=STORAGES)
    def test_the_property_page_lists_what_is_missing_and_a_file_can_be_added_there(self):
        boss(self.client)
        prop = Property.objects.create(name='Beach House', property_type=Property.Type.SHORT_TERM_RENTAL)
        html = self.client.get(reverse('property_detail', args=[prop.pk])).content.decode()
        self.assertIn('Required documents', html)
        self.assertIn('Management agreement', html)
        self.assertIn('Beach House', self.client.get(reverse('missing_documents')).content.decode())
        self.client.post(reverse('property_detail', args=[prop.pk]), {'action': 'add_required_document', 'kind': 'contract', 'file': SimpleUploadedFile('agreement.pdf', b'%PDF-1.4 x', content_type='application/pdf')})
        self.assertTrue(AssociationDocument.objects.filter(property=prop, kind='contract').exists())
        self.assertNotIn('Beach House', self.client.get(reverse('missing_documents')).content.decode())


class LeaseBankTests(TestCase):
    def test_two_leases_of_one_tenant_pay_into_two_accounts(self):
        from accounting.views import _lease_bank
        prop = make_commercial()
        jmpc = BankAccount.objects.create(association=prop, name='324 JMPC Acct', last4='4613', account=ledger.account(prop, '1110'))
        other = BankAccount.objects.create(association=prop, name='324 Other Acct', kind='reserve', last4='', account=ledger.account(prop, '1120'))
        tenant = Contact.objects.create(name='Dandelight LLC', contact_type=Contact.ContactType.TENANT)
        u3, roof = Unit.objects.create(property=prop, label='3'), Unit.objects.create(property=prop, label='ROOFTOP')
        l3 = Lease.objects.create(property=prop, unit=u3, tenant=tenant, start_date=date(2023, 9, 1), base_rent=D('9141.21'), bank_account=jmpc)
        lr = Lease.objects.create(property=prop, unit=roof, tenant=tenant, start_date=date(2026, 5, 1), base_rent=D('1100'), bank_account=other)
        self.assertEqual(_lease_bank(prop, u3), jmpc)
        self.assertEqual(_lease_bank(prop, roof), other)
        self.assertEqual(l3.tenant_id, lr.tenant_id)


class LoadYardiTenantsTests(TestCase):
    def _setup(self):
        from io import StringIO
        lofts = make_commercial('324 Lofts')
        BankAccount.objects.create(association=lofts, name='324 JMPC Acct', last4='4613', account=ledger.account(lofts, '1110'))
        BankAccount.objects.create(association=lofts, name='324 Other Acct', kind='reserve', account=ledger.account(lofts, '1120'))
        make_commercial('Del Park LLC')
        make_commercial('Red Barn 381')
        for name in ('2036 Alta Meadows Ln', '225 NE 1st St', '300 Ross'):
            Property.objects.create(name=name, property_type=Property.Type.LONG_TERM_RENTAL, monthly_rent=D('3150'))
        return StringIO()

    def test_a_dry_run_changes_nothing_and_apply_loads_everything_once(self):
        from django.core.management import call_command
        out = self._setup()
        call_command('load_yardi_tenants', stdout=out)
        self.assertEqual((Lease.objects.count(), LongTermLease.objects.count()), (0, 0))
        call_command('load_yardi_tenants', '--apply', stdout=out)
        call_command('load_yardi_tenants', '--apply', stdout=out)           # safe to repeat
        self.assertEqual((Lease.objects.count(), LongTermLease.objects.count()), (7, 3))
        roof = Lease.objects.get(unit__label='ROOFTOP')
        three = Lease.objects.get(unit__label='3')
        self.assertEqual(roof.tenant_id, three.tenant_id)                      # one tenant record, two leases
        self.assertEqual((three.bank_account.name, roof.bank_account.name), ('324 JMPC Acct', '324 Other Acct'))
        self.assertEqual(roof.seasons.get().monthly_rent, D('850.00'))
        self.assertEqual(Lease.objects.get(unit__label='4').cam_estimate, D('3564.40'))
        self.assertEqual(LongTermLease.objects.get(tenant__name='Padgett').end_date, date(2027, 6, 30))
        from . import rent
        self.assertEqual(rent.rent_for_month(roof, date(2026, 11, 1))['rent'], D('1100.00'))
        self.assertEqual(rent.rent_for_month(roof, date(2027, 6, 1))['rent'], D('850.00'))
