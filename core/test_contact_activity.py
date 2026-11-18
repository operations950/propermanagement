"""The contact interaction ticker: `touch` (one cheap UPDATE that never raises), where it fires (a ticket or visit given to a contact, a person attached to a ticket, a follow-up sent, a Quo text,
a vendor bill or payment, a document emailed) and the backfill that gave it a head start."""
import importlib
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from unittest import mock

from django.apps import apps
from django.test import TestCase
from django.utils import timezone

from accounting import payables
from accounting.models import Bill, Payment
from accounting.tests import make_assoc
from core import contact_activity
from core.models import Contact, Property, StaffProfile
from intake.models import QuoMessage
from onsite.models import Visit, VisitType
from tickets.models import FollowUpLog, Ticket, TicketContact


def contact(name='Pat Vendor', **kw):
    kw.setdefault('contact_type', Contact.ContactType.VENDOR)
    return Contact.objects.create(name=name, **kw)


def fresh(c):
    c.refresh_from_db()
    return c


class TouchTests(TestCase):
    def test_a_touch_stamps_the_time_and_counts_up(self):
        c = contact()
        self.assertIsNone(c.last_interaction_at)
        self.assertEqual(c.interaction_count, 0)
        self.assertTrue(contact_activity.touch(c))
        c = fresh(c)
        first = c.last_interaction_at
        self.assertIsNotNone(first)
        self.assertEqual(c.interaction_count, 1)
        contact_activity.touch(c.pk)                                           # an id works as well as the contact
        c = fresh(c)
        self.assertEqual(c.interaction_count, 2)
        self.assertGreaterEqual(c.last_interaction_at, first)

    def test_a_given_moment_is_used(self):
        c = contact()
        moment = timezone.now() - timedelta(days=3)
        contact_activity.touch(c, when=moment)
        self.assertEqual(fresh(c).last_interaction_at, moment)

    def test_it_is_one_query(self):
        c = contact()
        with self.assertNumQueries(1):
            contact_activity.touch(c)

    def test_it_never_raises(self):
        c = contact()
        with mock.patch('core.models.Contact.objects') as objects:
            objects.filter.side_effect = RuntimeError('database on fire')
            self.assertFalse(contact_activity.touch(c))
        self.assertFalse(contact_activity.touch(None))
        self.assertFalse(contact_activity.touch(0))
        self.assertEqual(contact_activity.touch_by_phone('abc'), 0)
        self.assertEqual(contact_activity.touch_by_email(''), 0)

    def test_a_phone_number_finds_the_contact_however_it_is_written(self):
        a, b = contact('Dash', phone='561-555-0110'), contact('Parens', phone='(561) 555-0111')
        self.assertEqual(contact_activity.touch_by_phone('+1 (561) 555-0110'), 1)
        self.assertEqual(contact_activity.touch_by_phone('15615550111'), 1)
        self.assertEqual((fresh(a).interaction_count, fresh(b).interaction_count), (1, 1))
        self.assertEqual(contact_activity.touch_by_phone('561-555-9999'), 0)       # nobody has it: ignored

    def test_an_email_address_finds_the_contact_whatever_its_case(self):
        c = contact('Mail', email='Owner@Example.com')
        self.assertEqual(contact_activity.touch_by_email(' owner@example.COM '), 1)
        self.assertEqual(fresh(c).interaction_count, 1)


