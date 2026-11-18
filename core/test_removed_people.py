"""People who were removed (login deactivated) are not offered when picking who does a job, and a job already theirs still shows who it is."""
from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import Property, StaffProfile
from core.people import active_staff, default_admin, label
from tickets.models import DepartmentDefaultAssignee, Ticket
from worksessions.models import Frequency, SessionTemplate


def person(username, role='admin', active=True, **kw):
    user = get_user_model().objects.create_user(username, password='pw12345678', first_name=username.capitalize(), is_active=active)
    return StaffProfile.objects.create(user=user, role=role, **kw)


class RemovedPeopleBase(TestCase):
    def setUp(self):
        self.boss = person('boss', is_company_admin=True, can_financial=True, can_time_admin=True)
        self.boss.user.is_superuser = True
        self.boss.user.save()
        self.hank = person('hank', 'property_manager')
        self.gail = person('gail', 'property_manager', active=False)          # removed
        self.client.force_login(self.boss.user)
        self.prop = Property.objects.create(name='9 Test St', property_type=Property.Type.SHORT_TERM_RENTAL)

    def names(self, qs):
        return [str(p) for p in qs]


class TheHelperTests(RemovedPeopleBase):
    def test_removed_people_are_left_out_and_anyone_named_in_also_stays(self):
        self.assertEqual(self.names(active_staff()), ['Boss', 'Hank'])
        self.assertEqual(self.names(active_staff(also=[self.gail.pk])), ['Boss', 'Gail', 'Hank'])
        self.assertEqual(self.names(active_staff(also=['none', '', None, str(self.gail.pk)])), ['Boss', 'Gail', 'Hank'])     # a filter value like "none" or a blank is just ignored
        self.assertEqual(self.names(active_staff(also=['none', None])), ['Boss', 'Hank'])

    def test_a_removed_person_reads_removed_and_an_active_one_does_not(self):
        self.assertTrue(self.gail.is_removed)
        self.assertFalse(self.hank.is_removed)
        self.assertEqual(label(self.gail), 'Gail (removed)')
        self.assertEqual(label(self.hank), 'Hank')

    def test_the_default_person_is_an_active_admin_never_a_removed_one(self):
        gone = person('zed', is_company_admin=True, active=False)
        StaffProfile.objects.filter(pk=self.boss.pk).update(is_company_admin=False)         # the only active admin steps down
        self.assertEqual(default_admin(), self.boss)                                          # falls to the first active person, not to the removed admin
        self.assertNotEqual(default_admin(), gone)
        self.assertNotEqual(default_admin(), self.gail)

    def test_nobody_active_means_nobody(self):
        get_user_model().objects.update(is_active=False)
        self.assertIsNone(default_admin())


