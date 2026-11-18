"""The four line-of-business pages (Associations, Short-term rentals, Long-term rentals, Commercial): the cards on each, who sees the money cards, and Legal forms living on the Associations page."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import Property, StaffProfile
from workspace import menu

User = get_user_model()

# What each page must offer: (a link that has to be on it, a link that must not be).
MUST = {
    'associations': ['legalforms:list', 'ws_compliance', 'ws_projects', 'ticket_list', 'faq_review_queue', 'missing_documents', 'accounting:home', 'accounting:collections_home', 'accounting:receipt_pick',
                     'accounting:budgets', 'accounting:master_chart', 'accounting:payables_home', 'accounting:payables_print_checks', 'accounting:payables_print_envelopes'],
    'short-term-rentals': ['onsite_str_today', 'onsite_dashboard', 'onsite_rentals_calendar', 'onsite_reservation_list', 'onsite_reservation_review', 'onsite_block_review', 'onsite_booking_feeds',
                           'onsite_performance', 'onsite_cleaner_activity', 'onsite_cleaning_payments', 'supplies:cart', 'close_home', 'close_overview', 'close_bank_tieout', 'close_payouts',
                           'tourist_tax', 'close_sales_tax', 'close_escrow', 'close_misc', 'owner_portal_admin'],
    'long-term-rentals': ['ticket_list', 'ws_compliance', 'ws_projects', 'missing_documents', 'close_home', 'close_rent_collection', 'close_pay_ltr_owners', 'commercial:renewals', 'close_overview',
                          'close_bank_tieout', 'close_escrow', 'close_misc', 'owner_portal_admin'],
    'commercial': ['ticket_list', 'ws_compliance', 'ws_projects', 'missing_documents', 'accounting:home_commercial', 'commercial:renewals', 'close_sales_tax', 'accounting:payables_home',
                   'accounting:payables_print_checks', 'accounting:payables_print_envelopes'],
}
MONEY = {   # cards that only people with the Financial switch see
    'associations': ['accounting:home', 'accounting:collections_home', 'accounting:budgets', 'accounting:payables_home'],
    'short-term-rentals': ['close_home', 'tourist_tax', 'close_payouts', 'owner_portal_admin'],
    'long-term-rentals': ['close_home', 'close_rent_collection', 'close_pay_ltr_owners'],
    'commercial': ['accounting:home_commercial', 'accounting:payables_home'],
}


def person(email, **flags):
    user = User.objects.create_user(username=email, email=email, password='pw12345678')
    return StaffProfile.objects.create(user=user, **flags)


class LinePagesTests(TestCase):
    def setUp(self):
        self.fin = person('fin@x.com', can_financial=True)
        self.plain = person('plain@x.com')
        Property.objects.create(name='Test Condo', property_type=Property.Type.ASSOCIATION, city='Boca')

    def page(self, who, slug):
        self.client.force_login(who.user)
        return self.client.get(reverse('ws_line', args=[slug]))

    def test_every_line_page_opens_and_offers_its_cards(self):
        for slug, names in MUST.items():
            r = self.page(self.fin, slug)
            self.assertEqual(r.status_code, 200, slug)
            html = r.content.decode()
            for name in names:
                self.assertIn(f'href="{reverse(name)}"', html, f'{slug}: {name}')

    def test_the_money_cards_are_for_people_with_the_financial_switch(self):
        for slug, names in MONEY.items():
            html = self.page(self.plain, slug).content.decode()
            for name in names:
                self.assertNotIn(f'href="{reverse(name)}"', html, f'{slug}: {name}')
            self.assertIn('href="' + reverse('ticket_list') + '"', html, slug)             # the everyday cards are still there

    def test_legal_forms_is_on_the_associations_page_and_nowhere_else(self):
        url = reverse('legalforms:list')
        self.assertIn(f'href="{url}"', self.page(self.plain, 'associations').content.decode())
        for slug in ('short-term-rentals', 'long-term-rentals', 'commercial'):
            self.assertNotIn(f'href="{url}"', self.page(self.plain, slug).content.decode(), slug)

    def test_legal_forms_is_not_a_main_menu_button_any_more(self):
        self.assertNotIn('legal', menu.BASE_MENU)
        self.client.force_login(self.plain.user)
        html = self.client.get(reverse('ws_home')).content.decode()
        self.assertNotIn('Legal forms</', html.split('class="ws-lines"')[0].split('ws-nav')[-1] if 'ws-nav' in html else '')

    def test_the_menu_keeps_properties_lit_inside_legal_forms(self):
        class R:
            user = self.plain.user
            path = '/documents/'
        lit = [i['key'] for i in menu.build(R, self.plain) if i['active']]
        self.assertEqual(lit, ['properties'])

    def test_no_card_points_at_a_page_that_does_not_exist(self):
        for slug, names in MUST.items():
            for name in names:
                self.assertTrue(reverse(name), name)

    def test_the_properties_still_list_under_the_cards(self):
        html = self.page(self.plain, 'associations').content.decode()
        self.assertIn('Test Condo', html)
        self.assertIn('Boca', html)


class UpFromTheLineCardsTests(TestCase):
    """A card on a line page leads to a screen whose Up link brings the person back to that line page."""

    def setUp(self):
        self.fin = person('fin2@x.com', can_financial=True)
        self.client.force_login(self.fin.user)

    def visit(self, name, *args):
        return self.client.get(reverse(name, args=list(args)), HTTP_ACCEPT='text/html')

    def test_the_line_page_names_itself_on_the_up_link_of_the_screens_below_it(self):
        for slug, label, name in (('associations', 'Associations', 'legalforms:list'), ('short-term-rentals', 'Short-term rentals', 'tourist_tax'),
                                  ('long-term-rentals', 'Long-term rentals', 'close_rent_collection'), ('commercial', 'Commercial', 'commercial:renewals')):
            self.visit('ws_line', slug)
            html = self.visit(name).content.decode()
            self.assertIn(f'href="{reverse("ws_line", args=[slug])}"', html, name)
            self.assertIn(label, html, name)

    def test_legal_forms_with_no_trail_goes_up_to_the_associations_page(self):
        html = self.visit('legalforms:list').content.decode()
        self.assertIn(f'href="{reverse("ws_line", args=["associations"])}"', html)

    def test_a_screen_reached_from_financials_still_goes_back_to_financials(self):
        self.visit('ws_financials')
        html = self.visit('tourist_tax').content.decode()
        self.assertIn(f'href="{reverse("ws_financials")}"', html)
