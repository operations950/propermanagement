"""The property page on a phone: name and address, four big buttons, then only what matters for that KIND of property, and everything else under "More". The desktop page stays as it was."""
import re

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from tickets.models import Ticket

from . import trash as trash_service
from .models import Contact, Property, StaffProfile, Unit
from .templatetags import property_phone

STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}


def prop(name, kind, **kw):
    return Property.objects.create(name=name, property_type=kind, street=kw.pop('street', '1 Main St'), city='Boca Raton', state='FL', zip_code='33432', **kw)


def contact(p, name, kind, **kw):
    c = Contact.objects.create(name=name, contact_type=kind, **kw)
    c.properties.add(p)
    return c


def sections(html):
    """The phone sections of the page, in order, as their keys; and which of them start open."""
    found = re.findall(r'<section class="pp-sec( pp-open)?" id="pp-(\w+)"', html)
    return [key for _, key in found], [key for opened, key in found if opened]


def phone_block(html):
    """Just the phone screen's own markup (before the "More" group)."""
    return html.split('class="pp d-md-none"')[1].split('<div class="pp-more"')[0]


@override_settings(STORAGES=STORAGES)
class PropertyOnAPhone(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678')
        StaffProfile.objects.create(user=self.user, role='admin', is_company_admin=True)
        self.client.force_login(self.user)
        # a short-term rental with codes, an owner, a cleaner, a ticket, a size and a trash schedule
        self.str = prop('Palm Cottage 12', 'str', street='12 Palm Way', gate_code='4821', door_code='7733', wifi_network='PalmWifi', wifi_password='sun&sand', access_notes='Side gate, key under the pot.',
                        bedroom_count=3, bathroom_count=2, square_footage=1450)
        contact(self.str, 'Olivia Owner', 'owner', phone='(561) 555-0101', email='olivia@example.com')
        contact(self.str, 'Quick Cleaners', 'vendor', trade='Cleaning', phone='561-555-0102')
        contact(self.str, 'Some Guest', 'guest', phone='561-555-0103')
        Ticket.objects.create(title='Dishwasher is leaking', property=self.str, created_by=self.user, priority='urgent')
        trash_service.replace_schedule(self.str, [{'kind': 'trash', 'label': '', 'days': [0, 3]}, {'kind': 'recycling', 'label': '', 'days': [3]}])
        # an association with a gate code, a manager, a board member, units with owners and a ticket
        self.assoc = prop('St Andrews Grand', 'association', gate_code='9911')
        contact(self.assoc, 'Dana Whitfield', 'board_member', phone='(561) 555-0110')
        contact(self.assoc, 'Gus Gate', 'on_site_staff', phone='561-555-0111')
        contact(self.assoc, 'Unit Owner', 'owner', phone='561-555-0112')                          # owners are listed with their units, not as main contacts
        for label in ('101', '102'):
            Unit.objects.create(property=self.assoc, label=label)
        Ticket.objects.create(title='Gate keypad', property=self.assoc, created_by=self.user)
        # a long-term rental: tenant first, a door code, no tickets
        self.ltr = prop('Oak Duplex', 'ltr', door_code='5566')
        tenant = contact(self.ltr, 'Tina Tenant', 'tenant', phone='561-555-0120')
        contact(self.ltr, 'Oscar Owner', 'owner', phone='561-555-0121')
        unit = Unit.objects.create(property=self.ltr, label='A')
        tenant.units.add(unit)
        # a commercial property with only a tenant
        self.com = prop('Plaza Suite 4', 'commercial')
        contact(self.com, 'Cafe Tenant', 'tenant', phone='561-555-0130')

    def page(self, p):
        return self.client.get(reverse('property_detail', args=[p.pk])).content.decode()

    # ---------------------------------------------------------------- the top of the screen
    def test_the_top_is_the_name_the_address_in_maps_and_four_big_buttons_and_nothing_else(self):
        top = phone_block(self.page(self.str))
        self.assertIn('<h1 class="pp-name">Palm Cottage 12</h1>', top)
        self.assertIn('https://www.google.com/maps/search/?api=1&amp;query=12+Palm+Way', top)
        self.assertIn(f'{reverse("ticket_create")}?property={self.str.pk}', top)
        self.assertIn('Tickets (1)', top)
        self.assertIn('Directions', top)
        self.assertIn('Call Olivia Owner', top)
        for gone in ('ml-back', 'ml-tag', 'Short-Term Rentals', 'Accounting', 'Open all sections', 'USPS', 'Properties', reverse('property_edit', args=[self.str.pk]), reverse('accounting:hub', args=[self.str.pk])):
            self.assertNotIn(gone, top, gone)

    def test_the_page_has_no_back_to_properties_link_on_a_phone_because_the_top_bar_has_back(self):
        html = self.page(self.assoc)
        self.assertNotIn('class="ml-back"', html)
        self.assertNotIn('Open all sections', html)
        self.assertIn(f'<span id="mBackUrl" hidden>{reverse("property_list")}</span>', html)          # the bar's Back goes up to the list

    def test_no_open_tickets_means_no_open_tickets_button_and_no_empty_section(self):
        html = self.page(self.ltr)
        self.assertNotIn('Tickets (', phone_block(html))
        self.assertNotIn('id="pp-tickets"', html)
        self.assertIn('New ticket', phone_block(html))

    def test_no_phone_numbers_means_no_call_button(self):
        quiet = prop('Quiet Place', 'str')
        self.assertNotIn('Call ', phone_block(self.page(quiet)))

    # ---------------------------------------------------------------- by kind of property
    def test_a_short_term_rental_leads_with_access_then_contacts_tickets_size_and_trash(self):
        keys, opened = sections(self.page(self.str))
        self.assertEqual(keys, ['access', 'contacts', 'tickets', 'units', 'trash'])
        self.assertEqual(opened, ['access', 'contacts'])                                  # the first two start open

    def test_an_association_leads_with_the_gate_code_then_its_people_tickets_and_units_and_owners(self):
        html = self.page(self.assoc)
        keys, opened = sections(html)
        self.assertEqual(keys, ['access', 'contacts', 'tickets', 'units'])                # no trash schedule, so no trash section
        self.assertEqual(opened, ['access', 'contacts'])
        self.assertIn('Units and owners', html)

    def test_a_long_term_rental_leads_with_its_tenant_and_owner_then_access(self):
        keys, _ = sections(self.page(self.ltr))
        self.assertEqual(keys, ['contacts', 'access', 'units'])

    def test_a_property_with_only_a_contact_has_only_that_section(self):
        keys, opened = sections(self.page(self.com))
        self.assertEqual(keys, ['contacts'])
        self.assertEqual(opened, ['contacts'])

    # ---------------------------------------------------------------- what each section shows
    def test_codes_are_big_and_each_has_a_copy_button(self):
        html = self.page(self.str)
        for code in ('4821', '7733', 'PalmWifi', 'sun&amp;sand'):
            self.assertIn(f'<div class="pp-code-val">{code}</div>', html)
        self.assertIn('data-pp-copy="4821"', html)
        self.assertIn('data-pp-copy="sun&amp;sand"', html)
        self.assertIn('Side gate, key under the pot.', html)
        self.assertIn('Gate code', html)

    def test_main_contacts_are_the_ones_that_matter_for_the_kind_of_property_with_call_and_text(self):
        html = self.page(self.str)
        block = html.split('id="pp-contacts"')[1].split('</section>')[0]
        self.assertIn('Olivia Owner', block)
        self.assertIn('Quick Cleaners', block)
        self.assertNotIn('Some Guest', block)                                              # a guest is not a main contact of a rental
        self.assertIn('href="tel:5615550101"', block)
        self.assertIn('href="sms:5615550101"', block)
        self.assertIn('data-quo="openphone://dial?number=%2B15615550101&amp;action=call"', block)
        self.assertIn('Cleaning', block)
        assoc_block = self.page(self.assoc).split('id="pp-contacts"')[1].split('</section>')[0]
        self.assertIn('Dana Whitfield', assoc_block)
        self.assertIn('Gus Gate', assoc_block)
        self.assertNotIn('Unit Owner', assoc_block)                                        # owners are with their units

    def test_open_tickets_show_urgent_inline_with_a_link_to_all_of_them(self):
        html = self.page(self.str)
        block = html.split('id="pp-tickets"')[1].split('</section>')[0]
        self.assertIn('<span class="pp-urgent">Urgent</span> Dishwasher is leaking', block)
        self.assertIn(f'{reverse("ticket_list")}?property={self.str.pk}', block)

    def test_units_show_their_size_for_a_rental_the_owners_for_an_association_and_the_tenant_for_a_long_term_rental(self):
        size = self.page(self.str).split('id="pp-units"')[1].split('</section>')[0]
        self.assertIn('3 bd · 2 ba · 1,450 sq ft', size)
        ltr = self.page(self.ltr).split('id="pp-units"')[1].split('</section>')[0]
        self.assertIn('Tina Tenant', ltr)
        from accounting.models import Ownership
        owner = Contact.objects.create(name='Olga Owner', contact_type='owner')
        Ownership.objects.create(unit=Unit.objects.get(property=self.assoc, label='101'), person=owner, is_billed=True)
        assoc = self.page(self.assoc).split('id="pp-units"')[1].split('</section>')[0]
        self.assertIn('Olga Owner', assoc)
        self.assertIn('2 units', assoc)

    def test_trash_shows_each_pickup_and_its_days(self):
        block = self.page(self.str).split('id="pp-trash"')[1].split('</section>')[0]
        self.assertIn('Regular trash', block)
        self.assertIn('Mon and Thu', block)
        self.assertIn('Recycling', block)

    # ---------------------------------------------------------------- everything else is still there, under More; the desktop page is as it was
    def test_the_rest_of_the_page_is_under_more_and_every_card_and_form_is_still_on_the_page(self):
        html = self.page(self.str)
        self.assertIn('data-pp-more-toggle', html)
        after = html.split('data-pp-more-body')[1]
        for anchor in ('id="building"', 'id="contacts"', 'id="tickets"', 'id="trash"', 'id="faq"', 'id="supplies"', 'name="action" value="save_access_info"', 'data-m-acc-none'):
            self.assertIn(anchor, after, anchor)
        self.assertIn('css/mobile-property.css', html)
        self.assertIn('js/mobile-property.js', html)
        self.assertIn('js/quo-links.js', html)

    def test_every_jump_link_goes_to_something_on_the_page(self):
        for p in (self.str, self.assoc, self.ltr, self.com):
            html = self.page(p)
            for href in re.findall(r'href="#([\w-]+)" data-pp-jump', html):
                self.assertIn(f'id="{href}"', html, (p.name, href))

    def test_the_desktop_header_with_accounting_and_edit_is_unchanged(self):
        html = self.page(self.assoc)
        head = html.split('class="my-3 d-none d-md-block"')[1].split('class="pp d-md-none"')[0]
        self.assertIn('Properties</a>', head)
        self.assertIn(reverse('property_edit', args=[self.assoc.pk]), head)
        self.assertIn('Accounting', head)
        self.assertIn('Associations', head)                                                # the type is still told on a computer
        self.assertIn('d-none d-md-flex flex-wrap gap-2 mb-3 small" aria-label="Sections of this property"', html)

    def test_the_section_tag_lists_only_what_has_data(self):
        class Fake(dict):
            pass
        bare = prop('Bare Place', 'ltr')
        out = property_phone.property_phone({'property': bare, 'units': [], 'contact_groups': [], 'open_tickets': [], 'trash': {}})
        self.assertEqual(out['sections'], [])
        self.assertFalse(out['has_tickets'])

    def test_size_text_uses_the_unit_then_falls_back_to_the_property(self):
        p = prop('Sized', 'str', bedroom_count=2, bathroom_count=1.5, square_footage=900)
        u = Unit.objects.create(property=p, label='B', bedroom_count=1)
        self.assertEqual(property_phone._size(u, p), '1 bd · 1.5 ba · 900 sq ft')
        self.assertEqual(property_phone._size(Unit(label='C'), None), '')