class WhereItFiresTests(TestCase):
    def setUp(self):
        self.admin = StaffProfile.objects.create(user=__import__('django.contrib.auth', fromlist=['get_user_model']).get_user_model().objects.create_user('adm', 'a@example.com', 'pw12345678'),
                                                 role='admin', is_company_admin=True)
        self.prop = Property.objects.create(name='Fire Condo', property_type='association', street='1 Main', city='Boca', state='FL', zip_code='33432')
        self.c = contact('Vendor One', phone='561-555-0100', email='v1@example.com')
        self.d = contact('Vendor Two')

    def count(self, c):
        return fresh(c).interaction_count

    def ticket(self, **kw):
        return Ticket.objects.create(title='Fix it', property=self.prop, created_by=self.admin.user, **kw)

    def test_a_ticket_given_to_a_contact_counts(self):
        t = self.ticket(assigned_contact=self.c)
        self.assertEqual(self.count(self.c), 1)
        self.assertIsNotNone(fresh(self.c).last_interaction_at)

    def test_saving_the_ticket_again_does_not_count_again(self):
        t = self.ticket(assigned_contact=self.c)
        t = Ticket.objects.get(pk=t.pk)
        t.title = 'Fix it properly'
        t.save()
        self.assertEqual(self.count(self.c), 1)

    def test_handing_the_ticket_to_somebody_else_counts_the_new_person(self):
        t = self.ticket(assigned_contact=self.c)
        t = Ticket.objects.get(pk=t.pk)
        t.assigned_contact = self.d
        t.save()
        self.assertEqual((self.count(self.c), self.count(self.d)), (1, 1))

    def test_a_ticket_given_to_staff_counts_nobody(self):
        self.ticket(assigned_staff=self.admin)
        self.assertEqual(self.count(self.c), 0)

    def test_a_save_that_does_not_touch_the_assignment_does_not_count(self):
        t = self.ticket(assigned_contact=self.c)
        t = Ticket.objects.get(pk=t.pk)
        t.title = 'Renamed'
        t.save(update_fields=['title'])
        self.assertEqual(self.count(self.c), 1)

    def test_a_person_attached_to_a_ticket_counts(self):
        t = self.ticket(assigned_staff=self.admin)
        TicketContact.objects.create(ticket=t, contact=self.d, role=TicketContact.Role.CONTRACTOR)
        self.assertEqual(self.count(self.d), 1)

    def test_a_sent_follow_up_counts_and_a_failed_one_does_not(self):
        t = self.ticket(assigned_staff=self.admin)
        FollowUpLog.objects.create(ticket=t, contact=self.c, channel='email', sent_to='v1@example.com', body='hi', success=True)
        FollowUpLog.objects.create(ticket=t, contact=self.c, channel='sms', sent_to='561-555-0100', body='hi', success=False)
        FollowUpLog.objects.create(ticket=t, contact=None, channel='email', sent_to='x@example.com', body='hi', success=True)
        self.assertEqual(self.count(self.c), 1)

    def test_a_quo_text_in_or_out_counts_the_contact_with_that_number(self):
        QuoMessage.objects.create(message_id='m1', direction='in', from_number='+15615550100', body='on my way')
        QuoMessage.objects.create(message_id='m2', direction='out', to_number='+15615550100', body='thanks')
        self.assertEqual(self.count(self.c), 2)

    def test_a_visit_given_to_a_contact_counts(self):
        vt = VisitType.objects.create(name='Turnover Clean')
        v = Visit.objects.create(property=self.prop, visit_type=vt, scheduled_date=timezone.localdate(), status=Visit.Status.SCHEDULED, assigned_contact=self.c)
        self.assertEqual(self.count(self.c), 1)
        v = Visit.objects.get(pk=v.pk)
        v.save()
        self.assertEqual(self.count(self.c), 1)                                  # a plain re-save is not an interaction
        v.assigned_contact = self.d
        v.save()
        self.assertEqual((self.count(self.c), self.count(self.d)), (1, 1))

    def test_a_bill_and_a_payment_for_a_vendor_count(self):
        assoc = make_assoc('Bill Condo')
        bill = payables.create_bill(assoc, date(2026, 10, 1), [('6906', D('100'), '')], vendor=self.c, number='1', due=date(2026, 10, 20), kind=Bill.Kind.ROUTINE)
        self.assertEqual(self.count(self.c), 1)
        from accounting.models import BankAccount
        from accounting import ledger
        bank = BankAccount.objects.create(association=assoc, name='Operating', last4='1234', kind='operating', account=ledger.account(assoc, '1110'))
        Payment.objects.create(association=assoc, bank_account=bank, number='1', date=date(2026, 10, 5), vendor=self.c, amount=D('100'), method=Payment.Method.CHECK)
        self.assertEqual(self.count(self.c), 2)

    def test_a_document_emailed_to_an_address_counts_its_contact(self):
        from legalforms import services
        doc = mock.Mock()
        doc.status = 'final'
        with mock.patch('legalforms.services.validate_email', create=True), mock.patch('django.core.mail.EmailMessage') as mail, mock.patch('legalforms.pdf.document_pdf', return_value=b'%PDF'), \
                mock.patch('legalforms.pdf.pdf_filename', return_value='a.pdf'), mock.patch('legalforms.services.config') as cfg:
            cfg.company.return_value = {'company_email': 'co@example.com'}
            mail.return_value.send.return_value = 1
            services.email_document(doc, 'v1@example.com', 'Subject', 'Message')
        self.assertEqual(self.count(self.c), 1)