class TicketPickerTests(RemovedPeopleBase):
    def ticket(self, staff):
        return Ticket.objects.create(title='Gate', property=self.prop, created_by=self.boss.user, assigned_staff=staff, assigned_role='property_manager')

    def test_the_ticket_list_does_not_offer_a_removed_person_but_keeps_the_one_filtered_to(self):
        self.ticket(self.hank)
        names = self.names(self.client.get(reverse('ticket_list')).context['staff_list'])
        self.assertIn('Hank', names)
        self.assertNotIn('Gail', names)
        filtered = self.client.get(reverse('ticket_list'), {'assigned_staff': self.gail.pk})
        self.assertIn('Gail', self.names(filtered.context['staff_list']))
        self.assertContains(filtered, 'Gail (removed)')
        self.assertNotIn('Gail', self.names(self.client.get(reverse('ticket_list'), {'assigned_staff': 'none'}).context['staff_list']))

    def test_a_ticket_still_shows_a_removed_assignee_in_the_row_editor(self):
        from django.template.loader import render_to_string
        t = self.ticket(self.gail)
        html = render_to_string('tickets/_ticket_row_edit_fields.html', {'t': t, 'staff_list': active_staff(), 'vendor_groups': [], 'request': None})
        self.assertIn('data-value="staff-%d" data-label="Gail (removed)"' % self.gail.pk, html)

    def test_reassign_offers_active_people_and_the_removed_one_already_on_the_ticket(self):
        mine = self.ticket(self.hank)
        form = self.client.get(reverse('ticket_detail', args=[mine.pk])).context['reassign_form']
        self.assertEqual(self.names(form.fields['assigned_staff'].queryset), ['Boss', 'Hank'])
        theirs = self.ticket(self.gail)
        form = self.client.get(reverse('ticket_detail', args=[theirs.pk])).context['reassign_form']
        self.assertEqual([label(p) for p in form.fields['assigned_staff'].queryset], ['Boss', 'Gail (removed)', 'Hank'])
        self.assertEqual(form.fields['assigned_staff'].label_from_instance(self.gail), 'Gail (removed)')

    def test_reassigning_to_a_removed_person_is_refused_but_saving_the_same_one_works(self):
        mine = self.ticket(self.hank)
        self.client.post(reverse('ticket_reassign', args=[mine.pk]), {'assigned_role': 'property_manager', 'assigned_staff': self.gail.pk})
        mine.refresh_from_db()
        self.assertEqual(mine.assigned_staff, self.hank)                                       # the removed person was not accepted
        theirs = self.ticket(self.gail)
        self.client.post(reverse('ticket_reassign', args=[theirs.pk]), {'assigned_role': 'maintenance', 'assigned_staff': self.gail.pk})
        theirs.refresh_from_db()
        self.assertEqual((theirs.assigned_staff, theirs.assigned_role), (self.gail, 'maintenance'))     # history unchanged, the other field saved

    def test_the_quick_assign_select_keeps_a_removed_current_assignee_instead_of_silently_picking_the_first_person(self):
        theirs = self.ticket(self.gail)
        choices = self.names(self.client.get(reverse('ticket_detail', args=[theirs.pk])).context['staff_choices'])
        self.assertIn('Gail', choices)
        html = self.client.get(reverse('ticket_detail', args=[theirs.pk])).content.decode()
        self.assertIn('selected>Gail (removed)</option>', html)
        mine = self.ticket(self.hank)
        self.assertNotIn('Gail', self.names(self.client.get(reverse('ticket_detail', args=[mine.pk])).context['staff_choices']))

    def test_a_new_ticket_is_never_automatically_given_to_a_removed_person(self):
        DepartmentDefaultAssignee.objects.create(role='maintenance', staff=self.gail)
        t = Ticket.objects.create(title='Leak', property=self.prop, created_by=self.boss.user, assigned_role='maintenance')
        self.assertNotEqual(t.assigned_staff, self.gail)
        self.assertEqual(t.assigned_staff, self.boss)                                          # falls to the active company admin
        # with a removed admin created AFTER the active one, still the active one (the first lowest pk would otherwise tie-break the other way round if the order flipped)
        person('zed', is_company_admin=True, active=False)
        self.assertEqual(Ticket.objects.create(title='Leak 2', property=self.prop, created_by=self.boss.user).assigned_staff, self.boss)

    def test_with_no_active_admin_a_ticket_is_still_made_and_goes_to_the_removed_admin_as_a_last_resort(self):
        StaffProfile.objects.filter(pk=self.boss.pk).update(is_company_admin=False)
        gone_admin = person('zed', is_company_admin=True, active=False)
        t = Ticket.objects.create(title='Leak 3', property=self.prop, created_by=self.boss.user)
        self.assertEqual(t.assigned_staff, gone_admin)


