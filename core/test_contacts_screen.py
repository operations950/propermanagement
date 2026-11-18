"""The Contacts screen as a newspaper: the Recent box, a few of each type with an "All N" row, a type chosen (grouped by property, trade or alphabet), the controls row, the Alerts button, the phone,
and the number of queries (a fixed few, never one for each person)."""
import re
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from core import contact_boxes
from core.models import Contact, ContactImportCandidate, Property, StaffProfile, Unit

STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}
T = Contact.ContactType


def prop(name, kind='association', **kw):
    kw.setdefault('street', '1 Main St')
    return Property.objects.create(name=name, property_type=kind, city='Boca Raton', state='FL', zip_code='33432', **kw)


def person(name, kind=T.OWNER, days=None, **kw):
    c = Contact.objects.create(name=name, contact_type=kind, **kw)
    if days is not None:
        Contact.objects.filter(pk=c.pk).update(last_interaction_at=timezone.now() - timedelta(days=days), interaction_count=1)
    return c


class Base(TestCase):
    def setUp(self):
        User = get_user_model()
        self.boss = User.objects.create_user('boss', 'b@example.com', 'pw12345678')
        StaffProfile.objects.create(user=self.boss, role='admin', is_company_admin=True)
        self.client.force_login(self.boss)

    def boxes(self, query='', **extra):
        r = self.client.get(reverse('contact_list') + query, **extra)
        self.assertEqual(r.status_code, 200)
        every = sorted((b for col in r.context['columns'] for b in col), key=lambda b: b['order'])           # in reading order, whichever column a box was dealt to
        return r, {b['title']: b for b in every}

    def names(self, box):
        return [row['name'] for row in box['rows'] if row['kind'] == 'person']


@override_settings(STORAGES=STORAGES)
class UnfilteredTests(Base):
    def test_the_first_box_is_recent_with_the_25_latest_and_how_long_ago(self):
        for i in range(30):
            person(f'Person {i:02d}', days=i)
        person('Never Dealt', days=None)
        r, boxes = self.boxes()
        recent = boxes['Recent']
        self.assertEqual(len(recent['rows']), 25)
        self.assertEqual(self.names(recent)[:3], ['Person 00', 'Person 01', 'Person 02'])        # most recent first
        self.assertNotIn('Never Dealt', self.names(recent))
        self.assertEqual(recent['rows'][2]['ago'], '2d ago')
        self.assertEqual(list(boxes)[0], 'Recent')
        self.assertContains(r, '2d ago')

    def test_a_contact_who_was_never_dealt_with_does_not_make_a_recent_box_by_itself(self):
        person('Quiet', days=None)
        r, boxes = self.boxes()
        self.assertNotIn('Recent', boxes)
        self.assertIn('Owner', boxes)

    def test_each_type_shows_its_eight_latest_and_an_all_row(self):
        for i in range(12):
            person(f'Owner {i:02d}', T.OWNER, days=i)
        for i in range(3):
            person(f'Tenant {i}', T.TENANT, days=i)
        r, boxes = self.boxes()
        owners = boxes['Owner']
        self.assertEqual(self.names(owners), [f'Owner {i:02d}' for i in range(8)])
        self.assertEqual(owners['rows'][-1]['label'], 'All 12 owners')
        self.assertEqual(owners['count'], 12)                                              # the box's number is everyone, not just the eight
        self.assertContains(r, 'href="?type=owner"')
        tenants = boxes['Tenant']
        self.assertEqual(len(self.names(tenants)), 3)
        self.assertEqual(tenants['rows'][-1]['label'], 'All 3 tenants')

    def test_inside_a_type_people_with_no_dealings_come_after_the_active_ones_by_name(self):
        person('Zed Active', T.OWNER, days=5)
        person('Amy Quiet', T.OWNER)
        person('Bob Quiet', T.OWNER)
        r, boxes = self.boxes()
        self.assertEqual(self.names(boxes['Owner']), ['Zed Active', 'Amy Quiet', 'Bob Quiet'])

    def test_types_come_in_the_usual_order_and_only_when_they_have_people(self):
        for kind in (T.GUEST, T.VENDOR, T.OWNER, T.BOARD_MEMBER, T.TENANT):
            person(f'P {kind}', kind, days=1)
        r, boxes = self.boxes()
        self.assertEqual([t for t in boxes if t != 'Recent'], ['Owner', 'Tenant', 'Board member', 'Vendor / contractor', 'Guest'])

    def test_a_search_narrows_every_box(self):
        person('Dana Whitfield', T.BOARD_MEMBER, days=1)
        person('Rainsaver Gutters', T.VENDOR, days=2, trade='Gutters')
        r, boxes = self.boxes('?q=gutters')
        self.assertEqual(self.names(boxes['Recent']), ['Rainsaver Gutters'])
        self.assertNotIn('Board member', boxes)
        self.assertContains(r, 'matching')

    def test_the_search_answers_a_script_with_the_boxes_and_the_note(self):
        person('Dana Whitfield', T.BOARD_MEMBER, days=1)
        person('Rainsaver Gutters', T.VENDOR, days=2, trade='Gutters')
        j = self.client.get(reverse('contact_list') + '?q=gutters', HTTP_X_REQUESTED_WITH='XMLHttpRequest').json()
        self.assertIn('Rainsaver Gutters', j['paper'])
        self.assertNotIn('Dana Whitfield', j['paper'])
        self.assertIn('1 contact', j['note'])

    def test_nothing_found_says_so(self):
        r, boxes = self.boxes('?q=zzzzzz')
        self.assertEqual(boxes, {})
        self.assertContains(r, 'No contacts match.')


