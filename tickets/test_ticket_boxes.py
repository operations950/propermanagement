"""The Tickets newspaper: every ticket in exactly one box by when it is due (Urgent, Overdue, Today, Tomorrow, This week through Saturday, Next week Sunday-Saturday, In 2 weeks ... , No due date)."""
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from core.models import Property, StaffProfile

from .boxes import bucket_of, ticket_boxes, week_start
from .models import Priority, Ticket, TicketClosingNote, TicketStatusNote

DONE = [Ticket.Status.COMPLETED, Ticket.Status.VERIFIED, Ticket.Status.CANCELLED, Ticket.Status.SKIPPED, Ticket.Status.NOT_APPLICABLE]
STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}
WED = date(2026, 11, 18)          # a Wednesday; its week runs Sunday Nov 15 to Saturday Nov 21


def at(day):
    return timezone.make_aware(datetime.combine(day, time(12)))


def tk(title, due=None, priority=Priority.MEDIUM, status=Ticket.Status.OPEN, completed=None):
    return SimpleNamespace(title=title, due_date=at(due) if due else None, priority=priority, status=status, completed_at=at(completed) if completed else None, created_at=at(date(2026, 1, 5)))


def titles(boxes):
    return [b['title'] for b in boxes]


class WeeksRunSundayToSaturdayTests(SimpleTestCase):
    def test_week_start_is_the_sunday_on_or_before(self):
        self.assertEqual(week_start(date(2026, 11, 15)), date(2026, 11, 15))          # a Sunday
        self.assertEqual(week_start(date(2026, 11, 16)), date(2026, 11, 15))          # Monday
        self.assertEqual(week_start(date(2026, 11, 21)), date(2026, 11, 15))          # Saturday
        self.assertEqual(week_start(date(2026, 11, 22)), date(2026, 11, 22))          # the next Sunday begins a new week


