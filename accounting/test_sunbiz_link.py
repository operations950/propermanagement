from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from . import ledger, sunbiz
from .tests import make_assoc

STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}


class SunbizLinkTests(TestCase):
    def test_the_link_is_a_name_search_that_sunbiz_accepts_not_a_document_number_search(self):
        link = sunbiz.search_link('Linton Woods Condominium Association, Inc.')
        self.assertEqual(link, 'https://search.sunbiz.org/Inquiry/CorporationSearch/SearchResults/EntityName/Linton%20Woods%20Condominium%20Association%20Inc/Page1?searchNameOrder=LINTONWOODSCONDOMINIUMASSOCIATIONINC')
        self.assertNotIn('DocumentNumber', link)

    def test_punctuation_is_dropped_the_way_the_lookup_does_it(self):
        self.assertIn('/EntityName/St%20Andrews%20Glen%20Condominium%20Association%20Inc/', sunbiz.search_link('St. Andrews Glen Condominium Association, Inc.'))

    def test_no_name_means_no_link(self):
        self.assertEqual(sunbiz.search_link(''), '')
        self.assertEqual(sunbiz.search_link(None), '')


@override_settings(STORAGES=STORAGES)
class AssociationLinkTests(TestCase):
    def setUp(self):
        self.prop = make_assoc('Test Condos')
        self.profile = ledger.profile_for(self.prop)
        self.user = get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678')
        self.client.force_login(self.user)

    def test_the_profile_link_comes_from_the_legal_name_even_when_an_old_bad_link_is_saved(self):
        self.profile.legal_name = 'Test Condos Association, Inc.'
        self.profile.sunbiz_url = 'https://search.sunbiz.org/Inquiry/CorporationSearch/SearchResults?inquiryType=DocumentNumber&searchTerm=123'
        self.profile.save()
        self.assertEqual(self.profile.sunbiz_link(), sunbiz.search_link('Test Condos Association, Inc.'))

    def test_without_a_legal_name_a_link_someone_typed_is_kept_and_with_neither_there_is_none(self):
        self.profile.legal_name, self.profile.sunbiz_url = '', 'https://example.org/typed'
        self.assertEqual(self.profile.sunbiz_link(), 'https://example.org/typed')
        self.profile.sunbiz_url = ''
        self.assertEqual(self.profile.sunbiz_link(), '')

    def test_the_settings_page_shows_the_working_link_and_none_for_a_blank_record(self):
        url = reverse('accounting:settings', args=[self.prop.pk])
        self.assertNotIn('Open on SunBiz', self.client.get(url).content.decode())
        self.profile.legal_name = 'Test Condos Association, Inc.'
        self.profile.sunbiz_url = 'https://search.sunbiz.org/Inquiry/CorporationSearch/SearchResults?inquiryType=DocumentNumber&searchTerm=123'
        self.profile.save()
        html = self.client.get(url).content.decode()
        self.assertIn('Open on SunBiz', html)
        self.assertIn('/SearchResults/EntityName/Test%20Condos%20Association%20Inc/', html)
        self.assertNotIn('inquiryType=DocumentNumber&amp;searchTerm=123" target', html)
