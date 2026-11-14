from datetime import datetime, time, timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import Property, StaffProfile

from .models import Booking, Visit, VisitType

STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}
CSS = 'css/mobile-onsite.css'


@override_settings(STORAGES=STORAGES)
class PhoneOnSiteTests(TestCase):
    """The On-Site screens on a phone: big visit cards and buttons, a way up at the top of every page, the week board as cards, the cleaner's own page easy to use with one thumb.
    Everything new sits in phone-only markup or a phone-only media query, so the desktop screens read exactly as before."""

    def setUp(self):
        User = get_user_model()
        self.boss = User.objects.create_superuser('boss', 'b@example.com', 'pw12345678')
        cu = User.objects.create_user('charlotte', first_name='Charlotte', last_name='Kim', password='pw12345678')
        self.cleaner = StaffProfile.objects.create(user=cu, role=StaffProfile.Role.CLEANER, new_face=True)
        self.prop = Property.objects.create(name='Palm Cottage 12', property_type='str', street='12 Palm Way', city='Delray Beach', state='FL', zip_code='33444', gate_code='4410')
        self.vt = VisitType.objects.create(name='Turnover Clean')
        self.today = timezone.localdate()
        self.visit = Visit.objects.create(property=self.prop, visit_type=self.vt, scheduled_date=self.today, status=Visit.Status.SCHEDULED, assigned_staff=self.cleaner,
                                          ready_by=timezone.make_aware(datetime.combine(self.today, time(15, 0))))
        self.client.force_login(self.boss)

    # ---------------------------------------------------------------- the visits list
    def test_the_visits_list_has_phone_cards_with_a_maps_link_and_the_desktop_table_is_still_there(self):
        html = self.client.get(reverse('onsite_dashboard')).content.decode()
        self.assertIn(CSS, html)
        self.assertIn('class="mo-card"', html)
        self.assertIn('Ready by 3:00 PM', html)
        self.assertIn('https://www.google.com/maps/search/?api=1&amp;query=12%20Palm%20Way%2C%20Delray%20Beach%2C%20FL%2033444', html)
        self.assertIn('d-none d-md-block table-responsive', html)            # the desktop table, untouched
        self.assertIn('d-none d-md-flex', html)                              # and the desktop toolbar

    def test_the_card_opens_the_visit_and_the_day_switcher_keeps_every_range(self):
        html = self.client.get(reverse('onsite_dashboard')).content.decode()
        self.assertIn(f'href="{reverse("onsite_visit_detail", args=[self.visit.pk])}" class="mo-title stretched-link"', html)
        for key in ('yesterday', 'week', 'month'):
            self.assertEqual(html.count(f'?range={key}'), 2, key)             # once in the phone buttons, once in the desktop group
        self.assertIn('mo-seg', html)

    def test_the_phone_header_keeps_every_shortcut_the_old_menu_had(self):
        html = self.client.get(reverse('onsite_dashboard')).content.decode()
        for name in ('onsite_str_today', 'onsite_rentals_calendar', 'onsite_reservation_list', 'onsite_performance', 'onsite_checklist_templates', 'onsite_visit_rule_list', 'onsite_visit_create',
                     'onsite_checklist_custom_items', 'onsite_cleaning_payments', 'onsite_booking_feeds'):
            self.assertIn(f'class="mo-btn" href="{reverse(name)}"' if name not in ('onsite_visit_create', 'onsite_str_today') else f'href="{reverse(name)}"', html, name)

    def test_a_drilled_in_day_has_a_big_way_back_up(self):
        html = self.client.get(reverse('onsite_dashboard') + f'?date={self.today:%Y-%m-%d}').content.decode()
        self.assertIn('mo-back', html)
        self.assertIn('Today + Tomorrow', html)

    # ---------------------------------------------------------------- the visit screen
    def test_the_visit_screen_has_a_big_way_up_directions_and_keeps_its_desktop_link(self):
        html = self.client.get(reverse('onsite_visit_detail', args=[self.visit.pk])).content.decode()
        self.assertIn('class="mo-page"', html)
        self.assertIn('class="mo-back d-md-none"', html)
        self.assertIn('d-none d-md-inline-flex', html)                       # the original small link, for wide screens
        self.assertIn('mo-btn d-md-none mt-2', html)                          # Directions, phones only
        self.assertIn('query=12%20Palm%20Way', html)

    # ---------------------------------------------------------------- the cleaner's own page
    def test_the_cleaner_page_has_directions_a_full_width_start_and_still_starts_the_visit(self):
        url = reverse('onsite_visit_public', args=[self.visit.access_token])
        html = self.client.get(url).content.decode()
        self.assertIn('class="mo-page"', html)
        self.assertIn('query=12%20Palm%20Way', html)
        self.assertIn('mo-start', html)
        self.assertIn('Gate code: <strong>4410</strong>', html)
        self.assertEqual(html.count('<div class="mo-page">'), html.count('<div class="mo-page">'))
        self.client.post(url, {'action': 'start'})
        self.visit.refresh_from_db()
        self.assertIsNotNone(self.visit.started_at)

    def test_the_cleaner_checklist_sizes_only_change_at_phone_width(self):
        css = (Path(settings.BASE_DIR) / 'static' / 'css' / 'checklist-mobile.css').read_text(encoding='utf-8')
        head, _, phone = css.partition('/* Phones: the cleaner works this page')
        self.assertIn('@media (max-width: 767.98px)', phone)
        self.assertIn('min-height: 56px', phone)
        self.assertNotIn('min-height: 56px', head)                            # the original sizes above it are unchanged

    # ---------------------------------------------------------------- the properties board
    def test_the_week_board_has_a_card_per_property_with_the_same_buttons_as_the_grid(self):
        now = timezone.now()
        Booking.objects.create(property=self.prop, source='manual', external_uid='a1', guest_name='Dana', check_in=now - timedelta(days=2), check_out=now.replace(hour=11, minute=0) + timedelta(days=1),
                               listing_name='Palm Cottage')
        html = self.client.get(reverse('ws_week')).content.decode()
        self.assertIn('class="pg-phone"', html)
        self.assertIn('class="pgscroll"', html)                               # the desktop grid is still there
        phone = html.split('class="pg-phone"')[1].split('class="pgscroll"')[0]
        self.assertIn('class="pname"', phone)
        self.assertIn('data-wk-time', phone)
        self.assertIn('Check-out', phone)
        self.assertIn('data-wk-clean', phone)
        self.assertIn('Guest in house', phone)

    def test_a_property_with_nothing_this_week_says_so_and_a_cleaning_button_is_never_hidden(self):
        html = self.client.get(reverse('ws_week')).content.decode()
        self.assertIn('class="nothing"', html)
        css = (Path(settings.BASE_DIR) / 'static' / 'css' / 'mobile-onsite.css').read_text(encoding='utf-8')
        self.assertNotIn('.pg-phone .none', css)                              # a cleaning button with the class "none" must not be hidden by the empty-week rule

    # ---------------------------------------------------------------- the other On-Site pages and the stylesheet
    def test_the_other_onsite_pages_get_the_same_phone_treatment(self):
        for name in ('onsite_str_today', 'onsite_reservation_list', 'onsite_rentals_calendar', 'onsite_calendar', 'onsite_visit_create', 'onsite_cleaner_activity'):
            html = self.client.get(reverse(name)).content.decode()
            self.assertIn(CSS, html, name)
            self.assertIn('class="mo-page"', html, name)

    def test_the_phone_stylesheet_changes_existing_looks_only_inside_the_phone_media_query(self):
        css = (Path(settings.BASE_DIR) / 'static' / 'css' / 'mobile-onsite.css').read_text(encoding='utf-8')
        before, _, inside = css.partition('@media (max-width: 767.98px)')
        for selector in ('.mo-page', '.ws-sub', '.pgw .'):
            self.assertNotIn(selector, before, selector)                      # nothing that restyles an existing element sits outside the media query
        self.assertIn('.ws-sub', inside)
        self.assertIn('min-height: 48px', inside)

    def test_the_section_tabs_are_still_the_same_links(self):
        self.client.force_login(self.cleaner.user)
        html = self.client.get(reverse('onsite_dashboard')).content.decode()
        for name in ('onsite_dashboard', 'ws_week', 'onsite_str_today', 'onsite_reservation_list', 'onsite_cleaner_activity', 'onsite_performance'):
            self.assertIn(f'href="{reverse(name)}"', html, name)