class TheBoxesTests(SimpleTestCase):
    def boxes(self, tickets, today=WED, **kw):
        return ticket_boxes(tickets, today, done_statuses=DONE, **kw)

    def test_the_boxes_come_in_order_and_each_ticket_is_in_exactly_one(self):
        tickets = [tk('urgent late', date(2026, 11, 10), Priority.URGENT), tk('overdue', date(2026, 11, 10)), tk('today', WED), tk('tomorrow', date(2026, 11, 19)),
                   tk('fri', date(2026, 11, 20)), tk('sat', date(2026, 11, 21)), tk('next sun', date(2026, 11, 22)), tk('next sat', date(2026, 11, 28)),
                   tk('two weeks a', date(2026, 11, 29)), tk('two weeks b', date(2026, 12, 5)), tk('three weeks', date(2026, 12, 6)), tk('four weeks', date(2026, 12, 13)), tk('no date')]
        boxes = self.boxes(tickets)
        self.assertEqual(titles(boxes), ['Urgent', 'Overdue', 'Today', 'Tomorrow', 'This week', 'Next week', 'In 2 weeks', 'In 3 weeks', 'In 4 weeks', 'No due date'])
        rows = [t.title for b in boxes for t in b['rows']]
        self.assertEqual(sorted(rows), sorted(t.title for t in tickets))                       # every ticket once
        self.assertEqual(len(rows), len(set(rows)))
        by = {b['title']: [t.title for t in b['rows']] for b in boxes}
        self.assertEqual(by['Urgent'], ['urgent late'])                                         # Urgent wins over its (late) date
        self.assertEqual(by['This week'], ['fri', 'sat'])                                       # after tomorrow, through Saturday
        self.assertEqual(by['Next week'], ['next sun', 'next sat'])                             # Sunday through Saturday
        self.assertEqual(by['In 2 weeks'], ['two weeks a', 'two weeks b'])                      # across the month end
        self.assertEqual(by['In 3 weeks'], ['three weeks'])

    def test_each_week_box_says_its_dates(self):
        boxes = {b['title']: b for b in self.boxes([tk('a', date(2026, 11, 20)), tk('b', date(2026, 11, 22)), tk('c', date(2026, 11, 30))])}
        self.assertEqual(boxes['This week']['hint'], 'Nov 15 – Nov 21')
        self.assertEqual(boxes['Next week']['hint'], 'Nov 22 – Nov 28')
        self.assertEqual(boxes['In 2 weeks']['hint'], 'Nov 29 – Dec 5')

    def test_only_the_boxes_that_have_tickets_are_made(self):
        self.assertEqual(titles(self.boxes([tk('only', date(2026, 12, 8))])), ['In 3 weeks'])
        self.assertEqual(self.boxes([]), [])

    def test_on_a_saturday_tomorrow_is_next_week_and_this_week_is_empty(self):
        saturday = date(2026, 11, 21)
        boxes = self.boxes([tk('sun', date(2026, 11, 22)), tk('tue', date(2026, 11, 24)), tk('sat', saturday)], today=saturday)
        self.assertEqual(titles(boxes), ['Today', 'Tomorrow', 'Next week'])
        self.assertEqual([t.title for t in boxes[2]['rows']], ['tue'])

    def test_on_a_sunday_this_week_is_the_rest_of_the_week_through_saturday(self):
        sunday = date(2026, 11, 22)
        boxes = self.boxes([tk('mon', date(2026, 11, 23)), tk('wed', date(2026, 11, 25)), tk('sat', date(2026, 11, 28)), tk('next sun', date(2026, 11, 29)), tk('next sat', date(2026, 12, 5))], today=sunday)
        self.assertEqual(titles(boxes), ['Tomorrow', 'This week', 'Next week'])
        self.assertEqual([t.title for t in boxes[1]['rows']], ['wed', 'sat'])
        self.assertEqual([t.title for t in boxes[2]['rows']], ['next sun', 'next sat'])             # a week that crosses a month end

    def test_on_a_friday_tomorrow_is_the_saturday_and_next_week_starts_sunday(self):
        friday = date(2026, 11, 20)
        boxes = self.boxes([tk('sat', date(2026, 11, 21)), tk('sun', date(2026, 11, 22))], today=friday)
        self.assertEqual(titles(boxes), ['Tomorrow', 'Next week'])

    def test_inside_a_box_the_tickets_run_by_due_date_then_priority_then_title(self):
        boxes = self.boxes([tk('b high', date(2026, 11, 21), Priority.HIGH), tk('a low', date(2026, 11, 21), Priority.LOW), tk('early', date(2026, 11, 20)), tk('a high', date(2026, 11, 21), Priority.HIGH)])
        self.assertEqual([t.title for t in boxes[0]['rows']], ['early', 'a high', 'b high', 'a low'])

    def test_a_chosen_sort_keeps_the_order_the_tickets_came_in(self):
        boxes = self.boxes([tk('z', date(2026, 11, 21)), tk('a', date(2026, 11, 20))], keep_order=True)
        self.assertEqual([t.title for t in boxes[0]['rows']], ['z', 'a'])

    def test_urgent_has_its_own_box_even_without_a_date(self):
        boxes = self.boxes([tk('urgent nodate', None, Priority.URGENT), tk('plain nodate')])
        self.assertEqual(titles(boxes), ['Urgent', 'No due date'])
        self.assertEqual(boxes[0]['tone'], 'urgent')

    def test_finished_tickets_are_not_due_they_go_in_a_box_for_the_month_they_were_closed(self):
        boxes = self.boxes([tk('open', date(2026, 11, 10)), tk('done nov', date(2026, 11, 10), status=Ticket.Status.COMPLETED, completed=date(2026, 11, 12)),
                            tk('done oct', None, status=Ticket.Status.CANCELLED, completed=date(2026, 10, 3)), tk('done nov 2', None, status=Ticket.Status.VERIFIED, completed=date(2026, 11, 2))])
        self.assertEqual(titles(boxes), ['Overdue', 'Done · November 2026', 'Done · October 2026'])
        self.assertEqual(sorted(t.title for t in boxes[1]['rows']), ['done nov', 'done nov 2'])

    def test_bucket_of_names_a_key_an_order_and_a_tone(self):
        self.assertEqual(bucket_of(tk('x', date(2026, 11, 10)), WED, DONE)[0::4], ('overdue', 'late'))
        self.assertEqual(bucket_of(tk('x', WED), WED, DONE)[:3], ('today', 2, 'Today'))


