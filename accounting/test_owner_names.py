"""Owner names that go out are LEGAL names: no phone-book tags ("Ray Griffith GLEN", "Ally Amorim - SAG"). The legal name is filled from the Yardi owner list; until one is on file the contact name is
shown without its tag; a late letter is addressed from the unit's owner records (any contact type) by legal name."""
from datetime import date
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.template import Context, Template
from django.test import TestCase
from django.urls import reverse

from core.models import Contact, Property, Unit

from . import legal_names, owners
from .models import OwnerProfile, Ownership
from .tests import DelinquencyBase, make_assoc, staff_admin


class CleanNameTests(TestCase):
    def test_association_tags_come_off_the_end_of_a_contact_name(self):
        for tagged, plain in [
            ('Ray Griffith GLEN', 'Ray Griffith'), ('Ally Amorim - SAG', 'Ally Amorim'), ('Lina Varela -SAG', 'Lina Varela'), ('David Waknine- SAG Board', 'David Waknine'),
            ('Diana Cajamarca (SAG)', 'Diana Cajamarca'), ('Laman Meshadiyeva SAG', 'Laman Meshadiyeva'), ('Bohra SAG 21551', 'Bohra'), ('Richard Brandt - GRAND', 'Richard Brandt'),
            ('Janine - GLEN Secretary', 'Janine'), ('Richard Bilotti - President LS12 LEAD', 'Richard Bilotti'), ('Barbara Gordon - LSP2', 'Barbara Gordon'), ('Darcy Rydlun LSP2', 'Darcy Rydlun'),
            ('Robyn Lawson - Lapensee', 'Robyn Lawson'), ('Paul Sanders- SAGlen', 'Paul Sanders'), ('Alana Peden- LAP', 'Alana Peden'), ('Oscar Amortegui - LS2', 'Oscar Amortegui'),
        ]:
            self.assertEqual(owners.clean_name(tagged), plain, tagged)

    def test_real_names_are_left_alone(self):
        for name in ('John Grand', 'Eric & Hannele Bertin', "Anthony O'Connell", 'Linton Woods 308, LLC', 'Mary Pino/Hamou', 'Sage Glenn', 'Rich Corporation'):
            self.assertEqual(owners.clean_name(name), name)
        self.assertEqual(owners.clean_name('SAG'), 'SAG')                          # never to nothing

    def test_the_legal_name_wins_and_without_one_the_tag_is_dropped(self):
        a = Contact.objects.create(name='Ray Griffith GLEN', contact_type=Contact.ContactType.OWNER)
        b = Contact.objects.create(name='Ally Amorim - SAG', contact_type=Contact.ContactType.OWNER)
        OwnerProfile.objects.create(contact=a, legal_name='Raymond J. Griffith')
        self.assertEqual(owners.display_name(a), 'Raymond J. Griffith')
        self.assertEqual(owners.display_name(b), 'Ally Amorim')
        self.assertEqual(owners.display_name(None), '')

    def test_the_template_filter_does_the_same(self):
        a = Contact.objects.create(name='Tim Scala - GLEN', contact_type=Contact.ContactType.OWNER)
        self.assertEqual(Template('{% load acc_extras %}{{ c|legal }}').render(Context({'c': a})), 'Tim Scala')


class WhereTheLegalNameShowsTests(DelinquencyBase):
    def setUp(self):
        super().setUp()
        self.owner.name = 'Late Owner - SAG'
        self.owner.save(update_fields=['name'])
        OwnerProfile.objects.create(contact=self.owner, legal_name='Lena T. Owner')
        self.client.force_login(self.staff.user)

    def test_the_late_list_and_the_units_list_show_the_legal_name_and_never_the_tag(self):
        for url in (reverse('accounting:delinquencies', args=[self.prop.pk]), reverse('accounting:units', args=[self.prop.pk]), reverse('accounting:collections', args=[self.prop.pk])):
            html = self.client.get(url).content.decode()
            self.assertNotIn('SAG', html, url)
        self.assertIn('Lena T. Owner', self.client.get(reverse('accounting:units', args=[self.prop.pk])).content.decode())

    def test_a_payment_is_recorded_under_the_legal_name(self):
        from . import charges
        receipt = charges.receive_payment(self.prop, self.late_unit, self.owner, date(2026, 11, 1), 100)
        self.assertIn('Lena T. Owner', receipt.entry.memo if receipt.entry_id else '')
        self.assertNotIn('SAG', receipt.entry.memo if receipt.entry_id else '')


