"""Where the regular assessments will land this year can be typed on a budget draft: the worked-out figure is only the starting point."""
import json
from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from . import budgeting as bg
from .models import Budget
from .tests import close, make_assoc, spend
from .test_budget_sections import STORAGES


@override_settings(STORAGES=STORAGES)
class TypedProjectedAssessmentsTests(TestCase):
    def setUp(self):
        self.prop = make_assoc('Typed Condos')
        for m in range(1, 7):
            spend(self.prop, date(2026, m, 10), '6906', '600')
        close(self.prop, range(1, 7))
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.page = reverse('accounting:budget_year', args=[self.prop.pk, 2027])
        self.client.post(self.page, {'action': 'start'})
        self.edit = reverse('accounting:budget_edit', args=[self.prop.pk, 2027])

    def state(self, stage='draft'):
        have = bg.versions(Budget.objects.get(association=self.prop, year=2027))
        return bg.editor_state(have[stage], have)['assess'], have

    def change(self, value, stage='draft'):
        r = self.client.post(self.edit + f'?stage={stage}', json.dumps({'op': 'assess_projected', 'value': value}), content_type='application/json')
        return r, r.json()

    def test_it_starts_worked_out_and_a_typed_figure_takes_over(self):
        before, _ = self.state()
        self.assertFalse(before['manual'])
        r, body = self.change('345,000.50')
        self.assertEqual(r.status_code, 200)
        a = body['assess']
        self.assertEqual((a['manual'], a['projected']), (True, 345000.50))
        self.assertEqual(a['calc'], before['projected'])                                  # the worked-out figure is still shown beside it
        self.assertAlmostEqual(a['nd'], 345000.50 - a['actual'], places=2)                 # the rest of the year is what is left to reach it

    def test_blank_puts_the_worked_out_figure_back(self):
        self.change('100000')
        r, body = self.change('')
        self.assertFalse(body['assess']['manual'])
        self.assertEqual(body['assess']['projected'], body['assess']['calc'])

    def test_something_that_is_not_an_amount_is_refused_and_changes_nothing(self):
        r, body = self.change('lots')
        self.assertEqual(r.status_code, 400)
        self.assertFalse(self.state()[0]['manual'])

    def test_it_does_not_change_what_the_budget_asks_the_owners_for(self):
        before, _ = self.state()
        self.change('999999')
        after, _ = self.state()
        self.assertEqual(after['proposed'], before['proposed'])

    def test_it_carries_to_the_next_stage_and_a_locked_stage_refuses_it(self):
        self.change('345000')
        self.client.post(self.page, {'action': 'advance'})
        a, have = self.state('board')
        self.assertEqual((a['manual'], a['projected']), (True, 345000.0))
        r, body = self.change('1', stage='draft')                                         # the draft is locked once the board draft was started
        self.assertEqual(r.status_code, 403)

    def test_the_screen_has_the_input(self):
        html = self.client.get(self.page).content.decode()
        self.assertIn('id="asProj"', html)
        self.assertIn('bdAssess', html)

    def test_the_script_has_no_quotes_that_end_its_own_strings_early(self):
        """The draft screen builds its rows in JavaScript strings in single quotes: a raw single quote inside an attribute broke the whole screen once."""
        html = self.client.get(self.page).content.decode()
        row = [line for line in html.splitlines() if 'id="asProj"' in line][0]
        self.assertNotIn("===\x27", row)
        self.assertNotIn("bdAssess(\x27\x27)", row)
        self.assertIn('event.keyCode===13', row)


@override_settings(STORAGES=STORAGES)
class ScheduledOwnerPaymentsTests(TestCase):
    """The rest of the year's regular assessments follow the owners' payment schedule (the assessment rules), not the pace of the months already booked."""

    def setUp(self):
        self.prop = make_assoc('Scheduled Condos')
        for m in range(1, 7):
            spend(self.prop, date(2026, m, 10), '6906', '600')
        close(self.prop, range(1, 7))                       # January to June are closed: July to December are still to come
        from core.models import Unit
        from .tests import owner_of
        for u in Unit.objects.filter(property=self.prop):
            owner_of(self.prop, u, f'Owner {u.label}')         # four units, each with a billed owner
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.page = reverse('accounting:budget_year', args=[self.prop.pk, 2027])

    def rule(self, **kw):
        from decimal import Decimal as D
        from . import ledger
        from .models import ChargeRule
        base = dict(association=self.prop, name='Assessment', frequency='monthly', first_due=date(2026, 1, 1), amount=D('250'), income_account=ledger.account(self.prop, '4500'))
        base.update(kw)
        return ChargeRule.objects.create(**base)

    def row(self):
        self.client.post(self.page, {'action': 'start'})
        from .models import Budget
        have = bg.versions(Budget.objects.get(association=self.prop, year=2027))
        return bg.editor_state(have['draft'], have)['assess']

    def units(self):
        from . import charges
        return len(charges.billable_units(self.prop))

    def test_a_monthly_rule_drives_the_six_months_to_come(self):
        self.rule()
        a = self.row()
        self.assertEqual(self.units(), 4)
        self.assertEqual(a['nd'], 6000.0)                                                  # July to December: 6 months x 4 units x 250
        self.assertTrue(a['scheduled'])
        self.assertEqual([m for m, v in a['sched_months']], ['Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'])
        self.assertAlmostEqual(a['projected'], a['actual'] + a['nd'], places=2)

    def test_a_quarterly_rule_counts_only_the_quarters_that_fall_in_the_months_to_come(self):
        self.rule(frequency='quarterly', amount=__import__('decimal').Decimal('700'))
        a = self.row()
        self.assertEqual(a['nd'], 5600.0)                                                  # July 1 and October 1: 2 quarters x 4 units x 700, not November or December
        self.assertEqual({m: v for m, v in a['sched_months']}['Nov'], 0.0)

    def test_a_charge_already_posted_ahead_counts_as_posted(self):
        from decimal import Decimal as D
        from . import charges
        r = self.rule(frequency='quarterly', amount=D('700'), first_due=date(2026, 1, 1))
        charges.post_rule(r, date(2026, 7, 1))
        a = self.row()
        self.assertEqual(a['nd'], 5600.0)                                                  # the July charge (2,800) is counted once, from what was posted, and October's from the rule

    def test_a_typed_figure_still_wins_over_the_schedule(self):
        self.rule()
        self.client.post(self.page, {'action': 'start'})
        r = self.client.post(reverse('accounting:budget_edit', args=[self.prop.pk, 2027]) + '?stage=draft', '{"op": "assess_projected", "value": "50000"}', content_type='application/json')
        a = r.json()['assess']
        self.assertEqual((a['manual'], a['projected'], a['scheduled']), (True, 50000.0, False))

    def test_a_special_assessment_rule_is_not_the_regular_assessment(self):
        self.rule(is_special=True)
        a = self.row()
        self.assertFalse(a['scheduled'])

    def test_with_no_schedule_it_is_the_pace_as_before(self):
        a = self.row()
        self.assertFalse(a['scheduled'])