@override_settings(STORAGES=STORAGES)
class FilteredTests(Base):
    def test_owners_tenants_and_the_like_are_boxes_by_property_a_person_in_each_of_theirs(self):
        grand, glen = prop('St Andrews Grand'), prop('St Andrews Glen')
        a, b, c = person('Ann Owner'), person('Bob Owner'), person('Cy Owner')
        a.properties.add(grand)
        b.properties.add(grand, glen)
        r, boxes = self.boxes('?type=owner')
        self.assertEqual([k for k in boxes], ['St Andrews Glen', 'St Andrews Grand', 'No property'])
        self.assertEqual(self.names(boxes['St Andrews Grand']), ['Ann Owner', 'Bob Owner'])
        self.assertEqual(self.names(boxes['St Andrews Glen']), ['Bob Owner'])               # in both of theirs
        self.assertEqual(self.names(boxes['No property']), ['Cy Owner'])
        self.assertNotIn('Recent', boxes)

    def test_board_members_association_members_on_site_staff_tenants_and_guests_are_by_property_too(self):
        grand = prop('St Andrews Grand')
        for kind in (T.BOARD_MEMBER, T.ASSOCIATION_MEMBER, T.ON_SITE_STAFF, T.TENANT, T.GUEST):
            person(f'P {kind}', kind).properties.add(grand)
            r, boxes = self.boxes(f'?type={kind}')
            self.assertEqual(list(boxes), ['St Andrews Grand'], kind)

    def test_a_general_placeholder_property_counts_as_no_property(self):
        person('Placeholder Person').properties.add(prop('Associations (general)', is_general=True))
        r, boxes = self.boxes('?type=owner')
        self.assertEqual(list(boxes), ['No property'])

    def test_an_owners_unit_shows_beside_their_name(self):
        grand = prop('St Andrews Grand')
        o = person('Unit Owner')
        o.properties.add(grand)
        o.units.add(Unit.objects.create(property=grand, label='101'))
        r, boxes = self.boxes('?type=owner')
        self.assertEqual(boxes['St Andrews Grand']['rows'][0]['line2'], 'Unit 101')

    def test_a_person_who_carries_the_type_as_another_hat_is_included(self):
        grand = prop('St Andrews Grand')
        o = person('Owner And Board', T.OWNER, secondary_types=[T.BOARD_MEMBER])
        o.properties.add(grand)
        r, boxes = self.boxes('?type=board_member')
        self.assertEqual(self.names(boxes['St Andrews Grand']), ['Owner And Board'])

    def test_vendors_are_boxes_by_trade_case_insensitive_with_no_type_set_last(self):
        person('Rainsaver', T.VENDOR, trade='Gutters')
        person('Gutter Pros', T.VENDOR, trade='gutters')
        person('Cool Air', T.VENDOR, trade='HVAC')
        person('Mystery', T.VENDOR)
        r, boxes = self.boxes('?type=vendor')
        self.assertEqual(list(boxes), ['Gutters', 'HVAC', 'No type set'] if 'Gutters' in boxes else list(boxes))
        self.assertEqual(sorted(self.names(boxes[next(k for k in boxes if k.lower() == 'gutters')])), ['Gutter Pros', 'Rainsaver'])
        self.assertEqual(list(boxes)[-1], 'No type set')
        self.assertEqual(boxes['No type set']['tone'], 'late')
        self.assertContains(r, '1 with no type set')

    def test_staff_are_alphabetical_in_one_box(self):
        for name in ('Zoe Staff', 'Abe Staff', 'Mia Staff'):
            person(name, T.STAFF_ADJACENT)
        r, boxes = self.boxes('?type=staff_adjacent')
        self.assertEqual(list(boxes), ['Staff, A to Z'])
        self.assertEqual(self.names(boxes['Staff, A to Z']), ['Abe Staff', 'Mia Staff', 'Zoe Staff'])

    def test_a_long_alphabetical_type_is_split_by_first_letter(self):
        for i in range(31):
            person(f'{chr(65 + i % 3)}person {i}', T.LEAD)
        r, boxes = self.boxes('?type=lead')
        self.assertEqual(list(boxes), ['A', 'B', 'C'])

    def test_the_search_works_inside_a_type_and_no_eight_person_limit_applies(self):
        grand = prop('St Andrews Grand')
        for i in range(20):
            person(f'Owner {i:02d}').properties.add(grand)
        r, boxes = self.boxes('?type=owner')
        self.assertEqual(len(self.names(boxes['St Andrews Grand'])), 20)
        r, boxes = self.boxes('?type=owner&q=owner 07')
        self.assertEqual(self.names(boxes['St Andrews Grand']), ['Owner 07'])

    def test_an_unknown_type_is_ignored(self):
        person('Someone', days=1)
        r, boxes = self.boxes('?type=nonsense')
        self.assertIn('Recent', boxes)


