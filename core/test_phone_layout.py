"""The shared phone layout: the rules and the script live in the base template (so no screen needs its own), the main record lists opt in with table.ac-stack, and the key screens still render."""
import os
from datetime import date

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from accounting import charges, ledger
from accounting.models import Charge
from accounting.tests import D, bank_for, make_assoc, owner_of
from core.models import Unit

STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}
ROOT = settings.BASE_DIR


def source(*parts):
    with open(os.path.join(ROOT, *parts), encoding='utf-8') as f:
        return f.read()


class SharedRulesTests(TestCase):
    def test_the_base_template_carries_the_phone_rules_and_the_script(self):
        base = source('templates', 'base.html')
        for token in ('table.ac-stack', '.table-scroll', '.is-wide', 'content: attr(data-label)', 'min-height: 44px', 'window.acPhoneTables',
                      'overflow-x: clip', '.ac-row-wrap', 'Tick all'):
            self.assertIn(token, base)
        # every rule sits inside the phone media query, so the desktop screens are not touched
        css = base[base.index('/* ---------- Phone layout'):base.index('</style>', base.index('/* ---------- Phone layout'))]
        self.assertIn('@media (max-width: 768px)', css)
        self.assertNotIn('@media (min-width', css)

    def test_a_stacked_table_hides_its_heading_without_removing_it(self):
        base = source('templates', 'base.html')
        # the heading is clipped, not display:none, so the script can still read its words for the labels and a screen reader still has them
        self.assertIn('table.ac-stack > thead { position: absolute;', base)
        self.assertNotIn('table.ac-stack > thead { display: none', base)

    def test_the_main_record_lists_opt_in_to_cards_on_a_phone(self):
        for parts in (('accounting', 'templates', 'accounting', 'collections.html'), ('accounting', 'templates', 'accounting', 'delinquencies.html'),
                      ('accounting', 'templates', 'accounting', 'late_fees.html'), ('accounting', 'templates', 'accounting', 'units.html'),
                      ('commercial', 'templates', 'commercial', 'invoices.html'), ('commercial', 'templates', 'commercial', 'leases.html')):
            self.assertIn('ac-stack', source(*parts), parts[-1])

    def test_the_small_buttons_in_the_accounting_pages_are_44px_on_a_phone(self):
        self.assertIn('.btn-sm, .btn-group-sm > .btn { min-height: 44px', source('accounting', 'templates', 'accounting', 'base_acc.html'))


@override_settings(STORAGES=STORAGES)
class KeyScreensStillRenderTests(TestCase):
    def setUp(self):
        self.prop = make_assoc()
        self.bank = bank_for(self.prop)
        prof = ledger.profile_for(self.prop)
        prof.late_fee_kind, prof.late_fee_flat, prof.late_fee_percent, prof.late_fee_grace_days, prof.late_fee_confirmed = 'greater', D('25'), D('5'), 10, True
        prof.interest_rate, prof.interest_confirmed = D('18'), True
        prof.save()
        unit = Unit.objects.filter(property=self.prop).order_by('label').first()
        owner = owner_of(self.prop, unit, 'Pat Owner')
        charges.post_charge(self.prop, unit, owner, Charge.Kind.ASSESSMENT, 'Monthly assessment', date(2026, 10, 1), D('615'))
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))

    def test_the_lists_render_as_stackable_tables_with_their_columns_intact(self):
        pk = self.prop.pk
        for name, extra in (('accounting:late_fees', '?as_of=2026-10-31'), ('accounting:units', ''), ('accounting:collections', ''), ('accounting:delinquencies', '')):
            r = self.client.get(reverse(name, args=[pk]) + extra)
            self.assertEqual(r.status_code, 200, name)
            html = r.content.decode()
            if name in ('accounting:late_fees', 'accounting:units'):
                self.assertIn('ac-stack', html, name)
            self.assertIn('window.acPhoneTables', html, name)               # the script that labels the cards and wraps the wide tables is on the page

    def test_the_wide_report_tables_are_on_the_page_with_the_scroll_box_class_the_script_looks_for(self):
        r = self.client.get(reverse('accounting:report_tb', args=[self.prop.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertIn('rp-wrap', r.content.decode())
