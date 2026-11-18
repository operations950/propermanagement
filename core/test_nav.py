"""The smart Up link: the session trail, the resolver, the tree of screens, and what the screens show (core/nav.py, core/nav_tree.py, {% up_link %})."""
import inspect
import re
from pathlib import Path
from types import SimpleNamespace

from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import get_resolver, reverse
from django.urls.resolvers import URLResolver

from . import nav, nav_tree
from .models import StaffProfile

STATIC = override_settings(STORAGES={'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                                     'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
ROOT = Path(__file__).resolve().parent.parent


def fake(view_name, path='/x/', kwargs=None, get=None, trail=None, accept='text/html', user=True, method='GET', headers=None):
    """A request as the middleware and resolver see it, with a dict for a session."""
    factory = RequestFactory()
    extra = {'HTTP_ACCEPT': accept} if accept else {}
    extra.update(headers or {})
    request = factory.get(path, get or {}, **extra) if method == 'GET' else factory.post(path, get or {}, **extra)
    request.session = {nav.TRAIL_KEY: [list(t) for t in trail]} if trail is not None else {}
    request.user = SimpleNamespace(is_authenticated=user)
    request.resolver_match = SimpleNamespace(view_name=view_name, kwargs=kwargs or {})
    return request


def page(status=200, content_type='text/html; charset=utf-8'):
    return HttpResponse('<p>hi</p>', status=status, content_type=content_type)


class TheTrailTests(SimpleTestCase):
    def trail(self, request):
        return request.session.get(nav.TRAIL_KEY, [])

    def test_a_page_visit_is_kept_with_its_address_and_arguments(self):
        r = fake('accounting:hub', '/accounting/3/', {'pk': 3})
        nav.record(r, page())
        self.assertEqual(self.trail(r), [['accounting:hub', '/accounting/3/', {'pk': '3'}]])

    def test_visiting_the_same_screen_again_replaces_the_top_entry(self):
        r = fake('close_home', '/close/', get={'month': '2026-10'})
        nav.record(r, page())
        r2 = fake('close_home', '/close/', get={'month': '2026-09'}, trail=self.trail(r))
        nav.record(r2, page())
        self.assertEqual(len(self.trail(r2)), 1)
        self.assertIn('2026-09', self.trail(r2)[0][1])

    def test_different_screens_stack_and_going_back_to_a_screen_adds_it_again(self):
        r = fake('close_home', '/close/')
        nav.record(r, page())
        for name, path in (('accounting:hub', '/accounting/3/'), ('close_home', '/close/')):
            r2 = fake(name, path, trail=self.trail(r))
            nav.record(r2, page())
            r.session = r2.session
        self.assertEqual([t[0] for t in self.trail(r)], ['close_home', 'accounting:hub', 'close_home'])

    def test_only_real_full_page_visits_count(self):
        for label, request, response in [
            ('a POST', fake('close_home', method='POST'), page()),
            ('a background fetch', fake('close_home', headers={'HTTP_X_REQUESTED_WITH': 'XMLHttpRequest'}), page()),
            ('an htmx call', fake('close_home', headers={'HTTP_HX_REQUEST': 'true'}), page()),
            ('no HTML accepted', fake('close_home', accept=''), page()),
            ('a frame', fake('close_home', headers={'HTTP_SEC_FETCH_DEST': 'iframe'}), page()),
            ('a redirect', fake('close_home'), page(302)),
            ('an error page', fake('close_home'), page(404)),
            ('a PDF', fake('close_home'), page(content_type='application/pdf')),
            ('a download', fake('close_home'), HttpResponse('x', content_type='text/html', headers={'Content-Disposition': 'attachment; filename=a.html'})),
            ('a visitor not signed in', fake('close_home', user=False), page()),
        ]:
            nav.record(request, response)
            self.assertEqual(self.trail(request), [], label)

    def test_a_person_who_navigates_to_a_page_is_recorded_when_the_browser_says_document(self):
        r = fake('close_home', headers={'HTTP_SEC_FETCH_DEST': 'document'})
        nav.record(r, page())
        self.assertEqual(len(self.trail(r)), 1)

    def test_the_trail_keeps_only_the_last_fifteen(self):
        r = fake('s0', '/s0/')
        for i in range(25):
            r2 = fake(f's{i}', f'/s{i}/', trail=self.trail(r))
            nav.record(r2, page())
            r.session = r2.session
        got = self.trail(r)
        self.assertEqual(len(got), nav.TRAIL_MAX)
        self.assertEqual(got[-1][0], 's24')


class TheResolverTests(SimpleTestCase):
    HUB = ['accounting:hub', '/accounting/3/', {'pk': '3'}]
    CLOSE = ['accounting:close', '/accounting/3/close/2026-10/', {'pk': '3', 'month': '2026-10'}]
    LIST = ['close_home', '/close/?month=2026-10', {}]
    PARENTS = {'accounting:close': 'Month-end close', 'accounting:hub': 'Accounting'}
    DEFAULT = ('/accounting/3/', 'Accounting')

    def test_the_wide_screen_you_came_from_wins_whichever_it_was(self):
        late = fake('accounting:late_fees', '/accounting/3/late-fees/', {'pk': 3})
        late.session[nav.TRAIL_KEY] = [self.HUB, self.CLOSE]
        got = nav.pick(late, self.PARENTS, self.DEFAULT)
        self.assertEqual((got.url, got.label, got.from_trail), ('/accounting/3/close/2026-10/', 'Month-end close', True))
        late.session[nav.TRAIL_KEY] = [self.CLOSE, self.HUB]
        got = nav.pick(late, self.PARENTS, self.DEFAULT)
        self.assertEqual((got.url, got.label), ('/accounting/3/', 'Accounting'))

    def test_a_visit_to_the_screen_itself_does_not_count_as_where_you_came_from(self):
        late = fake('accounting:late_fees', '/accounting/3/late-fees/', {'pk': 3}, trail=[self.HUB, ['accounting:late_fees', '/accounting/3/late-fees/', {'pk': '3'}]])
        late.session[nav.TRAIL_KEY] = [self.HUB, ['accounting:late_fees', '/accounting/3/late-fees/', {'pk': '3'}]]
        self.assertEqual(nav.pick(late, self.PARENTS, self.DEFAULT).label, 'Accounting')

    def test_a_screen_that_is_not_one_of_the_parents_is_skipped(self):
        late = fake('accounting:late_fees', '/accounting/3/late-fees/', {'pk': 3}, trail=[self.CLOSE, ['ticket_detail', '/tickets/9/', {'pk': '9'}]])
        got = nav.pick(late, self.PARENTS, self.DEFAULT)
        self.assertEqual(got.label, 'Month-end close')

    def test_another_associations_screen_is_never_used(self):
        other = ['accounting:hub', '/accounting/4/', {'pk': '4'}]
        late = fake('accounting:late_fees', '/accounting/3/late-fees/', {'pk': 3}, trail=[other])
        got = nav.pick(late, self.PARENTS, self.DEFAULT)
        self.assertEqual((got.url, got.from_trail), ('/accounting/3/', False))

    def test_another_months_screen_is_skipped_too(self):
        sept = fake('accounting:bank', '/accounting/3/bank/7/2026-09/', {'pk': 3, 'bank_pk': 7, 'month': '2026-09'}, trail=[self.CLOSE])
        got = nav.pick(sept, {'accounting:close': 'Month-end close'}, ('/accounting/3/close/2026-09/', 'Month-end close'))
        self.assertEqual((got.url, got.from_trail), ('/accounting/3/close/2026-09/', False))

    def test_the_month_on_the_screen_you_leave_goes_with_you(self):
        screen = fake('tourist_tax', '/close/tourist-tax/', get={'month': '2026-09'}, trail=[self.LIST])
        got = nav.pick(screen, {'close_home': 'Month-end checklist'}, ('/close/', 'Month-end checklist'))
        self.assertEqual(got.url, '/close/?month=2026-09')

    def test_a_bookmark_falls_back_to_the_usual_parent(self):
        late = fake('accounting:late_fees', '/accounting/3/late-fees/', {'pk': 3}, trail=[])
        got = nav.pick(late, self.PARENTS, self.DEFAULT)
        self.assertEqual((got.url, got.label, got.from_trail), ('/accounting/3/', 'Accounting', False))

    def test_a_label_can_be_worked_out_from_the_request(self):
        r = fake('accounting:unit', '/accounting/3/unit/5/', {'pk': 3, 'unit_pk': 5}, trail=[['accounting:units', '/accounting/3/units/', {'pk': '3'}]])
        got = nav.pick(r, {'accounting:units': lambda request, ctx: 'Units & tenants'}, ('/x/', 'Back'))
        self.assertEqual(got.label, 'Units & tenants')

    def test_nowhere_to_go_is_an_empty_up(self):
        r = fake('x', trail=[])
        self.assertFalse(nav.pick(r, {'a': 'A'}, None))

    def test_a_usual_parent_is_built_from_the_current_address(self):
        r = fake('onsite_performance_calendar', '/onsite/performance/7/calendar/', {'property_id': 7}, trail=[])
        got = nav.pick(r, {}, nav.D('onsite_performance_property', 'Property performance', args={'property_id': 'property_id'}))
        self.assertEqual(got.url, reverse('onsite_performance_property', args=[7]))
        r = fake('tourist_tax', '/close/tourist-tax/', get={'month': '2026-08'}, trail=[])
        self.assertEqual(nav.pick(r, {}, nav.D('close_home', 'Month-end checklist', query=('month',))).url, reverse('close_home') + '?month=2026-08')
        self.assertEqual(nav.pick(r, {}, nav.D('ws_compliance', 'Compliance rules', fixed={'tab': 'rules'})).url, reverse('ws_compliance') + '?tab=rules')

    def test_the_usual_parent_may_depend_on_the_person(self):
        r = fake('session_detail', '/compliance/3/', {'pk': 3}, trail=[])
        r.user = SimpleNamespace(is_authenticated=True, staff_profile=SimpleNamespace(new_face=True))
        self.assertEqual(nav.resolve(r).label, 'Compliance')
        r.user = SimpleNamespace(is_authenticated=True, staff_profile=SimpleNamespace(new_face=False))
        self.assertEqual(nav.resolve(r).label, 'My Recurring')


def _all_url_names():
    names = set()

    def walk(patterns, prefix=''):
        for p in patterns:
            if isinstance(p, URLResolver):
                walk(p.url_patterns, prefix + (p.namespace + ':' if p.namespace else ''))
            elif p.name:
                names.add(prefix + p.name)
    walk(get_resolver().url_patterns)
    return names


class TheTreeTests(SimpleTestCase):
    """core/nav_tree.py walked end to end: the rules every screen's place in it must keep."""

    def test_every_screen_in_the_tree_is_a_real_url_and_so_is_every_parent_and_usual_parent(self):
        real = _all_url_names()
        for name, node in nav_tree.SCREENS.items():
            self.assertIn(name, real, name)
            for parent, _ in node.parents:
                self.assertIn(parent, real, f'{name} lists {parent}')
                self.assertIn(parent, nav_tree.SCREENS, f'{name} lists {parent}, which has no place of its own in the tree')
            if isinstance(node.usual, dict):
                self.assertIn(node.usual['name'], real, f'{name} usual parent')

    def test_there_is_no_cycle_and_no_screen_is_its_own_ancestor(self):
        for name in nav_tree.SCREENS:
            self.assertNotIn(name, nav_tree.ancestors(name), f'{name} is above itself')
            for above in nav_tree.ancestors(name):
                self.assertNotIn(name, nav_tree.ancestors(above), f'{name} and {above} are above each other')

    def test_a_parent_is_a_wide_screen_with_a_label_or_the_record_a_form_hangs_off(self):
        for name, node in nav_tree.SCREENS.items():
            for parent, edge_label in node.parents:
                above = nav_tree.SCREENS[parent]
                self.assertTrue(edge_label or above.label, f'{name} goes up to {parent}, which has no label')
                if above.kind == nav_tree.DETAIL:
                    self.assertEqual(node.kind, nav_tree.FORM, f'{name} ({node.kind}) lists the detail screen {parent}: only a form or page that belongs to that record may')

    def test_a_detail_screen_is_the_bottom_level_so_nothing_but_its_own_forms_is_beneath_it(self):
        for name, node in nav_tree.SCREENS.items():
            for above in nav_tree.ancestors(name):
                if nav_tree.SCREENS[above].kind == nav_tree.DETAIL:
                    self.assertEqual(node.kind, nav_tree.FORM, f'{above} (a detail screen) is above {name}')

    def test_screens_that_sit_beside_each_other_are_never_ancestors_of_each_other(self):
        pairs = [('accounting:late_fees', 'accounting:collections'), ('accounting:late_fees', 'accounting:reserve_check'), ('accounting:reserve_check', 'accounting:send_board'),
                 ('close_payouts', 'tourist_tax'), ('close_overview', 'close_long_term'), ('onsite_performance', 'onsite_reservation_list'),
                 ('accounting:units', 'accounting:charges'), ('accounting:contracts', 'accounting:recurring')]
        for a, b in pairs:
            self.assertNotIn(a, nav_tree.ancestors(b), f'{a} is above {b}')
            self.assertNotIn(b, nav_tree.ancestors(a), f'{b} is above {a}')

    def test_every_screen_that_ever_had_a_hand_written_up_link_is_in_the_tree(self):
        """The screens that listed their own parents before the tree existed (accounting's acc_up, the old registry, the views that called nav.up)."""
        before = """
            accounting:hub accounting:close_review accounting:close accounting:units accounting:units_import accounting:unit accounting:owner accounting:shares accounting:charges
            accounting:charge_new accounting:charge_edit accounting:charge_one accounting:receipt_new accounting:budget_year accounting:budget_project accounting:budget_packet
            accounting:budget_load accounting:master_chart accounting:reserve_check accounting:send_board accounting:collections_home accounting:collections accounting:late_fees
            accounting:contracts accounting:contracts_print accounting:delinquencies accounting:case accounting:bank accounting:deposits accounting:bills accounting:bill accounting:pay
            accounting:checks accounting:check_setup accounting:recurring accounting:journal accounting:reports accounting:report_tb accounting:report_bs accounting:report_pl
            accounting:report_bva accounting:report_is accounting:report_gl accounting:report_aging accounting:report_payables accounting:report_checks accounting:report_rec
            accounting:budget accounting:payables_home accounting:settings commercial:leases commercial:lease_new commercial:lease_edit commercial:lease commercial:invoices
            commercial:owners commercial:cam commercial:cam_pool_new commercial:cam_pool commercial:cam_reconcile commercial:ltr_leases commercial:ltr_lease commercial:renewals
            bot_keys property_data_import quickbooks_accounts staff_create owner_portal_admin close_bank_tieout close_long_term close_overview close_escrow close_misc close_pay_owners
            close_pay_proper close_pay_ltr_owners close_rent_collection close_sales_tax tourist_tax close_payouts missing_documents faq_review_queue property_create property_edit
            property_detail contact_detail contact_create contact_edit onsite_booking_feeds onsite_calendar onsite_checklist_custom_items onsite_checklist_templates onsite_cleaner_activity
            onsite_cleaning_payments onsite_visit_create onsite_visit_rule_list onsite_visit_detail onsite_checklist_template_detail onsite_booking_import onsite_booking_import_preview
            onsite_feeds_recalibrate onsite_performance onsite_rentals_calendar onsite_reservation_list onsite_performance_property onsite_performance_calendar
            onsite_reservation_review onsite_reservation_create onsite_reservation_edit ticket_create department_dashboard ticket_previous_conversation ticket_detail timeclock_policy
            timeclock_change timeclock_time_off timeclock_fix timeclock_punch session_detail session_template_detail session_template_edit session_template_create
            process_template_create process_template_edit process_template_preview ws_people portfolio_business_detail portfolio_task_detail supplies:order_detail
            close_property close_unit close_statement close_month_statement close_pay_booking legalforms:list legalforms:new legalforms:edit legalforms:detail ws_run_detail
        """.split()
        self.assertEqual([n for n in before if n not in nav_tree.SCREENS], [])

    def test_every_view_that_works_out_an_up_link_is_a_screen_in_the_tree(self):
        """A view that calls acc_up() or nav.up() must have its url name in the tree, or the trail is ignored and Up only ever goes to its usual parent."""
        import ast
        wanted = {}

        def walk(patterns, prefix=''):
            for p in patterns:
                if isinstance(p, URLResolver):
                    walk(p.url_patterns, prefix + (p.namespace + ':' if p.namespace else ''))
                elif p.name:
                    callback = p.callback
                    while hasattr(callback, '__wrapped__'):
                        callback = callback.__wrapped__
                    wanted.setdefault((callback.__module__, callback.__name__), []).append(prefix + p.name)
        walk(get_resolver().url_patterns)
        called = []
        for path in ROOT.glob('*/views*.py'):
            source = path.read_text(encoding='utf-8')
            if 'acc_up(' not in source and 'nav.up(' not in source:
                continue
            module = '.'.join(path.relative_to(ROOT).with_suffix('').parts)
            for fn in [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.FunctionDef)]:
                body = ast.get_source_segment(source, fn) or ''
                if 'acc_up(' in body or 'nav.up(' in body:
                    called.append((module, fn.name, wanted.get((module, fn.name), [])))
        self.assertGreater(len(called), 40)
        unregistered = [f'{m}.{f}: {names}' for m, f, names in called if names and not any(n in nav_tree.SCREENS for n in names)]
        self.assertEqual(unregistered, [])

    def test_late_fees_and_collections_are_steps_beside_each_other_so_up_never_goes_between_them(self):
        self.assertNotIn('accounting:collections', nav_tree.allowed('accounting:late_fees'))
        self.assertNotIn('accounting:late_fees', nav_tree.allowed('accounting:collections'))
        self.assertEqual(sorted(k for k in nav_tree.allowed('accounting:late_fees') if k.startswith('accounting:')), ['accounting:close', 'accounting:home', 'accounting:hub'])

    def test_a_close_step_of_the_checklist_is_below_the_checklist_and_the_financials_screen(self):
        self.assertEqual(set(nav_tree.allowed('tourist_tax')), {'close_home', 'ws_financials', 'ws_line'})       # and the line page it is a card on
        self.assertEqual(nav_tree.allowed('accounting:reserve_check')['close_home'], 'Month-end checklist')

    def test_the_usual_parent_of_a_screen_without_one_written_is_its_first_parent(self):
        self.assertEqual(nav_tree.usual('accounting:deposits')['name'], 'accounting:bank')
        self.assertIsNone(nav_tree.usual('ws_home'))

    def test_no_screen_still_has_a_hand_written_up_link(self):
        """Every Up link is the shared one ({% up_link %}); an in-screen switch is marked data-no-back."""
        left = []
        pattern = re.compile(r'<a\b[^>]*>(?:(?!</a>).)*?data-lucide="arrow-up"(?:(?!</a>).)*?</a>', re.S)
        for template in ROOT.glob('*/templates/**/*.html'):
            text = template.read_text(encoding='utf-8')
            for m in pattern.finditer(text):
                if 'data-no-back' not in m.group(0):
                    left.append(f'{template.relative_to(ROOT)}: {m.group(0)[:80]}')
        self.assertEqual(left, [])


@STATIC
class WhatTheScreensShowTests(TestCase):
    def setUp(self):
        from accounting.tests import make_assoc, staff_admin
        staff_admin('mgr')
        self.client.login(username='mgr', password='pw12345678')
        self.prop = make_assoc('Test Condos')
        from accounting import ledger
        prof = ledger.profile_for(self.prop)
        prof.late_fee_kind, prof.late_fee_flat, prof.late_fee_confirmed = 'flat', 25, True
        prof.save()

    def get(self, url, **kw):
        return self.client.get(url, HTTP_ACCEPT='text/html', **kw)

    def test_late_fees_goes_up_to_the_screen_you_opened_it_from(self):
        late = reverse('accounting:late_fees', args=[self.prop.pk])
        close = reverse('accounting:close', args=[self.prop.pk, '2026-10'])
        hub = reverse('accounting:hub', args=[self.prop.pk])
        self.get(close)
        response = self.get(late)
        self.assertEqual((response.context['acc_back'], response.context['acc_back_label']), (close, 'Month-end close'))
        self.assertContains(response, f'href="{close}"')
        self.get(hub)
        response = self.get(late)
        self.assertEqual((response.context['acc_back'], response.context['acc_back_label']), (hub, 'Accounting'))

    def test_the_trail_survives_a_month_arrow_and_a_reload_of_the_same_screen(self):
        late = reverse('accounting:late_fees', args=[self.prop.pk])
        close = reverse('accounting:close', args=[self.prop.pk, '2026-10'])
        self.get(close)
        self.get(late + '?as_of=2026-10-31')
        self.get(late + '?as_of=2026-10-20')
        response = self.get(late)
        self.assertEqual(response.context['acc_back'], close)

    def test_the_phone_back_button_gets_the_same_address(self):
        late = reverse('accounting:late_fees', args=[self.prop.pk])
        close = reverse('accounting:close', args=[self.prop.pk, '2026-10'])
        self.get(close)
        html = self.get(late).content.decode()
        self.assertRegex(html, r'<span id="mBackUrl" hidden>\s*' + re.escape(close) + r'\s*</span>')

    def test_a_close_step_goes_up_to_the_checklist_you_came_from_with_its_month(self):
        checklist = reverse('close_home') + '?month=2026-09'
        self.get(checklist)
        html = self.get(reverse('tourist_tax') + '?month=2026-09').content.decode()
        self.assertIn(f'href="{checklist}"', html)
        self.assertIn('Month-end checklist', html)

    def test_a_bookmark_goes_to_the_usual_parent(self):
        html = self.get(reverse('bot_keys')).content.decode()
        self.assertIn(f'href="{reverse("admin_tools")}"', html)
        self.assertIn('Admin Tools', html)

    def test_a_property_page_goes_up_to_the_list_it_came_from_and_never_to_a_detail_screen(self):
        from .models import Property
        prop = Property.objects.get(pk=self.prop.pk)
        self.get(reverse('close_overview'))
        self.get(reverse('contact_list'))
        html = self.get(reverse('property_detail', args=[prop.pk])).content.decode()
        self.assertIn(f'href="{reverse("contact_list")}"', html)
        self.assertEqual(self.client.session[nav.TRAIL_KEY][-1][0], 'property_detail')


@STATIC
class TheTicketPageTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        from core.models import Property
        from tickets.models import Priority, Ticket
        user = get_user_model().objects.create_user('pm', password='pw12345678', first_name='Pat')
        self.staff = StaffProfile.objects.create(user=user, role=StaffProfile.Role.PROPERTY_MANAGER, new_face=False)
        self.client.login(username='pm', password='pw12345678')
        self.prop = Property.objects.create(name='Grand Condos', property_type=Property.Type.ASSOCIATION, is_active=True)
        self.ticket = Ticket.objects.create(title='Roof leak', created_by=user, source=Ticket.Source.MANUAL, status=Ticket.Status.OPEN, priority=Priority.URGENT,
                                            assigned_role=StaffProfile.Role.PROPERTY_MANAGER, assigned_staff=self.staff, property=self.prop)

    def get(self, url):
        return self.client.get(url, HTTP_ACCEPT='text/html')

    def test_the_ticket_goes_up_to_the_filtered_list_it_was_opened_from(self):
        listing = reverse('ticket_list') + '?priority=urgent'
        self.get(listing)
        response = self.get(reverse('ticket_detail', args=[self.ticket.pk]))
        self.assertEqual(response.context['back_url'], listing)
        self.assertEqual(response.context['up'].label, 'Tickets')

    def test_a_ticket_opened_from_a_dashboard_goes_up_to_that_dashboard(self):
        self.get(reverse('dashboard'))
        response = self.get(reverse('ticket_detail', args=[self.ticket.pk]))
        self.assertEqual(response.context['up'].url, reverse('dashboard'))

    def test_a_ticket_with_no_trail_still_returns_to_the_saved_filters(self):
        self.client.get(reverse('ticket_list') + '?priority=urgent')           # no Accept header: not on the trail, but the filter memory still works
        response = self.client.get(reverse('ticket_detail', args=[self.ticket.pk]))
        self.assertEqual(response.context['back_url'], reverse('ticket_list') + '?priority=urgent')


@STATIC
class TheCommercialScreensTests(TestCase):
    def setUp(self):
        from accounting.tests import staff_admin
        from commercial.tests import bank_for, make_commercial, make_lease
        staff_admin('mgr')
        self.client.login(username='mgr', password='pw12345678')
        self.prop = make_commercial()
        bank_for(self.prop)
        self.lease = make_lease(self.prop)

    def get(self, url):
        return self.client.get(url, HTTP_ACCEPT='text/html')

    def test_the_tenant_page_goes_up_to_the_wide_screen_it_was_opened_from(self):
        leases = reverse('commercial:leases', args=[self.prop.pk])
        invoices = reverse('commercial:invoices', args=[self.prop.pk])
        lease = reverse('commercial:lease', args=[self.prop.pk, self.lease.pk])
        self.get(leases)
        response = self.get(lease)
        self.assertEqual((response.context['acc_back'], response.context['acc_back_label']), (leases, 'Leases'))
        self.get(invoices)
        response = self.get(lease)
        self.assertEqual((response.context['acc_back'], response.context['acc_back_label']), (invoices, 'Tenant invoices'))

    def test_the_tenant_pages_edit_form_goes_up_to_the_tenant_and_the_list_goes_up_to_the_hub_or_the_close(self):
        lease = reverse('commercial:lease', args=[self.prop.pk, self.lease.pk])
        self.get(lease)
        response = self.get(reverse('commercial:lease_edit', args=[self.prop.pk, self.lease.pk]))
        self.assertEqual((response.context['acc_back'], response.context['acc_back_label']), (lease, self.lease.tenant.name))
        close = reverse('accounting:close', args=[self.prop.pk, '2026-10'])
        self.get(close)
        response = self.get(reverse('commercial:leases', args=[self.prop.pk]))
        self.assertEqual((response.context['acc_back'], response.context['acc_back_label']), (close, 'Month-end close'))
        self.get(reverse('accounting:hub', args=[self.prop.pk]))
        response = self.get(reverse('commercial:leases', args=[self.prop.pk]))
        self.assertEqual(response.context['acc_back_label'], 'Accounting')
        self.assertContains(response, f'href="{reverse("accounting:hub", args=[self.prop.pk])}"')

    def test_a_bookmarked_tenant_page_goes_to_the_leases_list(self):
        response = self.get(reverse('commercial:lease', args=[self.prop.pk, self.lease.pk]))
        self.assertEqual((response.context['acc_back'], response.context['acc_back_label']), (reverse('commercial:leases', args=[self.prop.pk]), 'Leases'))

    def test_the_year_on_a_cam_screen_goes_up_with_it(self):
        screen = fake('commercial:cam_pool', '/commercial/3/cam/pool/new/', {'pk': 3}, get={'year': '2025'}, trail=[['commercial:cam', '/commercial/3/cam/?year=2026', {'pk': '3'}]])
        got = nav.pick(screen, {'commercial:cam': 'CAM 2025'}, ('/commercial/3/cam/?year=2025', 'CAM 2025'))
        self.assertEqual(got.url, '/commercial/3/cam/?year=2025')
