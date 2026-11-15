"""A vendor's mailing address on the contact screens, and the Documents and Processes cards that came off them."""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from accounting import check_runs
from accounting.models import VendorProfile

from .models import Contact, ContactDocument

STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}
ADDRESS = {'street': '1330 West Industrial Avenue, #107', 'city': 'Boynton Beach', 'state': 'fl', 'zip_code': '33426'}


@override_settings(STORAGES=STORAGES)
class VendorAddressOnTheContactTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.vendor = Contact.objects.create(name='Total Power Generation LLC', contact_type='vendor', trade='Electrical', phone='561-555-0199')
        self.owner = Contact.objects.create(name='Dana Whitfield', contact_type='board_member', phone='561-555-0110')

    def edit(self, contact, **extra):
        data = {'name': contact.name, 'contact_type': contact.contact_type, 'trade': contact.trade, 'phone': contact.phone, 'email': contact.email, **extra}
        return self.client.post(reverse('contact_edit', args=[contact.pk]), data)

    def test_the_edit_screen_has_the_address_fields_for_a_vendor_to_fill_in(self):
        html = self.client.get(reverse('contact_edit', args=[self.vendor.pk])).content.decode()
        for name in ('street', 'city', 'state', 'zip_code'):
            self.assertIn(f'name="{name}"', html)
        self.assertIn('where we mail their checks', html)

    def test_a_vendors_address_is_saved_and_shows_again_when_it_is_opened(self):
        response = self.edit(self.vendor, **ADDRESS)
        self.assertEqual(response.status_code, 302)
        profile = VendorProfile.objects.get(contact=self.vendor)
        self.assertEqual((profile.street, profile.city, profile.state, profile.zip_code), ('1330 West Industrial Avenue, #107', 'Boynton Beach', 'FL', '33426'))
        html = self.client.get(reverse('contact_edit', args=[self.vendor.pk])).content.decode()
        self.assertIn('value="1330 West Industrial Avenue, #107"', html)
        self.assertIn('value="Boynton Beach"', html)
        self.assertIn('value="FL"', html)

    def test_the_address_is_what_an_envelope_prints(self):
        self.edit(self.vendor, **ADDRESS)
        self.assertEqual(check_runs.vendor_address_lines(self.vendor), ['1330 West Industrial Avenue, #107', 'Boynton Beach, FL 33426'])

    def test_an_address_already_on_the_vendor_is_changed_in_place_and_other_profile_details_are_kept(self):
        VendorProfile.objects.create(contact=self.vendor, street='old', city='Old City', state='NY', zip_code='10001', tax_id='12-3456789', is_1099=True)
        self.edit(self.vendor, **ADDRESS)
        profile = VendorProfile.objects.get(contact=self.vendor)
        self.assertEqual((profile.city, profile.tax_id, profile.is_1099), ('Boynton Beach', '12-3456789', True))
        self.assertEqual(VendorProfile.objects.filter(contact=self.vendor).count(), 1)

    def test_clearing_the_address_clears_it(self):
        self.edit(self.vendor, **ADDRESS)
        self.edit(self.vendor, street='', city='', state='', zip_code='')
        self.assertEqual(VendorProfile.objects.get(contact=self.vendor).address_lines, [])

    def test_a_vendor_saved_with_no_address_gets_no_empty_profile(self):
        self.edit(self.vendor)
        self.assertFalse(VendorProfile.objects.filter(contact=self.vendor).exists())

    def test_a_new_vendor_is_made_with_its_address(self):
        response = self.client.post(reverse('contact_create'), {'name': 'Palm Roofing', 'contact_type': 'vendor', 'trade': 'Roofing', **ADDRESS})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(VendorProfile.objects.get(contact__name='Palm Roofing').address_lines[1], 'Boynton Beach, FL 33426')

    def test_a_contact_who_is_not_a_vendor_does_not_get_a_profile_from_stray_address_fields(self):
        self.edit(self.owner, **ADDRESS)
        self.assertFalse(VendorProfile.objects.filter(contact=self.owner).exists())

    def test_the_contact_page_shows_a_vendors_address_or_asks_for_one(self):
        url = reverse('contact_detail', args=[self.vendor.pk])
        html = self.client.get(url).content.decode()
        self.assertIn('Add a mailing address', html)
        self.edit(self.vendor, **ADDRESS)
        html = self.client.get(url).content.decode()
        self.assertIn('1330 West Industrial Avenue, #107', html)
        self.assertIn('Boynton Beach, FL 33426', html)
        self.assertNotIn('Add a mailing address', html)
        self.assertNotIn('Add a mailing address', self.client.get(reverse('contact_detail', args=[self.owner.pk])).content.decode())


@override_settings(STORAGES=STORAGES)
class TheCardsThatCameOffTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.vendor = Contact.objects.create(name='Total Power Generation LLC', contact_type='vendor', trade='Electrical')

    def test_the_edit_screen_has_no_documents_or_processes_card(self):
        html = self.client.get(reverse('contact_edit', args=[self.vendor.pk])).content.decode()
        self.assertNotIn('data-lucide="folder"', html)
        self.assertNotIn('Documents', html)
        self.assertNotIn('Processes', html)
        self.assertNotIn('add_document', html)

    def test_the_contact_page_has_no_documents_section(self):
        html = self.client.get(reverse('contact_detail', args=[self.vendor.pk])).content.decode()
        self.assertNotIn('Documents', html)
        self.assertIn('Open tickets', html)

    def test_deleting_a_contact_still_warns_about_the_documents_it_would_take_with_it(self):
        ContactDocument.objects.create(contact=self.vendor, name='W-9', file='contact_documents/w9.pdf')
        html = self.client.get(reverse('contact_edit', args=[self.vendor.pk])).content.decode()
        self.assertIn('permanently delete 1 document', html)