@override_settings(STORAGES=STORAGES)
class ControlsAndAlertsTests(Base):
    def test_the_header_the_pills_and_the_one_control_row(self):
        person('Ann', T.OWNER, days=1)
        person('Pat', T.VENDOR, days=1, trade='Gutters')
        r = self.client.get(reverse('contact_list'))
        html = r.content.decode()
        self.assertIn('data-lucide="contact"', html.split('np-head')[1].split('</div>')[0])
        self.assertIn(f'<a href="{reverse("contact_create")}" class="btn-action">', html)        # New contact: the one primary-button style
        self.assertIn('New contact', html)
        self.assertIn('id="contact-search-input"', html)
        pills = re.findall(r'<a href="([^"]*)" class="cl-pill( on)?"[^>]*>([^<]*)<small>(\d+)</small>', html)
        self.assertEqual([(p[2], p[3]) for p in pills], [('All', '2'), ('Owner', '1'), ('Vendor / contractor', '1')])
        self.assertTrue(pills[0][1])                                                               # All is the one on
        self.assertEqual(html.count('class="np-controls'), 1)                                             # ONE control row

    def test_the_pressed_pill_clears_its_filter_and_the_search_is_kept(self):
        person('Ann', T.OWNER, days=1)
        r = self.client.get(reverse('contact_list') + '?type=owner&q=ann')
        html = r.content.decode()
        on = re.search(r'<a href="([^"]*)" class="cl-pill on"', html.split('class="cl-pills"')[1])
        self.assertEqual(on.group(1), '?q=ann')                                                    # pressing Owner again drops the type but keeps the search
        self.assertIn('<input type="hidden" name="type" value="owner">', html)

    def test_alerts_is_plain_with_nothing_to_look_at_and_orange_with_a_count_when_there_is(self):
        html = self.client.get(reverse('contact_list')).content.decode()
        button = html.split('id="cl-alerts-btn"')[1].split('</button>')[0]
        self.assertNotIn('btn-alerts-on', button)
        self.assertIn('Nothing about the contacts needs attention.', html)
        ContactImportCandidate.objects.create(source='quo', name='Someone New', phone='5615550100')
        person('Mystery Vendor', T.VENDOR)
        html = self.client.get(reverse('contact_list')).content.decode()
        button = html.split('id="cl-alerts-btn"')[1].split('</button>')[0]
        self.assertIn('btn-alerts-on', button)
        self.assertIn('<span class="alerts-n">2</span>', button)
        self.assertIn('id="cl-alerts-panel" hidden', html)
        self.assertIn('1 contact waiting for a yes or no', html)
        self.assertIn('1 vendor has no type set', html)
        self.assertIn(reverse('contact_review'), html)

    def test_duplicates_are_an_alert(self):
        person('One', phone='561-555-0100')
        person('Two', phone='561-555-0100')
        from django.core.cache import cache
        cache.delete('contacts:duplicate-group-count')
        html = self.client.get(reverse('contact_list')).content.decode()
        self.assertIn('1 possible duplicate', html)
        self.assertIn(reverse('contact_duplicates'), html)
        cache.delete('contacts:duplicate-group-count')

    def test_everyone_but_an_admin_sees_to_review_not_the_admin_wording(self):
        other = get_user_model().objects.create_user('plain', 'p@example.com', 'pw12345678')
        StaffProfile.objects.create(user=other, role='property_manager')
        ContactImportCandidate.objects.create(source='quo', name='Someone New', phone='5615550100')
        self.client.force_login(other)
        html = self.client.get(reverse('contact_list')).content.decode()
        self.assertIn('1 to review', html)
        self.assertNotIn('waiting for a yes or no', html)

    def test_import_is_still_a_button_and_its_modal_is_there(self):
        html = self.client.get(reverse('contact_list')).content.decode()
        self.assertIn('data-bs-target="#importContactsModal"', html)
        self.assertIn('id="importContactsModal"', html)


