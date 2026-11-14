from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import Property, StaffProfile

from .models import Priority, Ticket, TicketStatusNote

STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}


def make_ticket(who, title, **kw):
    """A ticket; unless the test says who has it, it belongs to a colleague (every ticket needs exactly one assignee)."""
    kw.setdefault('status', Ticket.Status.OPEN)
    kw.setdefault('priority', Priority.MEDIUM)
    kw.setdefault('assigned_role', StaffProfile.Role.PROPERTY_MANAGER)
    if 'assigned_staff' not in kw and 'assigned_contact' not in kw:
        kw['assigned_staff'] = StaffProfile.objects.exclude(user=who).first()
    return Ticket.objects.create(title=title, created_by=who, source=Ticket.Source.MANUAL, **kw)


@override_settings(STORAGES=STORAGES)
class PhoneTicketListTests(TestCase):
    """On a phone the Tickets list is a big New ticket button, one-tap filter chips, a search box and big cards. Every chip is a filter the full list already had
    (or the priority filter), and the full table, header and filter box are exactly where they were."""

    def setUp(self):
        User = get_user_model()
        self.me = User.objects.create_user('justin', password='pw12345678', first_name='Justin', last_name='Browne')
        self.staff = StaffProfile.objects.create(user=self.me, role=StaffProfile.Role.PROPERTY_MANAGER)
        StaffProfile.objects.create(user=User.objects.create_user('gia', password='pw12345678', first_name='Gia', last_name='Isolano'), role=StaffProfile.Role.PROPERTY_MANAGER)
        self.prop = Property.objects.create(name='Grand Condos', property_type=Property.Type.ASSOCIATION, is_active=True)
        self.other = Property.objects.create(name='Pine Villas', property_type=Property.Type.ASSOCIATION, is_active=True)
        now = timezone.now()
        self.urgent = make_ticket(self.me, 'Roof leak over the laundry', property=self.prop, priority=Priority.URGENT, due_date=now - timedelta(days=3), assigned_staff=self.staff)
        self.high = make_ticket(self.me, 'Gate code not working', property=self.other, priority=Priority.HIGH, due_date=now + timedelta(days=2))
        self.progress = make_ticket(self.me, 'Pool pump noise', property=self.prop, status=Ticket.Status.IN_PROGRESS)
        self.done = make_ticket(self.me, 'Replace bulbs', property=self.prop, status=Ticket.Status.COMPLETED)
        TicketStatusNote.objects.create(ticket=self.urgent, body='Called the roofer', created_by=self.me)
        self.client.force_login(self.me)
        self.url = reverse('ticket_list')

    def titles(self, response):
        return {t.title for t in response.context['tickets']}

    def test_the_phone_header_has_a_big_new_ticket_button_the_chips_a_search_box_and_more_filters(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn('class="m-tk d-md-none"', html)
        self.assertIn(f'class="m-tk-new" href="{reverse("ticket_create")}"', html)
        for label in ('Mine', 'Open', 'Urgent', 'Due today', 'Late', 'In progress', 'Done'):
            self.assertIn(f'>{label}</a>', html)
        self.assertIn('class="m-tk-search"', html)
        self.assertIn('data-m-filters-open', html)

    def test_each_chip_is_a_filter_and_tapping_a_chip_that_is_on_clears_it(self):
        chips = {c['label']: c for c in self.client.get(self.url).context['phone_chips']}
        self.assertEqual(chips['Urgent']['url'], '?priority=urgent')
        self.assertEqual(chips['Late']['url'], '?due=overdue')
        self.assertEqual(chips['Due today']['url'], '?due=today')
        self.assertEqual(chips['In progress']['url'], '?status=in_progress')
        self.assertEqual(chips['Done']['url'], '?status=complete')
        self.assertEqual(chips['Mine']['url'], f'?assigned_staff={self.staff.pk}')
        self.assertTrue(chips['Open']['on'])
        on = {c['label']: c for c in self.client.get(self.url + '?priority=urgent&due=overdue').context['phone_chips']}
        self.assertTrue(on['Urgent']['on'] and on['Late']['on'])
        self.assertEqual(on['Urgent']['url'], '?due=overdue')                  # off again: the rest of the filters stay
        self.assertEqual(on['Late']['url'], '?priority=urgent')

    def test_the_chips_show_what_the_list_shows(self):
        self.assertEqual(self.titles(self.client.get(self.url + '?priority=urgent')), {'Roof leak over the laundry'})
        self.assertEqual(self.titles(self.client.get(self.url + '?due=overdue')), {'Roof leak over the laundry'})
        self.assertEqual(self.titles(self.client.get(self.url + '?status=in_progress')), {'Pool pump noise'})
        self.assertEqual(self.titles(self.client.get(self.url + '?status=complete')), {'Replace bulbs'})
        self.assertEqual(self.titles(self.client.get(self.url + f'?assigned_staff={self.staff.pk}')), {'Roof leak over the laundry'})
        self.assertEqual(self.titles(self.client.get(self.url)), {'Roof leak over the laundry', 'Gate code not working', 'Pool pump noise'})     # Open

    def test_a_bad_priority_is_ignored_and_a_chip_keeps_the_other_filters_but_starts_page_one(self):
        self.assertEqual(len(self.titles(self.client.get(self.url + '?priority=nonsense'))), 3)
        chips = {c['label']: c for c in self.client.get(self.url + f'?property={self.prop.pk}&page=2&q=pump').context['phone_chips']}
        self.assertIn(f'property={self.prop.pk}', chips['Urgent']['url'])
        self.assertIn('q=pump', chips['Urgent']['url'])
        self.assertNotIn('page=', chips['Urgent']['url'])

    def test_somebody_without_a_staff_profile_gets_no_mine_chip(self):
        stranger = get_user_model().objects.create_user('nostaff', password='pw12345678')
        self.client.force_login(stranger)
        labels = [c['label'] for c in self.client.get(self.url).context['phone_chips']]
        self.assertNotIn('Mine', labels)
        self.assertIn('Urgent', labels)

    def test_more_filters_counts_only_the_filters_that_are_not_chips(self):
        self.assertEqual(self.client.get(self.url).context['phone_more_count'], 0)
        self.assertEqual(self.client.get(self.url + '?priority=urgent&due=overdue&status=in_progress').context['phone_more_count'], 0)
        self.assertEqual(self.client.get(self.url + f'?property={self.prop.pk}&role=maintenance').context['phone_more_count'], 2)

    def test_a_card_is_one_big_link_to_the_ticket_with_the_things_that_matter(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn(f'<a class="m-tk-card is-urgent is-late" href="{reverse("ticket_detail", args=[self.urgent.pk])}">', html)
        for text in ('Roof leak over the laundry', 'Grand Condos', 'Justin Browne', 'Late', 'Called the roofer', 'Urgent'):
            self.assertIn(text, html)
        self.assertIn('is-high', html)

    def test_an_empty_list_says_so_and_offers_a_way_back(self):
        html = self.client.get(self.url + '?q=zzzznothing').content.decode()
        self.assertIn('No tickets here', html)
        self.assertIn('Show all open tickets', html)

    def test_the_search_form_keeps_the_filters_and_the_ajax_search_still_answers_with_the_phone_cards(self):
        html = self.client.get(self.url + '?priority=high&q=gate').content.decode()
        self.assertIn('<input type="hidden" name="priority" value="high">', html)
        self.assertNotIn('<input type="hidden" name="q"', html)
        data = self.client.get(self.url + '?q=gate', HTTP_X_REQUESTED_WITH='XMLHttpRequest').json()
        self.assertIn('m-tk-card', data['mobile'])
        self.assertIn('Gate code not working', data['mobile'])

    def test_the_full_screen_is_still_there_for_wide_screens(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn('id="ticket-table"', html)
        self.assertIn('id="tf-box"', html)
        self.assertIn('id="ticket-search-input"', html)
        self.assertIn('d-none d-md-flex justify-content-between align-items-center flex-wrap gap-2 my-3', html)          # the wide header: hidden on phones only
        self.assertIn('d-none d-md-block card p-0', html)                                                                   # the table: wide screens only

    def test_the_filter_memory_still_works_with_chips(self):
        self.client.get(self.url + '?priority=urgent')
        self.assertEqual(self.client.session['ticket_list_qs'], 'priority=urgent')
        response = self.client.get(reverse('ticket_detail', args=[self.urgent.pk]))
        self.assertEqual(response.context['back_url'], self.url + '?priority=urgent')


@override_settings(STORAGES=STORAGES)
class PhoneTicketPageAndFormTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.me = User.objects.create_user('justin', password='pw12345678', first_name='Justin', last_name='Browne')
        self.staff = StaffProfile.objects.create(user=self.me, role=StaffProfile.Role.PROPERTY_MANAGER)
        StaffProfile.objects.create(user=User.objects.create_user('gia', password='pw12345678', first_name='Gia', last_name='Isolano'), role=StaffProfile.Role.PROPERTY_MANAGER)
        self.prop = Property.objects.create(name='Grand Condos', property_type=Property.Type.ASSOCIATION, is_active=True)
        self.ticket = make_ticket(self.me, 'Roof leak over the laundry', property=self.prop, priority=Priority.URGENT, due_date=timezone.now() - timedelta(days=1), assigned_staff=self.staff)
        self.client.force_login(self.me)

    def test_the_ticket_page_has_the_essentials_and_the_big_action_buttons_for_phones_and_keeps_the_wide_header(self):
        html = self.client.get(reverse('ticket_detail', args=[self.ticket.pk])).content.decode()
        self.assertIn('class="m-td d-md-none"', html)
        self.assertIn('Roof leak over the laundry', html)
        for goto in ('note', 'status', 'Internal Reassign', 'Mark Complete', 'Documents'):
            self.assertIn(f'data-m-goto="{goto}"', html)
        self.assertIn('Late', html)
        self.assertIn('d-none d-md-flex justify-content-between align-items-start my-3', html)                                # the wide header is untouched
        self.assertIn('mobile-tickets.js', html)

    def test_the_phone_buttons_point_at_sections_that_exist_on_the_page(self):
        html = self.client.get(reverse('ticket_detail', args=[self.ticket.pk])).content.decode()
        for title in ('Update status', 'Internal Reassign', 'Mark Complete', 'Documents'):
            self.assertIn(title, html)

    def test_the_new_ticket_form_keeps_every_field_and_adds_the_phone_helpers(self):
        other = Property.objects.create(name='Pine Villas', property_type=Property.Type.ASSOCIATION, is_active=True)
        make_ticket(self.me, 'Older one', property=other)
        response = self.client.get(reverse('ticket_create'))
        html = response.content.decode()
        for field in ('name="title"', 'name="description"', 'name="property"', 'name="priority"', 'name="due_date"', 'name="assigned_role"', 'name="assigned_staff"',
                      'name="assigned_contact"', 'name="reporter_contact"'):
            self.assertIn(field, html)
        self.assertIn('data-m-me', html)                                                                   # "Assign to me"
        self.assertIn('m-submitbar', html)
        self.assertIn('id="m-more-fields"', html)
        names = [p['name'] for p in response.context['recent_properties']]
        self.assertEqual(set(names), {'Grand Condos', 'Pine Villas'})
        self.assertEqual(response.context['my_staff_id'], self.staff.pk)

    def test_recent_properties_put_the_most_used_first(self):
        other = Property.objects.create(name='Pine Villas', property_type=Property.Type.ASSOCIATION, is_active=True)
        for i in range(3):
            make_ticket(self.me, f'Pine {i}', property=other)
        names = [p['name'] for p in self.client.get(reverse('ticket_create')).context['recent_properties']]
        self.assertEqual(names[0], 'Pine Villas')

    def test_creating_a_ticket_still_posts_to_the_same_place_and_lands_on_the_ticket(self):
        response = self.client.post(reverse('ticket_create'), {'title': 'Broken gate arm', 'description': '', 'property': self.prop.pk, 'priority': 'high', 'due_date': '',
                                                               'assigned_role': 'maintenance', 'assigned_staff': self.staff.pk})
        ticket = Ticket.objects.get(title='Broken gate arm')
        self.assertRedirects(response, reverse('ticket_detail', args=[ticket.pk]))
        self.assertEqual((ticket.priority, ticket.assigned_staff_id, ticket.property_id), ('high', self.staff.pk, self.prop.pk))
        bad = self.client.post(reverse('ticket_create'), {'title': '', 'assigned_role': ''})
        self.assertEqual(bad.status_code, 200)                                                             # errors show on the same form
