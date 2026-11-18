"""Compliance (items and rules) and Projects (active and templates) in the newspaper look, and the header, tabs, buttons and control row the three newspaper screens (with Tickets) share."""
from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import Property, StaffProfile
from processes.models import ProcessTemplate, ProcessTemplateStep, ProcessRun, StepType
from processes.projects import start_project
from tickets.models import Ticket
from worksessions.models import Frequency, Session, SessionTemplate
from worksessions.services import generation

STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}
T = Property.Type


@override_settings(STORAGES=STORAGES)
class _Base(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user('pm', 'pm@example.com', 'pw12345678', first_name='Pat', last_name='Manager')
        self.staff = StaffProfile.objects.get_or_create(user=user)[0]
        self.staff.new_face = True
        self.staff.save()
        self.client.force_login(user)
        self.assoc = Property.objects.create(name='Grand Condos', property_type=T.ASSOCIATION)
        self.rental = Property.objects.create(name='800 Tropic', property_type=T.SHORT_TERM_RENTAL)

    def box_titles(self, response):
        return [b['title'] for b in response.context['boxes']]


class ComplianceBoxesTests(_Base):
    def setUp(self):
        super().setUp()
        self.rules = {}
        for name, freq in (('Water meter reading', Frequency.DAILY), ('Pool log', Frequency.WEEKLY), ('Bank reconciliation', Frequency.MONTHLY), ('Fire inspection', Frequency.YEARLY),
                           ('Insurance review', Frequency.QUARTERLY), ('Elevator test', Frequency.SEMIANNUAL)):
            rule = SessionTemplate.objects.create(name=name, frequency=freq, next_open_date=date(2026, 11, 2), owner=self.staff)
            generation.generate_for_template(rule, today=date(2026, 11, 20))
            self.rules[name] = rule
        self.url = reverse('ws_compliance')

    def test_the_items_are_boxed_by_how_often_from_daily_to_yearly(self):
        self.assertEqual(self.box_titles(self.client.get(self.url)), ['Daily', 'Weekly', 'Monthly', 'Quarterly', 'Twice a year', 'Yearly'])

    def test_the_rules_have_the_same_boxes_so_the_two_tabs_match(self):
        items = self.box_titles(self.client.get(self.url + '?tab=items'))
        rules = self.box_titles(self.client.get(self.url + '?tab=rules'))
        self.assertEqual(items, rules)

    def test_a_rule_with_no_open_item_only_has_its_box_on_the_rules_tab(self):
        SessionTemplate.objects.create(name='Fleet check', frequency=Frequency.BIWEEKLY, next_open_date=date(2027, 3, 1), owner=self.staff)
        self.assertNotIn('Bi-weekly', self.box_titles(self.client.get(self.url + '?tab=items')))
        rules = self.box_titles(self.client.get(self.url + '?tab=rules'))
        self.assertEqual(rules.index('Bi-weekly'), rules.index('Weekly') + 1)               # in its place between weekly and monthly

    def test_a_frequency_that_is_not_in_the_usual_list_goes_last(self):
        SessionTemplate.objects.create(name='One-off audit', frequency=Frequency.ONCE, next_open_date=date(2027, 3, 1), owner=self.staff)
        self.assertEqual(self.box_titles(self.client.get(self.url + '?tab=rules'))[-1], 'One time')

    def test_items_and_rules_sit_in_the_boxes_of_their_own_frequency(self):
        items = {b['title']: [r['title'] for r in b['rows']] for b in self.client.get(self.url).context['boxes']}
        self.assertEqual(items['Monthly'], ['Bank reconciliation'])
        rules = {b['title']: [r['t'].name for r in b['rows']] for b in self.client.get(self.url + '?tab=rules').context['boxes']}
        self.assertEqual(rules['Yearly'], ['Fire inspection'])

    def test_the_frequency_filter_leaves_just_that_box_on_both_tabs(self):
        self.assertEqual(self.box_titles(self.client.get(self.url + '?frequency=monthly')), ['Monthly'])
        self.assertEqual(self.box_titles(self.client.get(self.url + '?tab=rules&frequency=monthly')), ['Monthly'])

    def test_switched_off_rules_only_show_when_asked_for(self):
        SessionTemplate.objects.filter(pk=self.rules['Pool log'].pk).update(is_active=False)
        self.assertNotIn('Weekly', self.box_titles(self.client.get(self.url + '?tab=rules')))
        self.assertIn('Weekly', self.box_titles(self.client.get(self.url + '?tab=rules&state=all')))
        self.assertIn('Switched off', self.client.get(self.url + '?tab=rules&state=off').content.decode())

    def test_a_row_opens_the_item_or_the_rule(self):
        item = Session.objects.filter(template=self.rules['Bank reconciliation']).first()
        self.assertIn(f'href="{reverse("session_detail", args=[item.pk])}"', self.client.get(self.url).content.decode())
        self.assertIn(f'href="{reverse("session_template_detail", args=[self.rules["Bank reconciliation"].pk])}"', self.client.get(self.url + '?tab=rules').content.decode())

    def test_the_live_search_answers_with_just_the_boxes(self):
        data = self.client.get(self.url + '?q=pool', HTTP_X_REQUESTED_WITH='XMLHttpRequest').json()
        self.assertEqual(set(data), {'paper', 'pager'})
        self.assertIn('Pool log', data['paper'])
        self.assertNotIn('Bank reconciliation', data['paper'])
        rules = self.client.get(self.url + '?tab=rules&q=fire', HTTP_X_REQUESTED_WITH='XMLHttpRequest').json()
        self.assertIn('Fire inspection', rules['paper'])
        self.assertEqual(rules['pager'], '')

    def test_the_old_green_blocks_and_table_are_gone(self):
        for tab in ('items', 'rules'):
            html = self.client.get(self.url + f'?tab={tab}').content.decode()
            self.assertNotIn('ws-blocks', html)
            self.assertNotIn('ws-green-rows', html)
            self.assertNotIn('<thead', html)


class ProjectBoxesTests(_Base):
    def setUp(self):
        super().setUp()
        self.template = ProcessTemplate.objects.create(name='Roof job', kind=ProcessTemplate.Kind.PROJECT, category='Association')
        ProcessTemplateStep.objects.create(process_template=self.template, sequence_order=1, step_type=StepType.CHECKBOX, label='Get bids')
        self.url = reverse('ws_projects')

    def project(self, prop, title):
        ticket = Ticket.objects.create(title=title, property=prop, assigned_staff=self.staff)
        return start_project(self.template, user=self.staff.user, ticket=ticket)

    def test_running_projects_are_boxed_by_the_line_of_business_of_their_property(self):
        self.project(self.assoc, 'Roof at Grand')
        self.project(self.rental, 'Paint 800 Tropic')
        self.project(Property.objects.create(name='Lofts', property_type=T.COMMERCIAL), 'Lofts lobby')
        self.project(Property.objects.create(name='225 NE 1st', property_type=T.LONG_TERM_RENTAL), 'Fence')
        self.project(Property.objects.create(name='1038 E Heritage', property_type=T.SNOWBIRD), 'Check pipes')
        self.assertEqual(self.box_titles(self.client.get(self.url)), ['Associations', 'Short-term rentals', 'Long-term rentals', 'Snowbird oversight', 'Commercial'])

    def test_a_project_with_no_property_goes_in_other_last(self):
        self.project(self.assoc, 'Roof at Grand')
        self.project(None, 'Office move')
        boxes = self.client.get(self.url).context['boxes']
        self.assertEqual([b['title'] for b in boxes], ['Associations', 'Other'])
        self.assertEqual([r['title'] for r in boxes[1]['rows']], ['Office move'])

    def test_a_running_project_row_links_to_it_and_shows_its_step(self):
        run = self.project(self.assoc, 'Roof at Grand')
        html = self.client.get(self.url).content.decode()
        self.assertIn('Roof at Grand', html)
        self.assertIn('Step 1 of 1: Get bids', html)
        self.assertIn('np-bar', html)
        self.assertTrue(ProcessRun.objects.filter(pk=run.pk).exists())

    def test_templates_are_boxed_by_the_line_their_category_names(self):
        for name, category, kind in (('Short-term clean', 'Short-Term Rental', ProcessTemplate.Kind.CHECKLIST), ('Lease up', 'Long-Term Rental', ProcessTemplate.Kind.PROJECT),
                                     ('Build-out', 'Commercial', ProcessTemplate.Kind.PROJECT), ('Snowbird check', 'Snowbird Oversight', ProcessTemplate.Kind.CHECKLIST), ('Odd job', '', ProcessTemplate.Kind.PROJECT)):
            ProcessTemplate.objects.create(name=name, category=category, kind=kind)
        self.assertEqual(self.box_titles(self.client.get(self.url + '?tab=templates')), ['Associations', 'Short-term rentals', 'Long-term rentals', 'Snowbird oversight', 'Commercial', 'Other'])

    def test_a_template_row_says_whether_it_is_a_project_or_a_process_and_opens_the_builder(self):
        ProcessTemplate.objects.create(name='Short-term clean', category='Short-Term Rental', kind=ProcessTemplate.Kind.CHECKLIST)
        html = self.client.get(self.url + '?tab=templates').content.decode()
        self.assertIn('>Process<', html)
        self.assertIn('>Project<', html)
        self.assertIn(f'href="{reverse("process_template_edit", args=[self.template.pk])}"', html)

    def test_the_templates_search_filters_by_name(self):
        ProcessTemplate.objects.create(name='Short-term clean', category='Short-Term Rental', kind=ProcessTemplate.Kind.CHECKLIST)
        data = self.client.get(self.url + '?tab=templates&q=clean', HTTP_X_REQUESTED_WITH='XMLHttpRequest').json()
        self.assertIn('Short-term clean', data['paper'])
        self.assertNotIn('Roof job', data['paper'])

    def test_the_active_filters_are_behind_the_filters_button_and_the_templates_tab_has_none(self):
        active = self.client.get(self.url + '?tab=active').content.decode()
        self.assertIn('data-np-filters-toggle="np-filters"', active)
        self.assertIn('id="tf-box"', active)
        templates = self.client.get(self.url + '?tab=templates').content.decode()
        self.assertNotIn('data-np-filters-toggle', templates)


class StartAProjectTests(_Base):
    def setUp(self):
        super().setUp()
        self.template = ProcessTemplate.objects.create(name='Roof job', kind=ProcessTemplate.Kind.PROJECT)
        ProcessTemplateStep.objects.create(process_template=self.template, sequence_order=1, step_type=StepType.CHECKBOX, label='Get bids')
        self.url = reverse('ws_start_project')

    def test_it_makes_a_ticket_and_starts_the_project_on_its_first_step(self):
        before = Ticket.objects.count()
        response = self.client.post(self.url, {'template': self.template.pk, 'project_property': self.assoc.pk, 'title': 'Roof at Grand'})
        self.assertEqual(Ticket.objects.count(), before + 1)
        run = ProcessRun.objects.get()
        self.assertEqual((run.ticket.title, run.ticket.property_id, run.process_template_id), ('Roof at Grand', self.assoc.pk, self.template.pk))
        self.assertEqual(run.assigned_to_id, self.staff.pk)                                    # "Me" by default
        self.assertEqual(response.status_code, 302)
        self.assertNotIn('Roof at Grand', [t.title for t in self.client.get(reverse('ticket_list')).context['tickets']])      # it lives under Projects, not Tickets

    def test_the_title_defaults_to_the_template_and_the_property_is_optional(self):
        self.client.post(self.url, {'template': self.template.pk})
        run = ProcessRun.objects.get()
        self.assertEqual(run.ticket.title, 'Roof job')
        self.assertIsNone(run.ticket.property)

    def test_no_template_or_a_process_template_is_refused_and_nothing_is_made(self):
        process = ProcessTemplate.objects.create(name='Clean', kind=ProcessTemplate.Kind.CHECKLIST)
        before = Ticket.objects.count()
        for data in ({}, {'template': process.pk}, {'template': 99999}):
            response = self.client.post(self.url, data, follow=True)
            self.assertContains(response, 'Choose a project template.')
        self.assertEqual((Ticket.objects.count(), ProcessRun.objects.count()), (before, 0))

    def test_a_get_is_refused(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_the_form_opens_when_quick_add_sends_start_1(self):
        self.assertNotIn('<details class="np-start" open>', self.client.get(reverse('ws_projects')).content.decode())
        self.assertIn('<details class="np-start" open>', self.client.get(reverse('ws_projects') + '?start=1').content.decode())
        self.assertIn('<details class="np-start" open>', self.client.get(reverse('ws_projects') + '?tab=templates&start=1').content.decode())


class TheSharedHeaderTabsAndControlsTests(_Base):
    """The three screens look and behave alike: icon and title, one line of words, the persistent buttons on both tabs, a two-segment switch, ONE control row."""

    def setUp(self):
        super().setUp()
        ProcessTemplate.objects.create(name='Roof job', kind=ProcessTemplate.Kind.PROJECT)

    SCREENS = [
        ('ticket_list', '', 'New ticket', ('Now', 'Later'), ()),
        ('ws_compliance', '?tab=items', 'New rule', ('Items', 'Rules'), ()),
        ('ws_compliance', '?tab=rules', 'New rule', ('Items', 'Rules'), ()),
        ('ws_projects', '?tab=active', 'Start project', ('Active', 'Templates'), ('Add new template',)),
        ('ws_projects', '?tab=templates', 'Start project', ('Active', 'Templates'), ('Add new template',)),
    ]

    def test_every_screen_has_the_header_the_buttons_the_switch_and_one_control_row(self):
        for name, query, button, tabs, more in self.SCREENS:
            html = self.client.get(reverse(name) + query).content.decode()
            where = f'{name}{query}'
            self.assertEqual(html.count('class="np-head"'), 1, where)
            self.assertIn(button, html.split('class="np-head"')[1].split('class="np-seg"')[0], where)
            for extra in more:
                self.assertIn(extra, html.split('class="np-head"')[1].split('class="np-seg"')[0], where)
            seg = html.split('class="np-seg"')[1].split('</nav>')[0]
            for label in tabs:
                self.assertIn(f'>{label}<', seg, where)
            self.assertEqual(html.count('class="np-seg"'), 1, where)
            self.assertEqual(html.count('class="np-controls"'), 1, where)
            self.assertIn('name="q"', html.split('class="np-controls"')[1].split('</form>')[0], where)
            self.assertTrue('np-cols' in html or 'np-empty' in html, where)          # the boxes (or the empty note) are there

    def test_the_buttons_use_the_one_action_button_style(self):
        for name, query, button, _, more in self.SCREENS:
            head = self.client.get(reverse(name) + query).content.decode().split('class="np-head"')[1].split('class="np-seg"')[0]
            self.assertIn('class="btn-action"', head, f'{name}{query}')
            self.assertNotIn('btn-primary', head, f'{name}{query}')
            for extra in more:
                self.assertIn('btn-action btn-action-outline', head)

    def test_the_active_tab_is_marked(self):
        seg = self.client.get(reverse('ws_compliance') + '?tab=rules').content.decode().split('class="np-seg"')[1].split('</nav>')[0]
        self.assertIn('class="on" aria-current="page">Rules<', seg)
        self.assertNotIn('class="on" aria-current="page">Items<', seg)

    def test_the_description_is_one_short_line_with_the_long_words_behind_a_bubble(self):
        for name, query, *_ in self.SCREENS:
            head = self.client.get(reverse(name) + query).content.decode().split('class="np-head"')[1].split('class="np-seg"')[0]
            self.assertIn('class="np-desc"', head)
            self.assertIn('data-tip=', head)

    def test_the_switch_and_boxes_are_made_of_the_shared_pieces(self):
        for name, query, *_ in self.SCREENS:
            html = self.client.get(reverse(name) + query).content.decode()
            self.assertIn('css/newspaper.css', html)
            self.assertIn('js/newspaper.js', html)