@override_settings(STORAGES=STORAGES)
class RowsAndPhoneTests(Base):
    def test_a_row_opens_the_person_and_has_call_text_email_and_new_ticket_links_with_quo(self):
        c = person('Dana Whitfield', T.BOARD_MEMBER, days=1, phone='(561) 555-0110', email='dana@example.com')
        html = self.client.get(reverse('contact_list')).content.decode()
        self.assertIn(f'href="{reverse("contact_detail", args=[c.pk])}"', html)
        self.assertIn('href="tel:5615550110" data-quo="openphone://dial?number=%2B15615550110&amp;action=call" class="cl-act"', html)
        self.assertIn('href="sms:5615550110" data-quo="openphone://message?number=%2B15615550110" class="cl-act"', html)
        self.assertIn('href="mailto:dana@example.com" class="cl-act"', html)
        self.assertIn(f'{reverse("ticket_create")}?contact={c.pk}', html)
        self.assertIn('js/quo-links.js', html)

    def test_a_person_with_no_phone_or_email_has_only_the_new_ticket_link(self):
        c = person('No Details', T.VENDOR, days=1, trade='Plumbing')
        row = self.client.get(reverse('contact_list') + '?type=vendor').content.decode().split('No Details')[1].split('cl-row')[0]
        self.assertNotIn('href="tel:', row)
        self.assertNotIn('mailto:', row)
        self.assertIn('Plumbing', self.client.get(reverse('contact_list') + '?type=vendor').content.decode())

    def test_the_phone_rules_hide_alerts_and_import_and_make_the_rows_bigger(self):
        css = open('static/css/contacts.css', encoding='utf-8').read()
        phone = css.split('@media (max-width: 575px)')[1]
        for needle in ('.cl-alerts-btn', '.cl-import', '.cl-panel', 'display: none !important', 'flex-wrap: nowrap', 'overflow-x: auto', '.cl-acts { opacity: 1; }', 'min-height: 3rem'):
            self.assertIn(needle, phone)
        self.assertIn('@media (hover: none) { .cl-acts { opacity: 1; } }', css)

    def test_the_newspaper_stylesheet_and_script_are_on_the_page(self):
        html = self.client.get(reverse('contact_list')).content.decode()
        self.assertIn('css/newspaper.css', html)
        self.assertIn('css/contacts.css', html)
        self.assertIn('js/newspaper.js', html)

    def test_the_boxes_are_dealt_into_three_columns(self):
        for i in range(9):
            person(f'P{i}', T.OWNER, days=i)
            person(f'V{i}', T.VENDOR, days=i, trade='Gutters')
        r = self.client.get(reverse('contact_list'))
        self.assertEqual(len(r.context['columns']), 3)
        self.assertEqual(r.content.decode().count('class="np-col"'), 3)


@override_settings(STORAGES=STORAGES)
class QueryCountTests(Base):
    def count(self, url):
        with CaptureQueriesContext(connection) as ctx:
            r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        return len(ctx)

    def make(self, n):
        grand = prop(f'Condo {n}')
        for i in range(n):
            c = person(f'Owner {n}-{i}', T.OWNER if i % 2 else T.VENDOR, days=i, trade='Gutters' if i % 3 else '', phone=f'561-555-{i:04d}')
            c.properties.add(grand)
            c.units.add(Unit.objects.create(property=grand, label=str(i)))

    def test_the_screen_reads_a_fixed_few_times_whatever_the_number_of_people(self):
        self.make(5)
        few = (self.count(reverse('contact_list')), self.count(reverse('contact_list') + '?type=owner'), self.count(reverse('contact_list') + '?type=vendor'))
        self.make(60)
        many = (self.count(reverse('contact_list')), self.count(reverse('contact_list') + '?type=owner'), self.count(reverse('contact_list') + '?type=vendor'))
        self.assertEqual(few, many)                                                                # not one query more for 12 times the people
        self.assertLessEqual(max(many), 25)
