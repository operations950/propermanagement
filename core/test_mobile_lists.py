"""The phone versions of Properties and Contacts: big rows, one tap per thing, a contact page, and the desktop screens left as they were."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.template import Context, Template
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from tickets.models import Ticket, TicketContact

from .models import Contact, Property, StaffProfile, Unit

STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}


def prop(name, kind='association', **kw):
    return Property.objects.create(name=name, property_type=kind, street=kw.pop('street', '1 Main St'), city='Boca Raton', state='FL', zip_code='33432', **kw)


@override_settings(STORAGES=STORAGES)
class PhoneLists(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678')
        StaffProfile.objects.create(user=self.user, role='admin', is_company_admin=True)               # a ticket needs someone to be assigned to
        self.client.force_login(self.user)
        self.grand = prop('St Andrews Grand')
        self.palm = prop('Palm Cottage 12', 'str', street='12 Palm Way')
        self.old = prop('Old Duplex', 'ltr', is_active=False)
        for label in ('101', '102'):
            Unit.objects.create(property=self.grand, label=label)
        self.board = Contact.objects.create(name='Dana Whitfield', contact_type='board_member', phone='(561) 555-0110', email='dana@example.com')
        self.board.properties.add(self.grand)
        self.vendor = Contact.objects.create(name='Rainsaver Gutters', contact_type='vendor', trade='Gutters', phone='561-555-0199')
        self.vendor.properties.add(self.grand)
        self.nophone = Contact.objects.create(name='Quick Plumbing', contact_type='vendor', trade='Plumbing', email='info@quick.example.com')
        for title in ('Gate keypad', 'Pool pump'):
            Ticket.objects.create(title=title, property=self.grand, created_by=self.user)
        Ticket.objects.create(title='Done one', property=self.grand, created_by=self.user, status=Ticket.Status.COMPLETED)

    # the properties list is covered by core/test_property_list.py (the newspaper boxes)

    # ---------------------------------------------------------------- a property
    def test_the_phone_header_has_one_primary_action_and_big_buttons(self):
        html = self.client.get(reverse('property_detail', args=[self.grand.pk])).content.decode()
        self.assertIn('data-m-acc-root', html)
        self.assertIn(f'{reverse("ticket_create")}?property={self.grand.pk}', html)
        self.assertIn('Tickets (2)', html)
        self.assertIn('https://www.google.com/maps/search/?api=1&amp;query=1+Main+St', html)           # the address opens in Maps
        self.assertIn('href="tel:5615550110"', html)                                       # the board member, not the vendor, is offered first
        self.assertIn('Call Dana Whitfield', html)
        self.assertIn('js/mobile-accordion.js', html)
        self.assertIn('css/mobile-lists.css', html)

    def test_no_call_button_when_nobody_at_the_property_has_a_phone_number(self):
        html = self.client.get(reverse('property_detail', args=[self.palm.pk])).content.decode()
        self.assertNotIn('Call ', html.split('class="pp d-md-none"')[1].split('<div class="pp-more"')[0])
        self.assertIn('New ticket', html)

    def test_the_desktop_header_and_section_nav_are_kept_and_only_shown_on_wide_screens(self):
        html = self.client.get(reverse('property_detail', args=[self.grand.pk])).content.decode()
        self.assertIn('class="my-3 d-none d-md-block"', html)
        self.assertIn('d-none d-md-flex flex-wrap gap-2 mb-3 small" aria-label="Sections of this property"', html)
        self.assertIn('id="tickets"', html)                                               # the cards the phone turns into sections are all still on the page
        self.assertIn('id="contacts"', html)

    def test_the_call_button_goes_to_the_most_useful_contact_first(self):
        t = Template('{% load core_extras %}{% property_call_contact p as c %}{{ c.name }}')
        self.assertEqual(t.render(Context({'p': self.grand})), 'Dana Whitfield')
        only_vendor = prop('Vendor Only')
        v = Contact.objects.create(name='Zed Vendor', contact_type='vendor', phone='561-555-0000')
        v.properties.add(only_vendor)
        self.assertEqual(t.render(Context({'p': only_vendor})), 'Zed Vendor')
        self.assertEqual(t.render(Context({'p': self.palm})), '')                       # nobody there has a phone number

    def test_the_phone_number_and_map_filters(self):
        t = Template('{% load core_extras %}{{ a|tel_href }}|{{ b|tel_href }}|{{ c|maps_href }}|{{ d|maps_href }}')
        parts = t.render(Context({'a': '(561) 555-0142', 'b': '+1 561 555 0142 ext 5', 'c': '12 Palm Way, Delray Beach', 'd': ''})).split('|')
        self.assertEqual(parts[0], '5615550142')
        self.assertEqual(parts[1], '+156155501425')                                       # a leading + is kept, everything else that is not a digit goes
        self.assertIn('query=12+Palm+Way%2C+Delray+Beach', parts[2])
        self.assertEqual(parts[3], '')

    # ---------------------------------------------------------------- the contacts list
    def test_a_contact_is_one_card_the_name_opens_it_and_call_text_and_email_are_their_own_buttons(self):
        html = self.client.get(reverse('contact_list')).content.decode()
        self.assertEqual(html.count('class="ml-card ml-contact"'), 3)
        self.assertIn(f'href="{reverse("contact_detail", args=[self.board.pk])}" class="ml-card-main"', html)
        self.assertIn('href="tel:5615550110" data-quo="openphone://dial?number=%2B15615550110&amp;action=call" class="ml-act"', html)
        self.assertIn('href="mailto:dana@example.com" class="ml-act"', html)
        self.assertIn('Vendor / Contractor &middot; Plumbing', html)
        card = html.split('Quick Plumbing')[-1].split('class="ml-card ml-contact"')[0]
        self.assertNotIn('Call</a>', card)                                                 # no phone, no Call button

    def test_the_contact_search_and_type_filters_still_work(self):
        j = self.client.get(reverse('contact_list') + '?q=gutters', HTTP_X_REQUESTED_WITH='XMLHttpRequest').json()
        self.assertIn('Rainsaver Gutters', j['mobile'])
        self.assertNotIn('Dana Whitfield', j['mobile'])
        self.assertIn('Rainsaver Gutters', j['desktop'])
        resp = self.client.get(reverse('contact_list') + '?type=board_member')
        self.assertEqual([c.name for c in resp.context['contacts']], ['Dana Whitfield'])
        html = resp.content.decode()
        self.assertIn('ml-chips', html)
        self.assertIn('ml-chip-minor', html)

    # ---------------------------------------------------------------- a contact
    def test_the_contact_page_has_call_text_email_and_a_new_ticket_button(self):
        html = self.client.get(reverse('contact_detail', args=[self.board.pk])).content.decode()
        self.assertIn('href="tel:5615550110"', html)
        self.assertIn('href="sms:5615550110"', html)
        self.assertIn('href="mailto:dana@example.com"', html)
        self.assertIn(f'{reverse("ticket_create")}?contact={self.board.pk}', html)
        self.assertIn(reverse('contact_edit', args=[self.board.pk]), html)                 # the existing edit page is one big button away
        self.assertIn(f'href="{reverse("contact_list")}" class="ml-back"', html)
        self.assertIn('St Andrews Grand', html)

    def test_a_contact_with_no_phone_has_no_call_or_text_button(self):
        html = self.client.get(reverse('contact_detail', args=[self.nophone.pk])).content.decode()
        self.assertNotIn('href="tel:', html)
        self.assertNotIn('href="sms:', html)
        self.assertIn('href="mailto:info@quick.example.com"', html)
        self.assertIn('No open tickets.', html)

    def test_the_contact_page_lists_open_tickets_they_are_assigned_or_attached_to(self):
        mine = Ticket.objects.create(title='Gutter quote', property=self.grand, created_by=self.user, assigned_contact=self.vendor)
        linked = Ticket.objects.create(title='Roof inspection', property=self.palm, created_by=self.user)
        TicketContact.objects.create(ticket=linked, contact=self.vendor, role='contractor')
        Ticket.objects.create(title='Finished job', property=self.grand, created_by=self.user, assigned_contact=self.vendor, status=Ticket.Status.COMPLETED)
        html = self.client.get(reverse('contact_detail', args=[self.vendor.pk])).content.decode()
        self.assertIn('Gutter quote', html)
        self.assertIn('Roof inspection', html)
        self.assertNotIn('Finished job', html)
        self.assertIn('Open tickets (2)', html)
        self.assertIn(reverse('ticket_detail', args=[mine.pk]), html)

    def test_the_contact_page_needs_a_login_and_a_real_contact(self):
        self.assertEqual(self.client.get(reverse('contact_detail', args=[99999])).status_code, 404)
        self.client.logout()
        response = self.client.get(reverse('contact_detail', args=[self.board.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response['Location'])

    def test_the_edit_page_still_works_with_a_big_way_back_on_phones(self):
        html = self.client.get(reverse('contact_edit', args=[self.board.pk])).content.decode()
        self.assertIn('ml-form', html)
        self.assertIn(f'href="{reverse("contact_detail", args=[self.board.pk])}" class="ml-back d-md-none"', html)
        self.assertIn('d-none d-md-inline-flex', html)                                    # the small desktop link is still there
        response = self.client.post(reverse('contact_edit', args=[self.board.pk]), {'name': 'Dana W.', 'contact_type': 'board_member', 'phone': '561-555-0110', 'email': 'dana@example.com'})
        self.assertEqual(response.status_code, 302)
        self.board.refresh_from_db()
        self.assertEqual(self.board.name, 'Dana W.')

    def test_no_existing_address_changed(self):
        self.assertEqual(reverse('contact_edit', args=[7]), '/contacts/7/edit/')
        self.assertEqual(reverse('contact_list'), '/contacts/')
        self.assertEqual(reverse('property_detail', args=[7]), '/properties/7/')
        self.assertEqual(reverse('contact_detail', args=[7]), '/contacts/7/')
