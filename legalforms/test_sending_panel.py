from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import Property, StaffProfile

from .models import DocumentTemplate, GeneratedDocument

STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}


@override_settings(STORAGES=STORAGES)
class SendingPanelTests(TestCase):
    """The Sending panel asks for the mailing date only: no 'How', no tracking number."""

    def setUp(self):
        user = get_user_model().objects.create_user('lf-admin', password='pw12345678')
        StaffProfile.objects.create(user=user, role=StaffProfile.Role.ACCOUNTING, is_company_admin=True)
        self.client.force_login(user)
        self.user = user
        self.prop = Property.objects.create(name='Test Condos', property_type=Property.Type.ASSOCIATION)
        self.template = DocumentTemplate.objects.create(slug='late-notice', name='Notice of late assessment', body='<p>Hello</p>', fields=[])
        self.doc = GeneratedDocument.objects.create(template=self.template, template_version=1, property=self.prop, subject='Pat Owner, unit 1', body_html='<p>Hello</p>',
                                                    status=GeneratedDocument.Status.FINAL, created_by=user)

    def act(self, **post):
        return self.client.post(reverse('legalforms:act', args=[self.doc.pk]), {'action': 'mailing', **post}, follow=True)

    def test_saving_with_only_a_date(self):
        resp = self.act(mailed_on='2026-10-15')
        self.assertContains(resp, 'Mailing recorded.')
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.mailed_on, date(2026, 10, 15))

    def test_method_and_tracking_are_not_asked_for_or_shown(self):
        html = self.client.get(reverse('legalforms:detail', args=[self.doc.pk])).content.decode()
        self.assertIn('name="mailed_on"', html)
        self.assertNotIn('name="mail_method"', html)
        self.assertNotIn('name="tracking"', html)
        self.assertNotIn('Tracking number', html)

    def test_a_posted_method_or_tracking_number_is_ignored(self):
        self.doc.mail_method, self.doc.tracking = 'Certified mail', '7020 1234'
        self.doc.save()
        self.act(mailed_on='2026-10-20', mail_method='Email', tracking='ZZZ')
        self.doc.refresh_from_db()
        self.assertEqual((self.doc.mail_method, self.doc.tracking), ('Certified mail', '7020 1234'))      # old data is kept, never changed from here
        self.assertEqual(self.doc.mailed_on, date(2026, 10, 20))

    def test_clearing_the_date_and_a_bad_date(self):
        self.doc.mailed_on = date(2026, 10, 1)
        self.doc.save()
        self.act(mailed_on='')
        self.doc.refresh_from_db()
        self.assertIsNone(self.doc.mailed_on)
        resp = self.act(mailed_on='not a date')
        self.assertContains(resp, 'Enter the mailing date.')

    def test_a_document_with_old_method_and_tracking_still_renders(self):
        self.doc.mailed_on, self.doc.mail_method, self.doc.tracking = date(2026, 9, 1), 'First class mail', 'ABC123'
        self.doc.save()
        resp = self.client.get(reverse('legalforms:detail', args=[self.doc.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'value="2026-09-01"')
        listing = self.client.get(reverse('legalforms:list') + f'?property={self.prop.pk}')
        self.assertContains(listing, 'mailed Sep 1')
