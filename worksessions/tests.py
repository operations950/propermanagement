from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import StaffProfile
from worksessions.models import Frequency, Session, SessionTemplate
from worksessions.services import generation


class TwiceAYearTests(TestCase):
    """A compliance rule can open twice a year: every six months from the first date."""

    def setUp(self):
        user = get_user_model().objects.create_user('pm', 'pm@example.com', 'pw12345678')
        self.staff = StaffProfile.objects.get_or_create(user=user)[0]

    def rule(self, first, **kw):
        return SessionTemplate.objects.create(name='Fire alarm inspection', frequency=Frequency.SEMIANNUAL, next_open_date=first, owner=self.staff, **kw)

    def test_it_is_in_the_list_and_reads_twice_a_year(self):
        self.assertIn(('semiannual', 'Twice a year'), Frequency.choices)
        self.assertEqual(self.rule(date(2026, 1, 15)).get_frequency_display(), 'Twice a year')

    def test_it_comes_round_every_six_months(self):
        self.assertEqual(generation.advance(date(2026, 1, 15), Frequency.SEMIANNUAL, None), date(2026, 7, 15))
        self.assertEqual(generation.advance(date(2026, 7, 15), Frequency.SEMIANNUAL, None), date(2027, 1, 15))
        self.assertEqual(generation.advance(date(2026, 8, 31), Frequency.SEMIANNUAL, None), date(2027, 2, 28))
        self.assertEqual(generation.period_label_for(date(2026, 7, 15), Frequency.SEMIANNUAL), 'July 2026')

    def test_the_preview_and_the_generator_use_it(self):
        t = self.rule(date(2026, 1, 15))
        self.assertEqual([p['date'] for p in generation.preview_next_occurrences(t, 3)], [date(2026, 1, 15), date(2026, 7, 15), date(2027, 1, 15)])
        self.assertEqual(generation.generate_for_template(t, today=date(2026, 8, 1)), 2)       # January and July have both opened
        t.refresh_from_db()
        self.assertEqual((t.next_open_date, sorted(Session.objects.values_list('period_label', flat=True))), (date(2027, 1, 15), ['January 2026', 'July 2026']))
        self.assertEqual(generation.generate_for_template(t, today=date(2026, 8, 2)), 0)       # nothing opens again until January

    def test_it_sorts_between_yearly_and_quarterly_and_filters_on_the_compliance_tab(self):
        from workspace import services
        order = list(services.FREQ_ORDER)
        self.assertEqual(order.index(Frequency.SEMIANNUAL), order.index(Frequency.YEARLY) + 1)
        self.assertLess(order.index(Frequency.SEMIANNUAL), order.index(Frequency.QUARTERLY))
        t = self.rule(date(2026, 1, 15))
        generation.generate_for_template(t, today=date(2026, 1, 15))
        items, sel = services.compliance_items({'frequency': 'semiannual'})
        self.assertEqual((sel['frequency'], items.count()), ('semiannual', 1))


from django.test import override_settings          # noqa: E402

from worksessions.models import SessionLine        # noqa: E402
from worksessions.services import lifecycle        # noqa: E402

_STATIC = override_settings(STORAGES={'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                                      'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})


@_STATIC
class DeleteAComplianceRuleTests(TestCase):
    """A rule can be deleted from its edit form, unless deleting it would erase finished work."""

    def setUp(self):
        user = get_user_model().objects.create_user('pm', 'pm@example.com', 'pw12345678')
        self.staff = StaffProfile.objects.get_or_create(user=user)[0]
        self.staff.new_face = True
        self.staff.save()
        self.client.force_login(user)

    def rule(self, name='Fire alarm inspection'):
        return SessionTemplate.objects.create(name=name, frequency=Frequency.QUARTERLY, next_open_date=date(2026, 1, 15), owner=self.staff)

    def delete(self, rule):
        return self.client.post(reverse('session_template_delete', args=[rule.pk]), follow=True)

    def test_a_rule_that_never_opened_an_item_is_deleted(self):
        rule = self.rule()
        response = self.delete(rule)
        self.assertFalse(SessionTemplate.objects.filter(pk=rule.pk).exists())
        self.assertContains(response, 'Deleted &quot;Fire alarm inspection&quot;.')
        self.assertEqual(response.redirect_chain[-1][0], reverse('ws_compliance') + '?tab=rules')

    def test_a_rule_with_only_untouched_open_items_goes_with_its_items(self):
        rule = self.rule()
        generation.generate_for_template(rule, today=date(2026, 5, 1))
        n = rule.sessions.count()
        self.assertGreater(n, 0)
        response = self.delete(rule)
        self.assertFalse(SessionTemplate.objects.filter(pk=rule.pk).exists())
        self.assertEqual(Session.objects.filter(template_id=rule.pk).count(), 0)
        self.assertContains(response, f'and its {n} open item')

    def test_a_rule_with_a_finished_item_is_kept_and_the_reason_says_so(self):
        rule = self.rule()
        generation.generate_for_template(rule, today=date(2026, 5, 1))
        lifecycle.submit_session(rule.sessions.first())
        response = self.delete(rule)
        self.assertTrue(SessionTemplate.objects.filter(pk=rule.pk).exists())
        self.assertContains(response, '1 finished item')
        self.assertContains(response, 'turn the rule off instead')

    def test_a_rule_with_a_line_that_was_worked_on_is_kept(self):
        rule = self.rule()
        generation.generate_for_template(rule, today=date(2026, 5, 1))
        line = SessionLine.objects.create(session=rule.sessions.first(), label='Unit 3B', state=SessionLine.State.PENDING)
        line.state = SessionLine.State.DONE
        line.save()
        self.delete(rule)
        self.assertTrue(SessionTemplate.objects.filter(pk=rule.pk).exists())

    def test_it_must_be_a_post(self):
        rule = self.rule()
        self.assertEqual(self.client.get(reverse('session_template_delete', args=[rule.pk])).status_code, 405)
        self.assertTrue(SessionTemplate.objects.filter(pk=rule.pk).exists())

    def test_the_edit_form_has_a_delete_button_on_the_right_of_save_and_a_new_rule_does_not(self):
        rule = self.rule()
        html = self.client.get(reverse('session_template_edit', args=[rule.pk])).content.decode()
        self.assertIn('Save changes', html)
        self.assertIn('form="delete-rule"', html)
        self.assertIn('ms-auto', html)
        self.assertIn('id="delete-rule" method="post" action="' + reverse('session_template_delete', args=[rule.pk]), html)
        self.assertIn('confirm(', html)
        self.assertNotIn('form="delete-rule"', self.client.get(reverse('session_template_create')).content.decode())
