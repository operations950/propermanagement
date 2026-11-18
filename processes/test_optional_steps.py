from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from core.models import Property, StaffProfile
from tickets.models import Ticket

from .models import ProcessRun, ProcessTemplate, ProcessTemplateStep, StepType
from .projects import start_run
from .templatetags.process_extras import is_welcome_letter, step_response_summary


class OptionalTaskAndWelcomeLetterTests(TestCase):
    """A task step does not force a ticket; a welcome-letter step does not force a letter. Both can still be made."""

    def setUp(self):
        self.user = User.objects.create_user('mgr', 'm@example.com', 'x', is_staff=True)
        StaffProfile.objects.update_or_create(user=self.user, defaults={'role': StaffProfile.Role.ADMIN, 'is_company_admin': True})        # a ticket always lands on someone: a company admin is the last resort
        self.client.force_login(self.user)
        self.prop = Property.objects.create(name='Test Condo', property_type=Property.Type.ASSOCIATION)
        self.template = ProcessTemplate.objects.create(name='Tenant approval', kind=ProcessTemplate.Kind.PROJECT)
        spec = [('arrange', StepType.TASK_ASSIGNMENT, 'If approved: arrange gate, parking and pool access', {}),
                ('welcome', StepType.GENERATE_DOCUMENT, 'Send new tenant welcome package', {'form': 'welcome-letter-tenant', 'email': True}),
                ('welcome-owner', StepType.GENERATE_DOCUMENT, 'Send new owner welcome package', {'form': 'welcome-letter-owner', 'email': True}),
                ('notice', StepType.GENERATE_DOCUMENT, 'Prepare notice', {'form': 'board-meeting-notice', 'email': False})]
        for i, (key, kind, label, cfg) in enumerate(spec, 1):
            ProcessTemplateStep.objects.create(process_template=self.template, sequence_order=i, step_key=key, step_type=kind, label=label, config=cfg)
        self.run = start_run(self.template, user=self.user, property=self.prop)
        self.steps = {s.step_key: s for s in self.run.steps.all()}

    def post(self, name, step):
        return self.client.post(reverse(name, args=[step.pk]), follow=True)

    def fresh(self, key):
        return self.run.steps.get(step_key=key)

    # ---------------------------------------------------------------- task steps
    def test_a_task_step_can_be_marked_complete_without_creating_a_ticket(self):
        before = Ticket.objects.count()
        self.post('process_run_step_complete_task', self.steps['arrange'])
        step = self.fresh('arrange')
        self.assertTrue(step.is_complete)
        self.assertEqual(Ticket.objects.count(), before)
        self.assertEqual(step_response_summary(step), 'Done')

    def test_the_task_step_still_offers_both_buttons_before_anything_is_done(self):
        from django.template.loader import render_to_string
        html = render_to_string('processes/steps/task_assignment.html', {'step': self.steps['arrange']}, request=None)
        self.assertIn('Create task', html)
        self.assertIn('Mark complete', html)

    def test_a_task_that_was_created_is_still_followed(self):
        self.post('process_run_step_assign_task', self.steps['arrange'])
        step = self.fresh('arrange')
        self.assertTrue(step.response.get('ticket_id'))
        self.post('process_run_step_complete_task', step)
        self.assertFalse(self.fresh('arrange').is_complete)                       # the ticket is still open: the old rule stands for a task that exists
        Ticket.objects.filter(pk=step.response['ticket_id']).update(status=Ticket.DEPENDENCY_SATISFYING_STATUSES[0])
        self.post('process_run_step_complete_task', self.fresh('arrange'))
        self.assertTrue(self.fresh('arrange').is_complete)

    # ---------------------------------------------------------------- welcome letters
    def test_the_welcome_letter_filter_matches_only_the_welcome_letter_steps(self):
        self.assertTrue(is_welcome_letter(self.steps['welcome']))
        self.assertTrue(is_welcome_letter(self.steps['welcome-owner']))
        self.assertFalse(is_welcome_letter(self.steps['notice']))
        self.assertFalse(is_welcome_letter(self.steps['arrange']))

    def test_a_welcome_letter_step_can_be_marked_sent_without_a_letter(self):
        for key in ('welcome', 'welcome-owner'):
            self.post('process_run_step_mark_document_sent', self.steps[key])
            step = self.fresh(key)
            self.assertTrue(step.is_complete, key)
            self.assertFalse(step.response.get('document_id'))
            self.assertEqual(step_response_summary(step), 'Sent')

    def test_other_document_steps_still_need_their_document(self):
        self.post('process_run_step_mark_document_sent', self.steps['notice'])
        self.assertFalse(self.fresh('notice').is_complete)
        self.post('process_run_step_mark_document_sent', self.steps['arrange'])
        self.assertFalse(self.fresh('arrange').is_complete)

    def test_the_welcome_step_offers_mark_as_sent_and_a_prepare_button_when_the_property_is_known(self):
        from django.template.loader import render_to_string
        html = render_to_string('processes/steps/generate_document.html', {'step': self.steps['welcome']}, request=None)
        self.assertIn('Mark as sent', html)
        self.assertIn('Prepare the welcome letter', html)
        other = render_to_string('processes/steps/generate_document.html', {'step': self.steps['notice']}, request=None)
        self.assertNotIn('Mark as sent', other)

    def test_a_step_already_done_does_not_offer_mark_as_sent_again(self):
        self.post('process_run_step_mark_document_sent', self.steps['welcome'])
        from django.template.loader import render_to_string
        html = render_to_string('processes/steps/generate_document.html', {'step': self.fresh('welcome')}, request=None)
        self.assertNotIn('Mark as sent', html)

    def test_the_whole_run_can_finish_without_a_ticket_or_a_letter(self):
        for key, name in (('arrange', 'process_run_step_complete_task'), ('welcome', 'process_run_step_mark_document_sent'), ('welcome-owner', 'process_run_step_mark_document_sent')):
            self.post(name, self.steps[key])
        self.assertEqual(self.run.steps.filter(is_complete=True).count(), 3)                  # only the notice, which really needs its document, is left
