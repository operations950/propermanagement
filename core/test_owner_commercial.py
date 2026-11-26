"""The owner portal for a commercial building: the released month's financial package, and the tickets staff chose to show."""
import tempfile
from datetime import date
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from accounting import ledger
from accounting.models import MonthLock
from core import owner_commercial, owner_portal
from core.models import Contact, MonthRelease, OwnerAccount, Property
from tickets.models import Ticket, TicketStatusNote

User = get_user_model()
TMP = tempfile.mkdtemp()
STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}


@override_settings(STORAGES=STORAGES, MEDIA_ROOT=TMP, SITE_BASE_URL='http://testserver')
class CommercialOwnerPortalTests(TestCase):
    def setUp(self):
        self.prop = Property.objects.create(name='324 Lofts', property_type=Property.Type.COMMERCIAL, owner_portal_open=True)
        prof = ledger.profile_for(self.prop)
        prof.we_do_financials = True
        prof.save()
        ledger.ensure_chart(self.prop)
        ledger.post_entry(self.prop, date(2026, 10, 5), [{'account': ledger.account(self.prop, '1110'), 'debit': D('1000'), 'credit': D('0')},
                                                         {'account': ledger.account(self.prop, '4500'), 'debit': D('0'), 'credit': D('1000')}], memo='October rent')
        self.owner = Contact.objects.create(name='Josh Owner', contact_type=Contact.ContactType.OWNER, email='josh@example.com')
        self.owner.properties.add(self.prop)
        user = User.objects.create_user(username=f'{owner_portal.USERNAME_PREFIX}abc123', password='x' * 20)
        self.account = OwnerAccount.objects.create(user=user, email='josh@example.com')
        self.staff = User.objects.create_user(username='staffer', password='x' * 20, is_staff=True)

    def sign_in(self):
        self.client.force_login(self.account.user)

    def close_october(self):
        MonthLock.objects.create(association=self.prop, month=date(2026, 10, 1), locked_by=self.staff)

    def test_an_open_commercial_building_is_visible_to_its_owner_and_a_closed_portal_is_not(self):
        self.assertEqual([p.pk for p in owner_portal.visible_properties('josh@example.com')], [self.prop.pk])
        Property.objects.filter(pk=self.prop.pk).update(owner_portal_open=False)
        self.assertEqual(owner_portal.visible_properties('josh@example.com'), [])

    def test_a_month_that_is_not_closed_cannot_be_released(self):
        with self.assertRaises(owner_portal.ReleaseBlocked):
            owner_commercial.release(date(2026, 10, 1), self.staff)
        self.assertFalse(MonthRelease.objects.exists())

    def test_releasing_a_closed_month_keeps_its_package_and_the_owner_can_open_it(self):
        self.close_october()
        out = owner_commercial.release(date(2026, 10, 1), self.staff)
        self.assertEqual((out['properties'], out['months']), (1, 1))
        row = MonthRelease.objects.get(property=self.prop, month=date(2026, 10, 1))
        self.assertEqual(row.snapshot['kind'], 'package')
        self.assertEqual(row.snapshot['figures']['income'], '1000.00')
        self.sign_in()
        home = self.client.get(reverse('owner_home'))
        self.assertEqual(home.status_code, 200)
        self.assertContains(home, 'October 2026')
        pdf = self.client.get(reverse('owner_package', args=[self.prop.pk, '2026-10']))
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(b''.join(pdf.streaming_content).startswith(b'%PDF'))

    def test_a_month_that_is_not_released_is_not_found_and_neither_is_another_owners_building(self):
        self.sign_in()
        self.assertEqual(self.client.get(reverse('owner_package', args=[self.prop.pk, '2026-10'])).status_code, 404)
        other = Property.objects.create(name='Someone Else', property_type=Property.Type.COMMERCIAL, owner_portal_open=True)
        self.assertEqual(self.client.get(reverse('owner_tickets', args=[other.pk])).status_code, 404)

    def test_tickets_start_private_and_only_released_ones_and_released_updates_show(self):
        t = Ticket.objects.create(title='Roof leak, unit 3', property=self.prop, assigned_contact=Contact.objects.create(name='Handyman Co'))
        self.assertFalse(t.owner_visible)
        TicketStatusNote.objects.create(ticket=t, body='Internal: the tenant is difficult.')
        TicketStatusNote.objects.create(ticket=t, body='Roofer is coming Tuesday.', show_to_owner=True)
        self.sign_in()
        self.assertNotContains(self.client.get(reverse('owner_tickets', args=[self.prop.pk])), 'Roof leak')
        self.assertEqual(self.client.get(reverse('owner_ticket', args=[self.prop.pk, t.pk])).status_code, 404)
        Ticket.objects.filter(pk=t.pk).update(owner_visible=True)
        self.assertContains(self.client.get(reverse('owner_tickets', args=[self.prop.pk])), 'Roof leak')
        page = self.client.get(reverse('owner_ticket', args=[self.prop.pk, t.pk]))
        self.assertContains(page, 'Roofer is coming Tuesday.')
        self.assertNotContains(page, 'tenant is difficult')

    def test_relocking_a_released_month_with_different_figures_issues_a_new_package(self):
        self.close_october()
        owner_commercial.release(date(2026, 10, 1), self.staff)
        MonthLock.objects.filter(association=self.prop).delete()
        ledger.post_entry(self.prop, date(2026, 10, 20), [{'account': ledger.account(self.prop, '1110'), 'debit': D('500'), 'credit': D('0')},
                                                          {'account': ledger.account(self.prop, '4500'), 'debit': D('0'), 'credit': D('500')}], memo='Late rent')
        self.close_october()
        owner_commercial.after_lock(self.prop, date(2026, 10, 1))
        row = MonthRelease.objects.get(property=self.prop, month=date(2026, 10, 1))
        self.assertEqual(row.snapshot['figures']['income'], '1500.00')
        self.assertEqual(len(row.revisions), 1)
