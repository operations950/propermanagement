"""People and roles: the switches (Financial, Time clock management, Admin, Portfolio) decide what each person can use, on the server as well as in the menu; and the screen adds, removes and saves people."""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from core import permissions
from core.models import Contact, StaffProfile
from workspace import menu

User = get_user_model()
PW = 'pw12345678'


def person(email, role='', **flags):
    user = User.objects.create_user(username=email, email=email, password=PW, first_name=email.split('@')[0].title())
    return StaffProfile.objects.create(user=user, role=role, **flags)


def keys(client, profile):
    client.force_login(profile.user)
    class R:
        pass
    r = R()
    r.user, r.path = profile.user, '/w/home'
    return [i['key'] for i in menu.build(r, profile)]


class SwitchTests(TestCase):
    def test_each_switch_stands_alone(self):
        fin = person('fin@x.com', can_financial=True)
        clock = person('clock@x.com', can_time_admin=True)
        admin = person('adm@x.com', is_company_admin=True)
        plain = person('plain@x.com')
        self.assertEqual([(permissions.has_financial(p.user), permissions.has_time_admin(p.user), permissions.has_admin(p.user)) for p in (fin, clock, admin, plain)],
                         [(True, False, False), (False, True, False), (False, False, True), (False, False, False)])

    def test_a_superuser_has_every_switch_and_a_switched_off_login_has_none(self):
        boss = User.objects.create_superuser('boss', 'b@x.com', PW)
        self.assertTrue(all(f(boss) for f in (permissions.has_admin, permissions.has_financial, permissions.has_time_admin)))
        off = person('off@x.com', can_financial=True, is_company_admin=True, can_time_admin=True)
        off.user.is_active = False
        off.user.save()
        self.assertFalse(any(f(off.user) for f in (permissions.has_admin, permissions.has_financial, permissions.has_time_admin)))

    def test_a_new_person_is_on_the_new_layout_with_no_switches(self):
        p = person('new@x.com')
        self.assertEqual((p.new_face, p.can_financial, p.can_time_admin, p.is_company_admin, p.is_portfolio_owner), (True, False, False, False, False))


class ServerEnforcementTests(TestCase):
    def go(self, p, name, *args):
        self.client.force_login(p.user)
        return self.client.get(reverse(name, args=args), follow=False)

    def test_the_books_and_the_month_end_checklist_need_financial(self):
        plain, admin_only, fin = person('plain@x.com'), person('adm@x.com', is_company_admin=True), person('fin@x.com', can_financial=True)
        for name in ('accounting:home', 'accounting:home_commercial', 'close_home', 'commercial:renewals'):
            self.assertEqual(self.go(plain, name).status_code, 302, name)
            self.assertEqual(self.go(admin_only, name).status_code, 302, name)          # Admin does not include the books
            self.assertEqual(self.go(fin, name).status_code, 200, name)

    def test_admin_tools_and_people_need_admin_not_financial(self):
        fin, admin = person('fin@x.com', can_financial=True), person('adm@x.com', is_company_admin=True)
        self.assertEqual(self.go(fin, 'admin_tools').status_code, 302)
        self.assertEqual(self.go(admin, 'admin_tools').status_code, 200)
        self.assertEqual(self.go(fin, 'ws_people').status_code, 302)
        self.assertEqual(self.go(admin, 'ws_people').status_code, 200)

    def test_the_time_clock_management_needs_its_own_switch(self):
        admin, clock = person('adm@x.com', is_company_admin=True), person('clock@x.com', can_time_admin=True)
        self.assertEqual(self.go(admin, 'timeclock_manage').status_code, 302)
        self.assertEqual(self.go(clock, 'timeclock_manage').status_code, 200)

    def test_properties_contacts_tickets_and_legal_forms_are_open_to_everyone(self):
        plain = person('plain@x.com', role='cleaner')
        for name in ('property_list', 'contact_list', 'ticket_list', 'legalforms:list'):
            self.assertEqual(self.go(plain, name).status_code, 200, name)

    def test_the_financials_screen_is_for_financial(self):
        plain, fin = person('plain@x.com'), person('fin@x.com', can_financial=True)
        self.assertEqual(self.go(plain, 'ws_financials').status_code, 302)
        self.assertEqual(self.go(fin, 'ws_financials').status_code, 200)