class WorkspaceAndRuleTests(RemovedPeopleBase):
    def test_compliance_and_projects_filters_leave_out_the_removed_unless_filtered_to_them(self):
        for name in ('ws_compliance', 'ws_projects'):
            self.assertEqual(self.names(self.client.get(reverse(name)).context['staff_list']), ['Boss', 'Hank'], name)
            r = self.client.get(reverse(name), {'owner': self.gail.pk})
            self.assertIn('Gail', self.names(r.context['staff_list']), name)
            self.assertContains(r, 'Gail (removed)')

    def test_the_rule_form_offers_active_people_and_keeps_the_rules_removed_owner(self):
        rule = SessionTemplate.objects.create(name='Pool check', frequency=Frequency.MONTHLY, next_open_date=date(2026, 12, 1), owner=self.gail)
        r = self.client.get(reverse('session_template_edit', args=[rule.pk]))
        self.assertIn('Gail', self.names(r.context['staff']))
        self.assertContains(r, 'Gail (removed)')
        other = SessionTemplate.objects.create(name='Gate check', frequency=Frequency.MONTHLY, next_open_date=date(2026, 12, 1), owner=self.hank)
        self.assertEqual(self.names(self.client.get(reverse('session_template_edit', args=[other.pk])).context['staff']), ['Boss', 'Hank'])
        self.assertEqual(self.names(self.client.get(reverse('session_template_create')).context['staff']), ['Boss', 'Hank'])

    def test_the_item_assign_select_keeps_a_removed_owner(self):
        from workspace import services
        from worksessions.models import Session
        rule = SessionTemplate.objects.create(name='Pool check', frequency=Frequency.MONTHLY, next_open_date=date(2026, 12, 1), owner=self.gail)
        session = Session.objects.create(template=rule, owner=self.gail, period_label='December 2026', period_key=date(2026, 12, 1), opens_at=date(2026, 12, 1))
        self.assertIn('Gail', self.names(services.item_context(session)['staff_choices']))
        session.owner = self.hank
        self.assertNotIn('Gail', self.names(services.item_context(session)['staff_choices']))


class OtherScreensTests(RemovedPeopleBase):
    def test_cleaner_report_lists_active_cleaners_and_keeps_the_one_being_looked_at(self):
        person('clara', 'cleaner')
        gone = person('gwen', 'cleaner', active=False)
        r = self.client.get(reverse('onsite_cleaner_activity'))
        self.assertEqual(self.names(r.context['cleaners']), ['Clara'])
        r = self.client.get(reverse('onsite_cleaner_activity'), {'who': f'staff:{gone.pk}'})
        self.assertEqual(self.names(r.context['cleaners']), ['Clara', 'Gwen'])
        self.assertEqual(r.context['staff'], gone)

    def test_time_clock_setup_lists_only_people_on_the_team(self):
        r = self.client.get(reverse('timeclock_setup'))
        self.assertEqual(sorted(str(row['staff']) for row in r.context['rows']), ['Boss', 'Hank'])

    def test_admin_tools_staff_table_lists_only_people_on_the_team(self):
        r = self.client.get(reverse('admin_tools'))
        self.assertEqual(self.names(r.context['staff_profiles']), ['Boss', 'Hank'])
        self.assertNotContains(r, 'Gail')

    def test_people_and_roles_still_lists_the_removed_so_they_can_be_brought_back(self):
        r = self.client.get(reverse('ws_people'))
        self.assertEqual(self.names(r.context['removed_list']), ['Gail'])


class DefaultOwnerTests(RemovedPeopleBase):
    def test_delinquency_reviews_go_to_an_active_person(self):
        from accounting import delinquency
        from accounting.models import AssociationProfile
        prop = Property.objects.create(name='Glen Test', property_type=Property.Type.ASSOCIATION)
        profile, _ = AssociationProfile.objects.get_or_create(property=prop)
        profile.compliance_owner = self.gail
        profile.save()
        self.assertNotEqual(delinquency.review_owner(prop), self.gail)
        self.assertEqual(delinquency.review_owner(prop), self.boss)
        profile.compliance_owner = self.hank
        profile.save()
        self.assertEqual(delinquency.review_owner(prop), self.hank)

    def test_the_settings_owner_picker_keeps_a_removed_owner_selected(self):
        from accounting.models import AssociationProfile
        prop = Property.objects.create(name='Glen Test', property_type=Property.Type.ASSOCIATION)
        profile, _ = AssociationProfile.objects.get_or_create(property=prop)
        profile.compliance_owner = self.gail
        profile.save()
        from accounting.views_setup import assoc_context
        self.assertIn('Gail', self.names(assoc_context(prop)['staff_choices']))
        profile.compliance_owner = None
        profile.save()
        self.assertNotIn('Gail', self.names(assoc_context(prop)['staff_choices']))

    def test_automatic_rules_are_handed_to_an_active_person(self):
        from accounting import contracts, delinquency
        StaffProfile.objects.filter(pk=self.boss.pk).update(is_company_admin=False)
        person('zed', is_company_admin=True, active=False)                                       # a removed admin with the lowest pk among admins must not be chosen
        for make in (contracts._template, delinquency._template):
            t = make()
            self.assertIsNotNone(t)
            self.assertFalse(t.owner.is_removed, make.__module__)
