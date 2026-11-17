"""The budget meeting notice (and its affidavit) is made only by following the budget steps: it is not offered anywhere else."""
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import Property

from .models import BUDGET_ONLY_SLUGS, DocumentTemplate

STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}


@override_settings(STORAGES=STORAGES)
class BudgetNoticeOnlyFromTheBudgetStepsTests(TestCase):
    def setUp(self):
        call_command('seed_legal_templates', verbosity=0)
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.prop = Property.objects.create(name='Notice Condos', property_type=Property.Type.ASSOCIATION)

    def test_the_budget_templates_exist_for_the_budget_steps_to_use(self):
        for slug in BUDGET_ONLY_SLUGS:
            self.assertTrue(DocumentTemplate.objects.filter(slug=slug).exists(), slug)

    def test_they_are_not_in_the_legal_forms_list(self):
        html = self.client.get(reverse('legalforms:list')).content.decode()
        self.assertNotIn('Notice of Budget Meeting', html)
        self.assertNotIn('Affidavit of Mailing', html)
        other = DocumentTemplate.objects.filter(is_active=True, companion_of__isnull=True).exclude(slug__in=BUDGET_ONLY_SLUGS).first()
        self.assertIn(other.name, html)                                                     # the other forms are still there

    def test_a_property_page_does_not_offer_them_either(self):
        html = self.client.get(reverse('property_detail', args=[self.prop.pk])).content.decode()
        self.assertNotIn('Notice of Budget Meeting', html)

    def test_starting_one_by_its_address_goes_to_the_budget_steps(self):
        for slug in BUDGET_ONLY_SLUGS:
            r = self.client.get(reverse('legalforms:new', args=[slug]) + f'?property={self.prop.pk}', follow=True)
            self.assertEqual(r.redirect_chain[-1][0], reverse('accounting:budgets'))
            self.assertContains(r, 'made by following the budget steps')
            r = self.client.post(reverse('legalforms:new', args=[slug]), {'property': self.prop.pk}, follow=True)
            self.assertEqual(r.redirect_chain[-1][0], reverse('accounting:budgets'))

    def test_another_form_still_opens_normally(self):
        other = DocumentTemplate.objects.filter(is_active=True, companion_of__isnull=True).exclude(slug__in=BUDGET_ONLY_SLUGS).first()
        r = self.client.get(reverse('legalforms:new', args=[other.slug]) + f'?property={self.prop.pk}')
        self.assertEqual(r.status_code, 200)
