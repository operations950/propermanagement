"""The smart Up link: the session trail, the resolver, the registry, and what the screens show (core/nav.py, core/nav_map.py, {% up_link %})."""
import inspect
import re
from pathlib import Path
from types import SimpleNamespace

from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import get_resolver, reverse
from django.urls.resolvers import URLResolver

from . import nav, nav_map
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
        got = nav.up(late, self.PARENTS, self.DEFAULT)
        self.assertEqual((got.url, got.label, got.from_trail), ('/accounting/3/close/2026-10/', 'Month-end close', True))
        late.session[nav.TRAIL_KEY] = [self.CLOSE, self.HUB]
        got = nav.up(late, self.PARENTS, self.DEFAULT)
        self.assertEqual((got.url, got.label), ('/accounting/3/', 'Accounting'))

    def test_a_visit_to_the_screen_itself_does_not_count_as_where_you_came_from(self):
        late = fake('accounting:late_fees', '/accounting/3/late-fees/', {'pk': 3}, trail=[self.HUB, ['accounting:late_fees', '/accounting/3/late-fees/', {'pk': '3'}]])
        late.session[nav.TRAIL_KEY] = [self.HUB, ['accounting:late_fees', '/accounting/3/late-fees/', {'pk': '3'}]]
        self.assertEqual(nav.up(late, self.PARENTS, self.DEFAULT).label, 'Accounting')

    def test_a_screen_that_is_not_one_of_the_parents_is_skipped(self):
        late = fake('accounting:late_fees', '/accounting/3/late-fees/', {'pk': 3}, trail=[self.CLOSE, ['ticket_detail', '/tickets/9/', {'pk': '9'}]])
        got = nav.up(late, self.PARENTS, self.DEFAULT)
        self.assertEqual(got.label, 'Month-end close')

    def test_another_associations_screen_is_never_used(self):
        other = ['accounting:hub', '/accounting/4/', {'pk': '4'}]
        late = fake('accounting:late_fees', '/accounting/3/late-fees/', {'pk': 3}, trail=[other])
        got = nav.up(late, self.PARENTS, self.DEFAULT)
        self.assertEqual((got.url, got.from_trail), ('/accounting/3/', False))

    def test_another_months_screen_is_skipped_too(self):
        sept = fake('accounting:bank', '/accounting/3/bank/7/2026-09/', {'pk': 3, 'bank_pk': 7, 'month': '2026-09'}, trail=[self.CLOSE])
        got = nav.up(sept, {'accounting:close': 'Month-end close'}, ('/accounting/3/close/2026-09/', 'Month-end close'))
        self.assertEqual((got.url, got.from_trail), ('/accounting/3/close/2026-09/', False))

    def test_the_month_on_the_screen_you_leave_goes_with_you(self):
        screen = fake('tourist_tax', '/close/tourist-tax/', get={'month': '2026-09'}, trail=[self.LIST])
        got = nav.up(screen, {'close_home': 'Month-end checklist'}, ('/close/', 'Month-end checklist'))
        self.assertEqual(got.url, '/close/?month=2026-09')

    def test_a_bookmark_falls_back_to_the_usual_parent(self):
        late = fake('accounting:late_fees', '/accounting/3/late-fees/', {'pk': 3}, trail=[])
        got = nav.up(late, self.PARENTS, self.DEFAULT)
        self.assertEqual((got.url, got.label, got.from_trail), ('/accounting/3/', 'Accounting', False))

    def test_a_label_can_be_worked_out_from_the_request(self):
        r = fake('accounting:unit', '/accounting/3/unit/5/', {'pk': 3, 'unit_pk': 5}, trail=[['accounting:units', '/accounting/3/units/', {'pk': '3'}]])
        got = nav.up(r, {'accounting:units': lambda request: 'Units & tenants'}, ('/x/', 'Back'))
        self.assertEqual(got.label, 'Units & tenants')

    def test_nowhere_to_go_is_an_empty_up(self):
        r = fake('x', trail=[])
        self.assertFalse(nav.up(r, {'a': 'A'}, None))

    def test_a_usual_parent_is_built_from_the_current_address(self):
        r = fake('onsite_performance_calendar', '/onsite/performance/7/calendar/', {'property_id': 7}, trail=[])
        got = nav.up(r, {}, nav.D('onsite_performance_property', 'Property performance', args={'property_id': 'property_id'}))
        self.assertEqual(got.url, reverse('onsite_performance_property', args=[7]))
        r = fake('tourist_tax', '/close/tourist-tax/', get={'month': '2026-08'}, trail=[])
        self.assertEqual(nav.up(r, {}, nav.D('close_home', 'Month-end checklist', query=('month',))).url, reverse('close_home') + '?month=2026-08')
        self.assertEqual(nav.up(r, {}, nav.D('ws_compliance', 'Compliance rules', fixed={'tab': 'rules'})).url, reverse('ws_compliance') + '?tab=rules')

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


class TheRegistryTests(SimpleTestCase):
    def test_every_name_in_the_registry_is_a_real_screen(self):
        real = _all_url_names()
        for name, (parents, default) in nav_map.UP.items():
            self.assertIn(name, real, name)
            for parent in parents:
                self.assertIn(parent, real, f'{name} lists {parent}')
            if isinstance(default, dict):
                self.assertIn(default['name'], real, f'{name} default')

    def test_a_detail_screen_never_goes_up_to_another_detail_screen(self):
        for name in nav_map.DETAIL_VIEWS:
            entry = nav_map.UP.get(name)
            if entry is None:
                continue
            parents, default = entry
            for parent in parents:
                self.assertNotIn(parent, nav_map.DETAIL_VIEWS, f'{name} lists the detail screen {parent}')
            if isinstance(default, dict):
                self.assertNotIn(default['name'], nav_map.DETAIL_VIEWS, f'{name} defaults to a detail screen')

    def test_the_accounting_detail_screens_list_only_wide_parents(self):
        from accounting import views, views_delinquency, views_money
        detail_names = {n for n in nav_map.DETAIL_VIEWS if n.startswith('accounting:')}
        from commercial import views as commercial_views, views_cam
        detail_names |= {'commercial:lease', 'commercial:cam_pool'}
        for module, function in ((views, 'unit_detail'), (views, 'owner_detail'), (views_money, 'bill_detail'), (views_delinquency, 'case_detail'),
                                 (commercial_views, 'lease_detail'), (views_cam, 'cam_pool')):
            source = inspect.getsource(getattr(module, function))
            names = set(re.findall(r"'(accounting:\w+|commercial:\w+|ws_\w+|close_\w+)'", source.split('acc_up(', 1)[1].split('],', 1)[0]))
            self.assertTrue(names, function)
            self.assertFalse(names & detail_names, f'{function} lists {names & detail_names}')

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
        self.staff = StaffProfile.objects.create(user=user, role=StaffProfile.Role.PROPERTY_MANAGER)
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
        got = nav.up(screen, {'commercial:cam': 'CAM 2025'}, ('/commercial/3/cam/?year=2025', 'CAM 2025'))
        self.assertEqual(got.url, '/commercial/3/cam/?year=2025')
