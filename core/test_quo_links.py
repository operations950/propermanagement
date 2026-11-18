"""Phone contacts: Call and Text open the Quo app first (core/quo_links.py, static/js/quo-links.js) with the phone's own tel: / sms: link as the fallback, and the back-office
alerts are left off the phone screen."""
from django.contrib.auth import get_user_model
from django.template import Context, Template
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from . import quo_links
from .models import Contact, ContactImportCandidate, StaffProfile

STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}


class TurningANumberIntoE164Tests(SimpleTestCase):
    def test_the_ways_a_us_number_is_typed_all_become_the_same_number(self):
        for typed in ('(561) 555-0100', '561-555-0100', '561.555.0100', '5615550100', '+1 561 555 0100', '+15615550100', '1 (561) 555-0100', '  561 555 0100  ', '1-561-555-0100'):
            self.assertEqual(quo_links.to_e164(typed), '+15615550100', typed)

    def test_a_number_from_another_country_must_carry_its_plus(self):
        self.assertEqual(quo_links.to_e164('+44 20 7946 0958'), '+442079460958')
        self.assertEqual(quo_links.to_e164('+52 55 1234 5678'), '+525512345678')
        self.assertEqual(quo_links.to_e164('44 20 7946 0958'), '')                             # without the plus it cannot be told from a US number

    def test_a_number_with_an_extension_has_no_quo_form_because_quo_cannot_dial_an_extension(self):
        for typed in ('561-555-0100 x12', '(561) 555-0100 ext. 12', '561-555-0100 ext 4', '561-555-0100 extension 4', '561-555-0100 #12', '+1 561 555 0100 X7'):
            self.assertEqual(quo_links.to_e164(typed), '', typed)

    def test_junk_and_short_numbers_have_no_quo_form(self):
        for typed in ('', None, '   ', 'call after 5', '555-0100', '12345', '1-800-FLOWERS', 'n/a', '061-555-0100', '+1 061 555 0100', '+1 561 555', '+123', '561-555-01000', '++'):
            self.assertEqual(quo_links.to_e164(typed), '', repr(typed))


class TheQuoLinksTests(SimpleTestCase):
    def test_dial_and_message_links_are_url_encoded_with_the_plus_escaped(self):
        self.assertEqual(quo_links.dial_url('(561) 555-0100'), 'openphone://dial?number=%2B15615550100&action=call')
        self.assertEqual(quo_links.message_url('(561) 555-0100'), 'openphone://message?number=%2B15615550100')

    def test_message_text_is_encoded_when_given(self):
        self.assertEqual(quo_links.message_url('561-555-0100', 'Hi & thanks!'), 'openphone://message?number=%2B15615550100&text=Hi%20%26%20thanks%21')

    def test_a_number_that_cannot_be_made_into_one_gets_no_link(self):
        self.assertEqual(quo_links.dial_url('555-0100'), '')
        self.assertEqual(quo_links.message_url('561-555-0100 x2'), '')

    @override_settings(QUO_FROM_NUMBER='')
    def test_no_sending_line_is_added_by_default(self):
        self.assertNotIn('from=', quo_links.dial_url('561-555-0100'))
        self.assertNotIn('from=', quo_links.message_url('561-555-0100'))

    @override_settings(QUO_FROM_NUMBER='(561) 599-6300')
    def test_the_shared_line_is_added_as_from_when_it_is_set_and_made_e164_too(self):
        self.assertEqual(quo_links.dial_url('561-555-0100'), 'openphone://dial?number=%2B15615550100&action=call&from=%2B15615996300')
        self.assertEqual(quo_links.message_url('561-555-0100'), 'openphone://message?number=%2B15615550100&from=%2B15615996300')

    @override_settings(QUO_FROM_NUMBER='not a number')
    def test_a_sending_line_that_is_not_a_number_is_ignored_rather_than_breaking_the_link(self):
        self.assertEqual(quo_links.dial_url('561-555-0100'), 'openphone://dial?number=%2B15615550100&action=call')

    def test_the_template_filters(self):
        t = Template('{% load core_extras %}{{ a|quo_dial_url }}|{{ a|quo_text_url }}|{{ b|quo_dial_url }}')
        parts = t.render(Context({'a': '(561) 555-0100', 'b': '555-0100'})).split('|')
        self.assertEqual(parts[2], '')
        self.assertTrue(parts[0].startswith('openphone://dial?number=%2B15615550100'))
        self.assertTrue(parts[1].startswith('openphone://message?number=%2B15615550100'))


