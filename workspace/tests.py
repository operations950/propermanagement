from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import Contact, Property, StaffProfile, Unit
from processes.models import ProcessTemplate, ProcessTemplateStep, StepType
from processes.projects import start_project
from tickets.models import Ticket

STATIC = override_settings(STORAGES={'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                                     'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})


class _Base(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user('pm', 'pm@example.com', 'pw12345678')
        self.staff = StaffProfile.objects.get_or_create(user=user)[0]
        self.staff.new_face = True
        self.staff.save()
        self.client.force_login(user)
        self.prop = Property.objects.create(name='12 Beach Rd', property_type=Property.Type.LONG_TERM_RENTAL)
        self.other = Property.objects.create(name='9 Pine St', property_type=Property.Type.LONG_TERM_RENTAL)

    def project_from_ticket(self, prop, title, due=None, assignee=None):
        template, _ = ProcessTemplate.objects.get_or_create(name='Roof job', kind=ProcessTemplate.Kind.PROJECT)
        if not template.steps.exists():
            ProcessTemplateStep.objects.create(process_template=template, sequence_order=1, step_type=StepType.CHECKBOX, label='Get bids')
        ticket = Ticket.objects.create(title=title, property=prop, due_date=due, assigned_staff=self.staff)
        return start_project(template, user=self.staff.user, assigned_to=assignee, ticket=ticket)


@STATIC
class ProjectFilterBubbleTests(_Base):
    def titles(self, **params):
        response = self.client.get(reverse('ws_projects'), {'tab': 'active', **params})
        self.assertEqual(response.status_code, 200)
        return [r['title'] for r in response.context['rows']], response

    def test_the_bubbles_replace_the_dropdown_and_filter_button(self):
        _, response = self.titles()
        html = response.content.decode()
        self.assertIn('id="tf-box"', html)
        self.assertIn('data-tf-open="property"', html)
        self.assertIn('data-tf-open="due"', html)
        self.assertIn('data-tf-open="staff"', html)
        self.assertNotIn('<select name="property"', html)

    def test_property_filter(self):
        self.project_from_ticket(self.prop, 'Roof at Beach')
        self.project_from_ticket(self.other, 'Roof at Pine')
        self.assertEqual(sorted(self.titles()[0]), ['Roof at Beach', 'Roof at Pine'])
        rows, response = self.titles(property=self.prop.pk)
        self.assertEqual(rows, ['Roof at Beach'])
        self.assertIn('Property: 12 Beach Rd', response.content.decode())

    def test_assigned_to_filter(self):
        self.project_from_ticket(self.prop, 'Mine', assignee=self.staff)
        self.project_from_ticket(self.other, 'Nobody')
        self.assertEqual(self.titles(owner=self.staff.pk)[0], ['Mine'])
        self.assertEqual(self.titles(owner='none')[0], ['Nobody'])

    def test_due_date_filter(self):
        now = timezone.now()
        self.project_from_ticket(self.prop, 'Late', due=now - timedelta(days=3))
        self.project_from_ticket(self.prop, 'Soon', due=now + timedelta(days=2))
        self.project_from_ticket(self.prop, 'Undated')
        self.assertEqual(self.titles(due='overdue')[0], ['Late'])
        self.assertEqual(self.titles(due='d3')[0], ['Soon'])
        self.assertEqual(self.titles(due='none')[0], ['Undated'])

    def test_search_still_works_with_the_bubbles(self):
        self.project_from_ticket(self.prop, 'Roof at Beach')
        self.project_from_ticket(self.other, 'Gutters')
        self.assertEqual(self.titles(q='gutter')[0], ['Gutters'])


@STATIC
class StepsStayOnTheProjectScreenTests(_Base):
    def test_checking_a_step_on_the_project_screen_stays_there_for_a_ticket_project(self):
        run = self.project_from_ticket(self.prop, 'Roof at Beach')
        step = run.steps.first()
        screen = reverse('ws_run_detail', args=[run.pk])
        response = self.client.post(reverse('process_run_step_update', args=[step.pk]), HTTP_REFERER='http://testserver' + screen)
        self.assertRedirects(response, screen, fetch_redirect_response=False)
        step.refresh_from_db()
        self.assertTrue(step.is_complete)

    def test_the_same_step_from_the_ticket_page_goes_back_to_the_ticket(self):
        run = self.project_from_ticket(self.prop, 'Roof at Beach')
        step = run.steps.first()
        ticket_page = reverse('ticket_detail', args=[run.ticket_id])
        response = self.client.post(reverse('process_run_step_update', args=[step.pk]), HTTP_REFERER='http://testserver' + ticket_page)
        self.assertRedirects(response, ticket_page, fetch_redirect_response=False)

    def test_no_referer_or_another_site_keeps_the_old_behaviour(self):
        run = self.project_from_ticket(self.prop, 'Roof at Beach')
        step = run.steps.first()
        screen = reverse('ws_run_detail', args=[run.pk])
        ticket_page = reverse('ticket_detail', args=[run.ticket_id])
        self.assertRedirects(self.client.post(reverse('process_run_step_update', args=[step.pk])), ticket_page, fetch_redirect_response=False)
        self.assertRedirects(self.client.post(reverse('process_run_step_update', args=[step.pk]), HTTP_REFERER='http://evil.example' + screen), ticket_page, fetch_redirect_response=False)


@STATIC
class ContactUnitsByPropertyTests(_Base):
    def setUp(self):
        super().setUp()
        self.assoc = Property.objects.create(name='Sea Oaks', property_type=Property.Type.ASSOCIATION)
        self.units = [Unit.objects.create(property=self.assoc, label=n) for n in ('101', '102')]
        self.bare = Property.objects.create(name='Plain House', property_type=Property.Type.ASSOCIATION)

    def test_units_are_grouped_under_their_property_not_listed_as_one_wall(self):
        html = self.client.get(reverse('contact_create')).content.decode()
        self.assertIn('data-unit-property="%d"' % self.assoc.pk, html)
        self.assertNotIn('data-unit-property="%d"' % self.bare.pk, html)
        self.assertNotIn('type="checkbox" name="units"', html)
        self.assertIn('data-value="%d" data-label="101" data-property="%d"' % (self.units[0].pk, self.assoc.pk), html)

    def test_a_unit_can_be_saved_with_its_property_and_not_without(self):
        data = {'name': 'Pat Owner', 'contact_type': 'owner', 'properties': [self.assoc.pk], 'units': [self.units[0].pk]}
        response = self.client.post(reverse('contact_create'), data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(Contact.objects.get(name='Pat Owner').units.all()), [self.units[0]])
        response = self.client.post(reverse('contact_create'), {**data, 'name': 'Stray Sam', 'properties': [self.other.pk]})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Contact.objects.filter(name='Stray Sam').exists())

    def test_saved_units_load_as_selected_bubbles(self):
        c = Contact.objects.create(name='Pat Owner', contact_type='owner')
        c.properties.add(self.assoc)
        c.units.add(self.units[1])
        html = self.client.get(reverse('contact_edit', args=[c.pk])).content.decode()
        self.assertIn('data-initial-values="%d"' % self.units[1].pk, html)