class LateLetterAddressTests(DelinquencyBase):
    def test_the_letter_is_addressed_from_the_units_owner_record_whatever_the_contact_type(self):
        """La Pensee 204: the owner is a contact whose main type is association member (owner is a secondary type) and who is not linked to the unit on the contact: the letter used to come out
        with no name and fail with 'Required.'"""
        from legalforms import services
        from legalforms.models import DocumentTemplate
        person = Contact.objects.create(name='Jeff Bellin', contact_type=Contact.ContactType.ASSOCIATION_MEMBER, secondary_types=['owner'])
        unit = Unit.objects.filter(property=self.prop).order_by('label')[2]
        Ownership.objects.create(unit=unit, person=person, start_date=date(2020, 1, 1), is_billed=True)
        OwnerProfile.objects.create(contact=person, legal_name='Jeffrey Bellin')
        template = DocumentTemplate.objects.get(slug='nola')
        values = services.initial_values(template, self.prop, unit, self.staff.user)
        self.assertEqual(values['owner_name'], 'Jeffrey Bellin')
        raw = {k: '' if v is None else str(v) for k, v in values.items()}
        doc, errors = services.save_document(template, self.prop, unit, self.staff.user, raw)
        self.assertFalse(errors)
        self.assertIsNotNone(doc)

    def test_two_owners_are_both_named_and_a_tag_never_gets_into_a_letter(self):
        from legalforms import services
        a = Contact.objects.create(name='Ray Griffith GLEN', contact_type=Contact.ContactType.OWNER)
        b = Contact.objects.create(name='Pam Griffith', contact_type=Contact.ContactType.OWNER)
        unit = Unit.objects.filter(property=self.prop).order_by('label')[3]
        Ownership.objects.create(unit=unit, person=a, start_date=date(2020, 1, 1), is_billed=True)
        Ownership.objects.create(unit=unit, person=b, start_date=date(2020, 1, 1))
        self.assertEqual(services.prefill_tokens(self.prop, unit, self.staff.user)['owner_names'], 'Ray Griffith and Pam Griffith')

    def test_a_unit_with_no_owner_record_still_falls_back_to_the_contacts_linked_to_it(self):
        from legalforms import services
        unit = Unit.objects.filter(property=self.prop).order_by('label')[3]
        c = Contact.objects.create(name='Linked Only', contact_type=Contact.ContactType.OWNER)
        c.units.add(unit)
        self.assertEqual(services.prefill_tokens(self.prop, unit, self.staff.user)['owner_names'], 'Linked Only')


YARDI = {'glen': {'units': [
    {'label': '7601-123', 'owners': [{'name': 'Ray Griffith', 'placeholder': False}]},
    {'label': '7601-113', 'owners': [{'name': 'Janine Gosney', 'placeholder': False}, {'name': 'Taylor Dawes', 'placeholder': False}]},
    {'label': '7601-114', 'owners': [{'name': 'Jari Lilja', 'placeholder': False}]},
    {'label': '7601-115', 'owners': [{'name': 'Jeffrey Homer', 'placeholder': False}, {'name': 'Homer', 'placeholder': False}]},
    {'label': '7601-116', 'owners': [{'name': 'Someone Else', 'placeholder': True}]},
]}}


class FillFromYardiTests(TestCase):
    def setUp(self):
        self.prop = Property.objects.create(name='St Andrews Glen', property_type=Property.Type.ASSOCIATION)
        patch = mock.patch.object(legal_names, 'DATA', YARDI)
        patch.start()
        self.addCleanup(patch.stop)

    def owner(self, unit_label, name, billed=True):
        unit, _ = Unit.objects.get_or_create(property=self.prop, label=unit_label)
        person = Contact.objects.create(name=name, contact_type=Contact.ContactType.OWNER)
        Ownership.objects.create(unit=unit, person=person, start_date=date(2020, 1, 1), is_billed=billed)
        return person

    def test_a_tagged_contact_gets_the_clean_yardi_name(self):
        ray = self.owner('7601-123', 'Ray Griffith GLEN')
        jan = self.owner('7601-113', 'Janine - GLEN Secretary')
        got = {p.pk: n for p, prop, unit, n in legal_names.plan()['set']}
        self.assertEqual(got[ray.pk], 'Ray Griffith')
        self.assertEqual(got[jan.pk], 'Janine Gosney')                       # the right one of the two Yardi owners of that unit

    def test_a_nickname_matches_and_a_duplicate_surname_entry_is_dropped(self):
        jeff = self.owner('7601-115', 'Jeff Homer')
        got = {p.pk: n for p, prop, unit, n in legal_names.plan()['set']}
        self.assertEqual(got[jeff.pk], 'Jeffrey Homer')

    def test_a_couple_saved_as_one_contact_is_never_shortened_to_one_of_them(self):
        couple = self.owner('7601-114', 'Jari & Kajanne Lilja')
        plan = legal_names.plan()
        self.assertNotIn(couple.pk, [p.pk for p, *_ in plan['set']])
        self.assertEqual([(u, c) for p, u, c, y in plan['unmatched']], [('7601-114', 'Jari & Kajanne Lilja')])
        self.assertEqual(owners.display_name(couple), 'Jari & Kajanne Lilja')            # still presentable meanwhile

    def test_a_unit_that_is_not_in_the_yardi_list_is_reported_not_guessed(self):
        self.owner('9999', 'New Owner')
        self.assertEqual([x[1:] for x in legal_names.plan()['no_yardi']], [('9999', 'New Owner')])

    def test_a_legal_name_already_typed_is_never_overwritten(self):
        ray = self.owner('7601-123', 'Ray Griffith GLEN')
        OwnerProfile.objects.create(contact=ray, legal_name='Raymond J. Griffith')
        plan = legal_names.plan()
        self.assertEqual((plan['set'], plan['already']), ([], 1))
        legal_names.apply(plan)
        self.assertEqual(OwnerProfile.objects.get(contact=ray).legal_name, 'Raymond J. Griffith')

    def test_the_command_is_a_dry_run_until_apply(self):
        ray = self.owner('7601-123', 'Ray Griffith GLEN')
        out = StringIO()
        call_command('fill_owner_legal_names', stdout=out)
        self.assertIn('Dry run', out.getvalue())
        self.assertFalse(OwnerProfile.objects.filter(contact=ray).exclude(legal_name='').exists())
        call_command('fill_owner_legal_names', '--apply', stdout=out)
        self.assertEqual(OwnerProfile.objects.get(contact=ray).legal_name, 'Ray Griffith')
        again = StringIO()
        call_command('fill_owner_legal_names', '--apply', stdout=again)
        self.assertIn('Wrote 0', again.getvalue())                             # safe to run again