@override_settings(STORAGES=STORAGES, QUO_FROM_NUMBER='')
class QuoButtonsOnTheContactScreensTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678')
        StaffProfile.objects.create(user=self.user, role='admin', is_company_admin=True)
        self.client.force_login(self.user)
        self.dana = Contact.objects.create(name='Dana Whitfield', contact_type='board_member', phone='(561) 555-0110', email='dana@example.com')
        self.office = Contact.objects.create(name='Front Office', contact_type='vendor', phone='561-555-0199 x12')       # an extension: no Quo link
        self.odd = Contact.objects.create(name='Short Number', contact_type='vendor', phone='555-0100')
        self.nophone = Contact.objects.create(name='No Phone', contact_type='vendor', email='n@example.com')

    def card(self, html, name):
        return html.split(name, 1)[1].split('class="np-row cl-row"')[0]                   # the one row of that person (from their name to the next row)

    def test_a_card_has_call_text_and_email_with_the_quo_links_beside_the_ordinary_ones(self):
        html = self.client.get(reverse('contact_list')).content.decode()
        self.assertEqual(html.count('class="np-row cl-row"'), 4)
        card = self.card(html, 'Dana Whitfield')
        self.assertIn('href="tel:5615550110" data-quo="openphone://dial?number=%2B15615550110&amp;action=call" class="cl-act"', card)
        self.assertIn('href="sms:5615550110" data-quo="openphone://message?number=%2B15615550110" class="cl-act"', card)
        self.assertIn('href="mailto:dana@example.com" class="cl-act"', card)
        self.assertIn('js/quo-links.js', html)

    def test_a_number_without_a_quo_form_keeps_the_plain_phone_links_only(self):
        html = self.client.get(reverse('contact_list')).content.decode()
        for name in ('Front Office', 'Short Number'):
            card = self.card(html, name)
            self.assertIn('class="cl-act" title="Call"', card)
            self.assertNotIn('data-quo', card)
            self.assertIn('href="sms:', card)
        self.assertNotIn('href="tel:', self.card(html, 'No Phone'))

    def test_the_contact_page_buttons_do_the_same(self):
        html = self.client.get(reverse('contact_detail', args=[self.dana.pk])).content.decode()
        self.assertIn('href="tel:5615550110" data-quo="openphone://dial?number=%2B15615550110&amp;action=call" class="ml-btn ml-btn-primary"', html)
        self.assertIn('href="sms:5615550110" data-quo="openphone://message?number=%2B15615550110" class="ml-btn"', html)
        self.assertIn('js/quo-links.js', html)
        office = self.client.get(reverse('contact_detail', args=[self.office.pk])).content.decode()
        self.assertNotIn('data-quo', office)
        self.assertIn('href="tel:', office)

    @override_settings(QUO_FROM_NUMBER='+15615996300')
    def test_the_shared_line_reaches_the_buttons_when_it_is_set(self):
        html = self.client.get(reverse('contact_detail', args=[self.dana.pk])).content.decode()
        self.assertIn('action=call&amp;from=%2B15615996300', html)
        self.assertIn('message?number=%2B15615550110&amp;from=%2B15615996300', html)

    def test_the_script_asks_quo_first_waits_then_falls_back_and_remembers(self):
        js = open('static/js/quo-links.js', encoding='utf-8').read()
        for needle in ("a[data-quo]", 'visibilitychange', 'pagehide', 'window.location.href = quo', 'window.location.href = plain', 'localStorage', '1500'):
            self.assertIn(needle, js)

    def test_the_back_office_alerts_are_left_off_the_phone_screen_but_kept_for_a_computer(self):
        ContactImportCandidate.objects.create(source=Contact.Source.choices[0][0], name='Someone New', phone='561-555-0000')
        html = self.client.get(reverse('contact_list')).content.decode()
        self.assertIn('id="cl-alerts-panel" hidden', html)                              # the alerts sit behind the Alerts button, closed until it is pressed
        css = open('static/css/contacts.css', encoding='utf-8').read().split('@media (max-width: 575px)')[1]
        for hidden in ('.cl-alerts-btn', '.cl-import', '.cl-panel'):
            self.assertIn(hidden, css)                                                  # and the phone leaves the button, the panel and Import out
        self.assertIn('display: none !important', css)
        self.assertIn('New contact', html)                                              # the one thing left in the header on a phone
        self.assertIn('id="contact-paper"', html)                                       # the boxes are the page now