@override_settings(STORAGES=STORAGES)
class TheTicketScreenTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user('pm', 'pm@example.com', 'pw12345678')
        self.staff = StaffProfile.objects.get_or_create(user=user)[0]
        self.staff.new_face = True
        self.staff.role = StaffProfile.Role.PROPERTY_MANAGER
        self.staff.save()
        self.client.force_login(user)
        self.prop = Property.objects.create(name='Grand Condos', property_type=Property.Type.ASSOCIATION, is_active=True)
        today = timezone.localdate()
        make = lambda title, **kw: Ticket.objects.create(title=title, property=self.prop, created_by=user, assigned_staff=self.staff, source=Ticket.Source.MANUAL, **kw)
        self.urgent = make('Roof leak', priority=Priority.URGENT, due_date=at(today + timedelta(days=1)))
        self.late = make('Overdue gate', due_date=at(today - timedelta(days=4)))
        self.today = make('Due today', due_date=at(today))
        self.far = make('Far off', due_date=at(today + timedelta(days=40)))
        self.nodate = make('Whenever')
        self.url = reverse('ticket_list')

    def box_titles(self, response):
        return [b['title'] for b in response.context['boxes']]

    def test_now_shows_urgent_overdue_today_and_undated_in_that_order(self):
        titles_ = self.box_titles(self.client.get(self.url))
        self.assertEqual(titles_[:3], ['Urgent', 'Overdue', 'Today'])
        self.assertEqual(titles_[-1], 'No due date')
        self.assertNotIn('Far off', [t.title for b in self.client.get(self.url).context['boxes'] for t in b['rows']])          # Later's, not Now's

    def test_later_has_the_far_off_ticket_in_its_week_box(self):
        response = self.client.get(self.url + '?horizon=later')
        boxes = response.context['boxes']
        self.assertEqual([t.title for b in boxes for t in b['rows']], ['Far off'])
        self.assertRegex(boxes[0]['title'], r'^In \d weeks$')

    def test_the_boxes_are_dealt_into_three_columns_in_reading_order(self):
        response = self.client.get(self.url)
        columns = response.context['columns']
        self.assertEqual(len(columns), 3)
        self.assertEqual([b['title'] for col in columns for b in col], self.box_titles(response))          # down the first column, then the next: the order is kept

    def test_the_header_buttons_tabs_and_one_control_row(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn('class="np-head"', html)
        self.assertIn('New ticket', html)
        self.assertEqual(html.count('class="np-seg"'), 1)
        seg = html.split('class="np-seg"')[1].split('</nav>')[0]
        self.assertIn('>Now<', seg)
        self.assertIn('>Later<', seg)
        self.assertEqual(html.count('class="np-controls"'), 1)
        controls = html.split('class="np-controls"')[1].split('</form>')[0]
        for needle in ('name="q"', 'data-np-filters-toggle="np-filters"', 'data-np-sort'):
            self.assertIn(needle, controls)
        self.assertNotIn('<thead', html.split('id="ticket-table"')[1].split('id="ticket-mobile-cards"')[0])          # no table any more

    def test_the_filters_button_says_how_many_are_on_and_clear_appears(self):
        html = self.client.get(self.url + f'?property={self.prop.pk}&due=today').content.decode()
        self.assertIn('<span class="np-n">2</span>', html)
        self.assertIn('class="np-clear"', html)
        self.assertNotIn('np-clear', self.client.get(self.url).content.decode().split('class="np-controls"')[1].split('</form>')[0])

    def row_of(self, html, ticket):
        """The HTML of one ticket's row in the boxes."""
        return html.split('<a class="np-link" href="%s"' % reverse('ticket_detail', args=[ticket.pk]))[1].split('<div class="np-row')[0]

    def test_a_ticket_row_shows_only_the_name_and_an_arrow_until_it_is_opened(self):
        html = self.client.get(self.url).content.decode()
        row = self.row_of(html, self.urgent)
        shown, hidden = row.split('<div class="np-more-body" hidden>')
        self.assertIn('Roof leak', shown)
        self.assertNotIn('np-flag-urgent', html)                                                  # it is already in the Urgent box: no Urgent tag on the row
        self.assertNotIn('Grand Condos', shown)                                                   # the property and the due date are not on the closed line
        self.assertNotIn('np-when', shown)
        self.assertIn('data-np-more aria-expanded="false"', shown)                                 # the down arrow
        self.assertIn('Grand Condos', hidden)                                                     # they are in the lines under it
        self.assertRegex(hidden, r'Due [A-Z][a-z]{2} \d{1,2}')
        self.assertEqual(html.count('class="np-sub"'), 0)

    def test_an_opened_row_has_the_latest_update_or_says_there_is_none(self):
        first = TicketStatusNote.objects.create(ticket=self.late, body='<b>First</b> note')
        TicketStatusNote.objects.filter(pk=first.pk).update(created_at=timezone.now() - timedelta(days=2))
        TicketStatusNote.objects.create(ticket=self.late, body='Gate motor ordered, <i>arrives Friday</i>')
        html = self.client.get(self.url).content.decode()
        hidden = self.row_of(html, self.late).split('<div class="np-more-body" hidden>')[1]
        self.assertIn('Gate motor ordered, arrives Friday', hidden)                               # the latest, tags stripped
        self.assertNotIn('First note', hidden)
        self.assertIn('np-late', hidden)                                                          # a late due date is red
        self.assertIn('No updates yet.', self.row_of(html, self.today).split('<div class="np-more-body" hidden>')[1])
        self.assertIn('No due date', self.row_of(html, self.nodate).split('<div class="np-more-body" hidden>')[1])

    def test_a_finished_ticket_opens_to_its_closing_note(self):
        TicketStatusNote.objects.create(ticket=self.late, body='an older update')
        Ticket.objects.filter(pk=self.late.pk).update(status=Ticket.Status.COMPLETED, completed_at=timezone.now())
        TicketClosingNote.objects.create(ticket=self.late, body='Gate fixed and tested', status=Ticket.Status.COMPLETED)
        html = self.client.get(self.url + '?status=complete').content.decode()
        hidden = self.row_of(html, self.late).split('<div class="np-more-body" hidden>')[1]
        self.assertIn('Gate fixed and tested', hidden)
        self.assertNotIn('an older update', hidden)

    def test_opening_a_row_costs_the_page_no_extra_queries(self):
        self.client.get(self.url)                                                                 # warm anything cached
        with CaptureQueriesContext(connection) as before:
            self.client.get(self.url)
        for i in range(8):
            t = Ticket.objects.create(title=f'more {i}', property=self.prop, created_by=self.staff.user, assigned_staff=self.staff, source=Ticket.Source.MANUAL, due_date=at(timezone.localdate() + timedelta(days=2)))
            TicketStatusNote.objects.create(ticket=t, body='an update')
        with CaptureQueriesContext(connection) as after:
            self.client.get(self.url)
        self.assertEqual(len(after), len(before))                                                 # eight more tickets with notes: same number of queries

    def test_a_ticket_row_is_one_line_with_a_high_tag_only_and_the_extra_facts_in_the_hover(self):
        high = Ticket.objects.create(title='Fix the fence', property=self.prop, created_by=self.staff.user, assigned_staff=self.staff, source=Ticket.Source.MANUAL, priority=Priority.HIGH,
                                     due_date=at(timezone.localdate() + timedelta(days=2)))
        html = self.client.get(self.url).content.decode()
        row = self.row_of(html, high)
        closed = row.split('<div class="np-more-body" hidden>')[0]
        self.assertNotIn('np-l2', row)                                                              # no second line of the old kind
        self.assertIn('np-flag-high', closed)
        self.assertIn('title="', closed)                                                            # who has it is the hover text
        self.assertNotIn('np-flag-urgent', html)
        self.assertEqual(html.count('class="np-l2"'), 0)

    def test_the_live_search_still_answers_with_the_boxes_and_the_phone_cards(self):
        data = self.client.get(self.url + '?q=gate', HTTP_X_REQUESTED_WITH='XMLHttpRequest').json()
        self.assertEqual(set(data), {'desktop', 'mobile', 'pager'})
        self.assertIn('np-box', data['desktop'])
        self.assertIn('Overdue gate', data['desktop'])
        self.assertNotIn('Roof leak', data['desktop'])
        self.assertIn('m-tk-card', data['mobile'])

    def test_the_sort_choice_keeps_the_order_inside_the_boxes(self):
        response = self.client.get(self.url + '?sort=issue')
        self.assertEqual(response.status_code, 200)
        self.assertIn('data-np-sort', response.content.decode())
        selected = response.content.decode().split('data-np-sort')[1].split('</select>')[0]
        self.assertIn('<option value="issue" selected>', selected)

    def test_a_list_of_finished_tickets_is_boxed_by_the_month_they_were_closed(self):
        Ticket.objects.filter(pk=self.late.pk).update(status=Ticket.Status.COMPLETED, completed_at=timezone.now())
        response = self.client.get(self.url + '?status=complete')
        self.assertEqual(len(response.context['boxes']), 1)
        self.assertTrue(response.context['boxes'][0]['title'].startswith('Done'))