class MenuTests(TestCase):
    def test_everyone_sees_the_team_items_and_commercial(self):
        plain = person('plain@x.com', role='cleaner')
        got = keys(self.client, plain)
        for k in ('home', 'tickets', 'properties', 'contacts', 'legal', 'time', 'compliance', 'projects', 'onsite'):
            self.assertIn(k, got)
        for k in ('financials', 'admin', 'dashboard', 'portfolio'):
            self.assertNotIn(k, got)
        self.client.force_login(plain.user)
        self.assertIn('Commercial', ' '.join(l['label'] for l in menu.lines(type('R', (), {'path': '/w/home'}), plain)))

    def test_the_switches_add_their_menu_items(self):
        self.assertIn('financials', keys(self.client, person('fin@x.com', can_financial=True)))
        admin = keys(self.client, person('adm@x.com', is_company_admin=True))
        self.assertIn('admin', admin)
        self.assertIn('dashboard', admin)
        self.assertNotIn('financials', admin)
        self.assertIn('portfolio', keys(self.client, person('port@x.com', is_portfolio_owner=True)))


class PeopleScreenTests(TestCase):
    def setUp(self):
        self.me = person('me@x.com', is_company_admin=True, can_financial=True)
        self.client.force_login(self.me.user)
        self.url = reverse('ws_people')

    def test_adding_a_person_makes_the_login_the_profile_and_a_contact(self):
        r = self.client.post(self.url, {'action': 'add', 'first_name': 'Dana', 'last_name': 'Lee', 'email': 'dana@x.com', 'role': 'maintenance', 'timezone': 'America/New_York', 'password': 'Temp-pass-123'})
        self.assertRedirects(r, self.url)
        p = StaffProfile.objects.get(user__email='dana@x.com')
        self.assertEqual((p.role, p.can_financial, p.can_time_admin, p.is_company_admin, p.new_face), ('maintenance', False, False, False, True))
        self.assertTrue(p.user.check_password('Temp-pass-123'))
        self.assertTrue(Contact.objects.filter(email='dana@x.com').exists())

    def test_a_duplicate_email_is_refused(self):
        self.client.post(self.url, {'action': 'add', 'first_name': 'Me', 'email': 'me@x.com', 'timezone': 'America/New_York', 'password': 'Temp-pass-123'})
        self.assertEqual(User.objects.filter(email__iexact='me@x.com').count(), 1)

    def test_saving_ticks_the_switches(self):
        other = person('o@x.com')
        self.client.post(self.url, {'action': 'save', f'row_{other.pk}': '1', f'financial_{other.pk}': 'on', f'time_{other.pk}': 'on', f'dept_{other.pk}': 'accounting',
                                    f'row_{self.me.pk}': '1', f'financial_{self.me.pk}': 'on'})
        other.refresh_from_db()
        self.assertEqual((other.can_financial, other.can_time_admin, other.is_company_admin, other.role), (True, True, False, 'accounting'))
        self.me.refresh_from_db()
        self.assertTrue(self.me.is_company_admin)                      # you cannot take your own Admin away here

    def test_a_person_left_off_the_form_is_not_changed(self):
        other = person('o@x.com', can_financial=True)
        self.client.post(self.url, {'action': 'save', f'row_{self.me.pk}': '1'})
        other.refresh_from_db()
        self.assertTrue(other.can_financial)

    def test_removing_turns_the_login_off_and_keeps_the_person_and_restoring_brings_it_back(self):
        other = person('o@x.com')
        self.client.post(self.url, {'action': 'remove', 'person': other.pk})
        other.user.refresh_from_db()
        self.assertFalse(other.user.is_active)
        self.assertTrue(StaffProfile.objects.filter(pk=other.pk).exists())
        self.client.post(self.url, {'action': 'restore', 'person': other.pk})
        other.user.refresh_from_db()
        self.assertTrue(other.user.is_active)

    def test_you_cannot_remove_yourself(self):
        self.client.post(self.url, {'action': 'remove', 'person': self.me.pk})
        self.me.user.refresh_from_db()
        self.assertTrue(self.me.user.is_active)

    def test_the_screen_lists_the_columns_and_the_old_ones_are_gone(self):
        html = self.client.get(self.url).content.decode()
        for word in ('People and roles', 'Financial', 'Time clock mgmt', 'Admin', 'Portfolio', 'Add a person'):
            self.assertIn(word, html)
        self.assertNotIn('New layout', html)
        self.assertNotIn('Menu</th>', html)