class BackfillTests(TestCase):
    """Migration 0088 gave the ticker a head start from what was already there."""

    def run_backfill(self):
        module = importlib.import_module('core.migrations.0088_contact_interaction_tracker')
        module.backfill(apps, None)
        return module

    def test_the_sources_are_added_up_and_the_latest_moment_wins(self):
        user = __import__('django.contrib.auth', fromlist=['get_user_model']).get_user_model().objects.create_user('bf', 'bf@example.com', 'pw12345678')
        staff = StaffProfile.objects.create(user=user, role='admin', is_company_admin=True)
        prop = Property.objects.create(name='Back Condo', property_type='association', street='1 Main', city='Boca', state='FL', zip_code='33432')
        a, b, nobody = (contact('Active Vendor', source=Contact.Source.YARDI), contact('Quiet Vendor', source=Contact.Source.YARDI), contact('Never Dealt With', source=Contact.Source.YARDI))     # imported: typing them in is not counted
        t1 = Ticket.objects.create(title='One', property=prop, created_by=user, assigned_contact=a)
        t2 = Ticket.objects.create(title='Two', property=prop, created_by=user, assigned_staff=staff)
        TicketContact.objects.create(ticket=t2, contact=b, role=TicketContact.Role.CONTRACTOR)
        FollowUpLog.objects.create(ticket=t1, contact=a, channel='email', sent_to='a@example.com', body='x', success=True)
        FollowUpLog.objects.create(ticket=t1, contact=a, channel='email', sent_to='a@example.com', body='x', success=False)       # a failed send is not an interaction
        Contact.objects.update(last_interaction_at=None, interaction_count=0)                                                    # as if the ticker did not exist yet
        old = timezone.now() - timedelta(days=40)
        Ticket.objects.filter(pk=t1.pk).update(updated_at=old)
        self.run_backfill()
        a, b, nobody = fresh(a), fresh(b), fresh(nobody)
        self.assertEqual(a.interaction_count, 2)                                  # one ticket and one follow-up that went out
        self.assertGreater(a.last_interaction_at, old)                            # the follow-up is newer than the ticket
        self.assertEqual(b.interaction_count, 1)
        self.assertEqual(nobody.interaction_count, 0)
        self.assertIsNone(nobody.last_interaction_at)

    def test_a_contact_typed_in_by_hand_counts_once_but_an_import_does_not(self):
        hand = contact('By Hand', source=Contact.Source.MANUAL)
        imported = contact('Imported', source=Contact.Source.YARDI)
        Contact.objects.update(last_interaction_at=None, interaction_count=0)
        self.run_backfill()
        self.assertEqual(fresh(hand).interaction_count, 1)
        self.assertIsNone(fresh(imported).last_interaction_at)

    def test_it_can_be_reversed(self):
        c = contact(source=Contact.Source.MANUAL)
        Contact.objects.update(last_interaction_at=None, interaction_count=0)
        module = self.run_backfill()
        self.assertEqual(fresh(c).interaction_count, 1)
        module.clear(apps, None)
        self.assertEqual(fresh(c).interaction_count, 0)
        self.assertIsNone(fresh(c).last_interaction_at)
