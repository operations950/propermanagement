"""The *Preparing the letter* step of Owner collections: opening a case drafts the late letter and sends nothing; the letter is made final, the affidavit prepared, the letter, affidavit and
envelope marked printed, and only a confirmed mailing date moves the case to *Letter sent*."""
from datetime import date, timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.urls import reverse

from core.models import Property, Unit
from legalforms.models import GeneratedDocument

from . import charges, delinquency, letter_prep, owner_collections
from .models import CaseEvent, CasePrep, Charge, DelinquencyCase
from .test_collections_close import CollectionsBase
from .tests import D, owner_of


class PrepBase(CollectionsBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678'))
        self.url = reverse('accounting:collections', args=[self.prop.pk])
        Property.objects.filter(pk=self.prop.pk).update(street='1 Main St', city='Boca Raton', state='FL', zip_code='33431')
        self.prop.refresh_from_db()

    def post(self, **data):
        with mock.patch('django.utils.timezone.localdate', return_value=self.today):
            return self.client.post(self.url, data, follow=True)

    def act(self, case, action, **data):
        url = reverse('accounting:prep_act', args=[self.prop.pk, case.pk])
        with mock.patch('django.utils.timezone.localdate', return_value=self.today):
            return self.client.post(url, {'action': action, **data}, follow=True)

    def open_one(self):
        self.post(action='open_cases', unit=[self.late_unit.pk], tab='none')
        return DelinquencyCase.objects.get(unit=self.late_unit)

    def late_owner(self, unit, name):
        owner = owner_of(self.prop, unit, name)
        charges.post_charge(self.prop, unit, owner, Charge.Kind.ASSESSMENT, 'Monthly assessment', date(2026, 10, 1), D('615'))
        return owner

    def messages(self, response):
        return [str(m) for m in response.context['messages']]

    def make_everything_ready(self, case, day=None):
        day = day or self.today
        self.act(case, 'finalize')
        self.act(case, 'affidavit', mailing_date=day.isoformat())
        for what in ('letter', 'affidavit', 'envelope'):
            self.act(case, 'printed', what=what)


class OpeningACaseTests(PrepBase):
    def test_opening_cases_for_three_owners_drafts_three_letters_and_sends_nothing(self):
        u2, u3 = list(Unit.objects.filter(property=self.prop).order_by('label'))[2:4]
        self.late_owner(u2, 'Second Owner')
        self.late_owner(u3, 'Third Owner')
        response = self.post(action='open_cases', unit=[self.late_unit.pk, u2.pk, u3.pk], tab='none')
        self.assertTrue(any('Opened 3 cases' in m for m in self.messages(response)))
        cases = DelinquencyCase.objects.all()
        self.assertEqual(cases.count(), 3)
        self.assertEqual({c.stage for c in cases}, {'preparing'})
        self.assertEqual(GeneratedDocument.objects.filter(template__slug='nola', status='draft').count(), 3)
        for case in cases:
            self.assertEqual(case.prep.letter_doc.status, 'draft')
            self.assertFalse(case.events.filter(kind='letter').exists())                 # nothing counts as sent
            self.assertIsNone(owner_collections.letter_sent_on(case))
        board = owner_collections.board(self.prop, self.today)
        by = {t['key']: t for t in board['tabs']}
        self.assertEqual((by['preparing']['count'], by['letter']['count']), (3, 0))
        self.assertTrue(response.redirect_chain[-1][0].endswith('?tab=preparing'))

    def test_the_tab_order_puts_preparing_between_not_in_a_case_and_letter_sent(self):
        keys = [t['key'] for t in owner_collections.board(self.prop, self.today)['tabs']]
        self.assertEqual(keys[:3], ['none', 'preparing', 'letter'])

    def test_an_owner_who_already_has_an_open_case_is_skipped_with_a_note(self):
        self.open_one()
        response = self.post(action='open_cases', unit=[self.late_unit.pk], tab='none')
        self.assertEqual(DelinquencyCase.objects.count(), 1)
        self.assertEqual(GeneratedDocument.objects.count(), 1)
        self.assertTrue(any('already has an open case' in m for m in self.messages(response)))

    def test_ticking_nobody_is_refused(self):
        response = self.post(action='open_cases', tab='none')
        self.assertTrue(any('Tick the owners first' in m for m in self.messages(response)))
        self.assertEqual(DelinquencyCase.objects.count(), 0)

    def test_the_old_action_name_still_works(self):
        self.post(action='make_letters', unit=[self.late_unit.pk], tab='none')
        self.assertEqual(DelinquencyCase.objects.get().stage, 'preparing')

    def test_the_first_tab_button_says_open_a_case(self):
        html = self.client.get(self.url + '?tab=none').content.decode()
        self.assertIn('Open a case for the selected owners', html)
        self.assertNotIn('Make late letters for the ticked', html)

    def test_the_preparing_tab_lists_the_case_with_its_checklist(self):
        self.open_one()
        html = self.client.get(self.url + '?tab=preparing').content.decode()
        for text in ('Late Owner', 'Open draft', 'Make final', 'Draft the missing letters', 'Print all letters', 'Print all affidavits', 'Print all envelopes', 'Mark mailed', 'Cancel this case'):
            self.assertIn(text, html)
        self.assertIn('ac-stack', html)


class TheChecklistTests(PrepBase):
    def test_make_final_records_who_and_when_and_reopen_takes_it_back(self):
        case = self.open_one()
        self.act(case, 'finalize')
        prep = CasePrep.objects.get(case=case)
        self.assertEqual(prep.letter_doc.status, 'final')
        self.assertEqual(prep.letter_final_by, get_user_model().objects.get(username='boss'))
        self.assertIsNotNone(prep.letter_final_at)
        self.act(case, 'reopen')
        prep.refresh_from_db()
        prep.letter_doc.refresh_from_db()
        self.assertEqual(prep.letter_doc.status, 'draft')
        self.assertIsNone(prep.letter_final_at)

    def test_a_draft_letter_cannot_be_marked_printed(self):
        case = self.open_one()
        response = self.act(case, 'printed', what='letter')
        self.assertTrue(any('final before it is marked printed' in m for m in self.messages(response)))
        self.assertIsNone(CasePrep.objects.get(case=case).letter_printed_at)

    def test_reopening_clears_the_printed_mark_of_the_letter(self):
        case = self.open_one()
        self.act(case, 'finalize')
        self.act(case, 'printed', what='letter')
        self.assertIsNotNone(CasePrep.objects.get(case=case).letter_printed_at)
        self.act(case, 'reopen')
        self.assertIsNone(CasePrep.objects.get(case=case).letter_printed_at)

    def test_the_affidavit_is_prepared_for_the_chosen_day_and_goes_with_the_letter(self):
        case = self.open_one()
        self.act(case, 'affidavit', mailing_date='2026-11-10')
        prep = CasePrep.objects.get(case=case)
        self.assertEqual(prep.affidavit_doc.template.slug, 'nola-affidavit')
        self.assertEqual(prep.affidavit_doc.parent_id, prep.letter_doc_id)
        self.assertEqual(prep.affidavit_doc.values['mailing_date'], '2026-11-10')
        self.assertEqual(letter_prep.status(case)['affidavit_date'], date(2026, 11, 10))

    def test_the_affidavit_can_be_prepared_again_for_another_day(self):
        case = self.open_one()
        self.act(case, 'affidavit', mailing_date='2026-11-10')
        first = CasePrep.objects.get(case=case).affidavit_doc_id
        self.act(case, 'affidavit', mailing_date='2026-11-11', replace='1')
        prep = CasePrep.objects.get(case=case)
        self.assertNotEqual(prep.affidavit_doc_id, first)
        self.assertFalse(GeneratedDocument.objects.filter(pk=first).exists())
        self.assertEqual(prep.affidavit_doc.values['mailing_date'], '2026-11-11')

    def test_printed_marks_can_be_set_and_undone(self):
        case = self.open_one()
        self.make_everything_ready(case)
        prep = CasePrep.objects.get(case=case)
        self.assertTrue(all([prep.letter_printed_at, prep.affidavit_printed_at, prep.envelope_printed_at]))
        self.act(case, 'unprinted', what='envelope')
        prep.refresh_from_db()
        self.assertIsNone(prep.envelope_printed_at)
        self.assertIn('print the envelope', letter_prep.status(case)['missing'])

    def test_the_affidavit_cannot_be_marked_printed_before_it_exists(self):
        case = self.open_one()
        response = self.act(case, 'printed', what='affidavit')
        self.assertTrue(any('Prepare the affidavit first' in m for m in self.messages(response)))

    def test_the_envelope_needs_a_mailing_address(self):
        Property.objects.filter(pk=self.prop.pk).update(street='', city='', state='', zip_code='', address='')
        case = self.open_one()
        response = self.act(case, 'printed', what='envelope')
        self.assertTrue(any('no mailing address' in m.lower() for m in self.messages(response)))
        self.assertIn("add the owner's mailing address", letter_prep.status(case)['missing'])


class MarkingItMailedTests(PrepBase):
    def test_mark_mailed_is_refused_until_everything_is_done_and_says_what_is_left(self):
        case = self.open_one()
        response = self.act(case, 'mailed', mailed_on=self.today.isoformat())
        text = ' '.join(self.messages(response))
        for left in ('make the letter final', 'prepare the affidavit', 'print the envelope'):
            self.assertIn(left, text)
        case.refresh_from_db()
        self.assertEqual(case.stage, 'preparing')
        self.assertFalse(case.events.filter(kind='letter').exists())

    def test_the_button_is_disabled_until_it_can_be_pressed(self):
        case = self.open_one()
        html = self.client.get(self.url + '?tab=preparing').content.decode()
        self.assertRegex(html, r'<button class="btn btn-sm btn-primary py-0" disabled>Mark mailed</button>')
        self.assertIn('Still to do:', html)
        self.make_everything_ready(case)
        html = self.client.get(self.url + '?tab=preparing').content.decode()
        self.assertNotRegex(html, r'disabled>Mark mailed')
        self.assertIn('Ready: confirm the day it went into the mail', html)

    def test_marking_it_mailed_moves_the_case_to_letter_sent_with_the_mailing_date(self):
        case = self.open_one()
        mailed = self.today - timedelta(days=2)
        self.make_everything_ready(case, day=mailed)
        response = self.act(case, 'mailed', mailed_on=mailed.isoformat())
        self.assertTrue(any('now on Letter sent' in m for m in self.messages(response)))
        case.refresh_from_db()
        prep = CasePrep.objects.get(case=case)
        self.assertEqual((case.stage, case.stage_since), ('letter', mailed))
        self.assertEqual(prep.letter_doc.mailed_on, mailed)
        self.assertEqual(prep.affidavit_doc.status, 'final')
        event = case.events.get(kind='letter')
        self.assertEqual((event.document_id, event.date), (prep.letter_doc_id, mailed))
        self.assertEqual(owner_collections.letter_sent_on(case), mailed)
        self.assertEqual(case.next_review, mailed + timedelta(days=30))
        by = {t['key']: t for t in owner_collections.board(self.prop, self.today)['tabs']}
        self.assertEqual((by['preparing']['count'], by['letter']['count']), (0, 1))

    def test_the_30_days_count_from_the_mailing_date(self):
        case = self.open_one()
        mailed = self.today - timedelta(days=5)
        self.make_everything_ready(case, day=mailed)
        self.act(case, 'mailed', mailed_on=mailed.isoformat())
        DelinquencyCase.objects.update(next_review=self.today + timedelta(days=60))
        row = next(t for t in owner_collections.board(self.prop, mailed + timedelta(days=31))['tabs'] if t['key'] == 'letter')['rows'][0]
        self.assertIn('Letter sent 31 days ago: can go to the lawyer', row['alert'])
        row = next(t for t in owner_collections.board(self.prop, mailed + timedelta(days=30))['tabs'] if t['key'] == 'letter')['rows'][0]
        self.assertEqual(row['alert'], '')

    def test_the_mailing_date_must_match_the_day_the_affidavit_swears_to(self):
        case = self.open_one()
        self.make_everything_ready(case, day=self.today)
        response = self.act(case, 'mailed', mailed_on=(self.today - timedelta(days=1)).isoformat())
        self.assertTrue(any('The affidavit swears to' in m for m in self.messages(response)))
        case.refresh_from_db()
        self.assertEqual(case.stage, 'preparing')

    def test_a_future_mailing_date_is_refused(self):
        case = self.open_one()
        self.make_everything_ready(case, day=self.today)
        response = self.act(case, 'mailed', mailed_on=(self.today + timedelta(days=1)).isoformat())
        self.assertTrue(any('cannot be in the future' in m for m in self.messages(response)))

    def test_a_case_that_is_not_preparing_cannot_be_marked_mailed_again(self):
        case = self.open_one()
        self.make_everything_ready(case)
        self.act(case, 'mailed', mailed_on=self.today.isoformat())
        response = self.act(case, 'mailed', mailed_on=self.today.isoformat())
        self.assertTrue(any('no longer on the preparing step' in m for m in self.messages(response)))
        self.assertEqual(DelinquencyCase.objects.get().events.filter(kind='letter').count(), 1)

    def test_the_case_page_cannot_move_a_preparing_case_to_another_step_by_hand(self):
        case = self.open_one()
        page_url = reverse('accounting:case', args=[self.prop.pk, case.pk])
        self.client.post(page_url, {'action': 'stage', 'stage': 'notice1'})
        case.refresh_from_db()
        self.assertEqual(case.stage, 'preparing')


class DraftingForTheMissingTests(PrepBase):
    def test_draft_the_missing_letters_makes_one_for_each_case_without_a_letter(self):
        case = delinquency.open_case(self.prop, self.late_unit, self.owner, user=None)
        self.assertEqual(case.stage, 'preparing')
        self.assertFalse(GeneratedDocument.objects.exists())
        with mock.patch('django.utils.timezone.localdate', return_value=self.today):
            response = self.client.post(reverse('accounting:prep_bulk', args=[self.prop.pk]), {}, follow=True)
        self.assertTrue(any('Drafted 1 letter' in m for m in self.messages(response)))
        self.assertEqual(CasePrep.objects.get(case=case).letter_doc.status, 'draft')
        with mock.patch('django.utils.timezone.localdate', return_value=self.today):
            response = self.client.post(reverse('accounting:prep_bulk', args=[self.prop.pk]), {}, follow=True)
        self.assertTrue(any('already has a letter' in m for m in self.messages(response)))
        self.assertEqual(GeneratedDocument.objects.count(), 1)

    def test_one_case_can_be_drafted_from_its_row(self):
        case = delinquency.open_case(self.prop, self.late_unit, self.owner, user=None)
        self.act(case, 'draft')
        self.assertEqual(CasePrep.objects.get(case=case).letter_doc.status, 'draft')


class PrintingTests(PrepBase):
    def get_pdf(self, kind, *ids):
        url = reverse('accounting:prep_pdf', args=[self.prop.pk, kind])
        return self.client.get(url, {'case': list(ids)} if ids else {})

    def test_the_combined_pdfs_render(self):
        case = self.open_one()
        self.act(case, 'affidavit', mailing_date=self.today.isoformat())
        for kind in ('letter', 'affidavit', 'envelope'):
            response = self.get_pdf(kind)
            self.assertEqual(response.status_code, 200, kind)
            self.assertEqual(response['Content-Type'], 'application/pdf')
            self.assertTrue(response.content.startswith(b'%PDF'), kind)

    def test_printing_does_not_mark_anything_printed(self):
        case = self.open_one()
        self.act(case, 'finalize')
        self.get_pdf('letter')
        self.get_pdf('envelope')
        prep = CasePrep.objects.get(case=case)
        self.assertIsNone(prep.letter_printed_at)
        self.assertIsNone(prep.envelope_printed_at)

    def test_only_the_ticked_cases_are_printed_when_some_are_ticked(self):
        u2 = list(Unit.objects.filter(property=self.prop).order_by('label'))[2]
        self.late_owner(u2, 'Second Owner')
        self.post(action='open_cases', unit=[self.late_unit.pk, u2.pk], tab='none')
        first = DelinquencyCase.objects.get(unit=self.late_unit)
        both = self.get_pdf('letter')
        one = self.get_pdf('letter', first.pk)
        from pypdf import PdfReader
        import io
        self.assertGreater(len(PdfReader(io.BytesIO(both.content)).pages), len(PdfReader(io.BytesIO(one.content)).pages))

    def test_an_affidavit_pdf_with_no_affidavit_goes_back_with_a_message(self):
        self.open_one()
        response = self.client.get(reverse('accounting:prep_pdf', args=[self.prop.pk, 'affidavit']), follow=True)
        self.assertTrue(any('no affidavit to print' in str(m) for m in response.context['messages']))

    def test_an_unknown_kind_is_a_404(self):
        self.assertEqual(self.client.get(reverse('accounting:prep_pdf', args=[self.prop.pk, 'photo'])).status_code, 404)

    def test_the_envelope_uses_the_company_return_address_not_the_association(self):
        case = self.open_one()
        data = letter_prep.envelopes_pdf(self.prop, [case])
        from pypdf import PdfReader
        import io
        text = PdfReader(io.BytesIO(data)).pages[0].extract_text()
        self.assertIn('Late Owner', text)
        self.assertIn('1 Main St', text)
        self.assertIn('Proper Realty', text)


class CancellingACaseTests(PrepBase):
    def test_cancelling_removes_the_case_and_its_draft_after_a_confirm(self):
        case = self.open_one()
        response = self.act(case, 'cancel')
        self.assertTrue(any('Confirm to cancel' in m for m in self.messages(response)))
        self.assertEqual(DelinquencyCase.objects.count(), 1)
        self.act(case, 'cancel', confirm='yes')
        self.assertEqual(DelinquencyCase.objects.count(), 0)
        self.assertEqual(GeneratedDocument.objects.filter(status='draft').count(), 0)
        self.assertEqual([t['count'] for t in owner_collections.board(self.prop, self.today)['tabs'] if t['key'] == 'none'], [1])      # back on the first tab

    def test_a_final_letter_is_kept_when_the_case_is_cancelled(self):
        case = self.open_one()
        self.act(case, 'finalize')
        self.act(case, 'cancel', confirm='yes')
        self.assertEqual(DelinquencyCase.objects.count(), 0)
        self.assertEqual(GeneratedDocument.objects.filter(template__slug='nola', status='final').count(), 1)

    def test_a_case_that_has_been_sent_cannot_be_cancelled_here(self):
        case = self.open_one()
        self.make_everything_ready(case)
        self.act(case, 'mailed', mailed_on=self.today.isoformat())
        self.act(case, 'cancel', confirm='yes')
        self.assertEqual(DelinquencyCase.objects.count(), 1)


class AttentionTests(PrepBase):
    def test_a_case_waiting_more_than_7_days_needs_attention_and_says_so_on_the_close_sheet(self):
        case = self.open_one()
        DelinquencyCase.objects.filter(pk=case.pk).update(stage_since=self.today - timedelta(days=8), opened_on=self.today - timedelta(days=8), next_review=self.today + timedelta(days=20))
        look = owner_collections.attention(self.prop, self.today)
        self.assertTrue(any('1 late letter waiting to be mailed' in bit for bit in look['bits']))
        by = {t['key']: t for t in owner_collections.board(self.prop, self.today)['tabs']}
        self.assertEqual(by['preparing']['alerts'], 1)
        self.assertIn('Waiting to be mailed for 8 days', by['preparing']['rows'][0]['alert'])
        self.assertEqual(look['open'], 1)

    def test_a_fresh_case_does_not_need_attention(self):
        self.open_one()
        look = owner_collections.attention(self.prop, self.today)
        self.assertFalse(any('waiting to be mailed' in bit for bit in look['bits']))
        by = {t['key']: t for t in owner_collections.board(self.prop, self.today)['tabs']}
        self.assertEqual(by['preparing']['alerts'], 0)


class TheCasePageTests(PrepBase):
    def test_a_preparing_case_shows_the_checklist_and_not_the_next_step_chooser(self):
        case = self.open_one()
        html = self.client.get(reverse('accounting:case', args=[self.prop.pk, case.pk])).content.decode()
        self.assertIn('Preparing the letter', html)
        self.assertIn('Make final', html)
        self.assertIn('Mark mailed', html)
        self.assertNotIn('Move it', html)

    def test_a_case_that_has_been_sent_shows_the_normal_next_step_chooser(self):
        case = self.open_one()
        self.make_everything_ready(case)
        self.act(case, 'mailed', mailed_on=self.today.isoformat())
        html = self.client.get(reverse('accounting:case', args=[self.prop.pk, case.pk])).content.decode()
        self.assertIn('Move it', html)
        self.assertNotIn('Make final', html)

    def test_the_draft_document_page_goes_up_to_owner_collections_when_opened_from_it(self):
        case = self.open_one()
        doc = CasePrep.objects.get(case=case).letter_doc
        self.client.get(self.url + '?tab=preparing', HTTP_ACCEPT='text/html')
        response = self.client.get(reverse('legalforms:detail', args=[doc.pk]), HTTP_ACCEPT='text/html')
        self.assertEqual(response.context['up'].label, 'Owner collections')
        self.assertIn('/collections/', response.context['up'].url)


class LegacyBehaviourTests(PrepBase):
    def test_a_unit_whose_case_is_already_sent_gets_a_further_letter_noted_on_it(self):
        case = self.open_one()
        self.make_everything_ready(case)
        self.act(case, 'mailed', mailed_on=self.today.isoformat())
        from legalforms import services
        from legalforms.models import DocumentTemplate
        template = DocumentTemplate.objects.get(slug='nola')
        values = services.initial_values(template, self.prop, self.late_unit, None)
        doc, errors = services.save_document(template, self.prop, self.late_unit, None, {k: '' if v is None else str(v) for k, v in values.items()})
        self.assertFalse(errors)
        case.refresh_from_db()
        self.assertEqual(case.stage, 'letter')
        self.assertEqual(case.events.filter(kind='letter').count(), 2)
        self.assertTrue(case.events.filter(document=doc).exists())

    def test_the_old_delinquencies_screen_opens_a_case_on_the_preparing_step_too(self):
        response = self.client.post(reverse('accounting:delinquencies', args=[self.prop.pk]), {'action': 'open_cases', 'unit': [self.late_unit.pk]}, follow=True)
        self.assertTrue(any('Opened 1 case' in str(m) for m in response.context['messages']))
        self.assertEqual(DelinquencyCase.objects.get().stage, 'preparing')
