from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import Property

NO_MANIFEST = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
               'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}


@override_settings(STORAGES=NO_MANIFEST)
class OwnerLegalNameTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.prop = Property.objects.create(name='800 Tropic', property_type=Property.Type.SHORT_TERM_RENTAL, financials_managed=True)

    def test_legal_name_is_saved_from_the_property_card(self):
        url = reverse('property_detail', args=[self.prop.pk])
        r = self.client.post(url, {'action': 'qb_set_owner_legal_name', 'owner_legal_name': '  4KC,   LLC '})
        self.assertEqual(r.status_code, 302)
        self.prop.refresh_from_db()
        self.assertEqual(self.prop.owner_legal_name, '4KC, LLC')
        self.assertContains(self.client.get(url), 'value="4KC, LLC"')

    def test_missing_name_shows_a_warning_not_a_block(self):
        self.assertContains(self.client.get(reverse('property_detail', args=[self.prop.pk])), 'Not set')
        self.assertEqual(self.client.get(reverse('close_pay_owners')).status_code, 200)
