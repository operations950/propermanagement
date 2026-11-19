"""Reading the association documents for settings: a new upload is read by itself (its effective date, then the late fee, grace period, interest, spending limit and management fee), one button reads
the rest, and of everything found for a setting only the document with the latest effective date stands. A confirm-all button applies what stands."""
from datetime import date
from types import SimpleNamespace
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse

from . import docreader, documents, ledger
from .models import AssociationDocument, DocumentPage, SettingProposal
from .test_document_binders import BinderBase, make_pdf, text_for


class FakeClaude:
    """Answers the two questions asked of a document: when it took effect (report_dates) and what settings it states (report_settings), from what each test has set up per document name."""
    def __init__(self, dates=None, found=None):
        self.dates, self.found, self.calls = dates or {}, found or {}, []
        self.messages = self

    def create(self, **kw):
        tool = kw['tool_choice']['name']
        text = kw['messages'][0]['content'][0]['text']
        self.calls.append(tool)
        name = next((n for n in list(self.dates) + list(self.found) if n in text), None)
        payload = {'dates': self.dates.get(name, [])} if tool == 'report_dates' else {'settings': self.found.get(name, [])}
        return SimpleNamespace(content=[SimpleNamespace(type='tool_use', input=payload)])


def late_fee(amount, page=2):
    return {'key': 'late_fee', 'late_fee_kind': 'flat', 'flat': amount, 'percent': None, 'page': page, 'quote': f'a late fee of ${amount}'}


@override_settings(ANTHROPIC_API_KEY='test')
class SettingsFromDocuments(BinderBase):
    def doc(self, kind, name, pages=4, **extra):
        d = documents._store(self.prop, kind, name, SimpleUploadedFile(f'{name}.pdf', make_pdf(pages), content_type='application/pdf'), self.user, text_status='ready', page_count=pages, **extra)
        DocumentPage.objects.bulk_create([DocumentPage(document=d, number=n, text=f'{name} ' + text_for(n)) for n in range(1, pages + 1)])
        return d

    def run_worker(self, fake, limit=10):
        with mock.patch.object(documents, '_client', return_value=fake):
            return documents.process_settings(limit=limit)

    def dated(self, name, iso, page=1):
        return {name: [{'page': page, 'date': iso, 'quote': f'recorded {iso}'}]}


class ANewUploadIsReadByItself(SettingsFromDocuments):
    def test_a_new_upload_is_queued_for_settings(self):
        d = documents.upload(self.prop, 'declaration', SimpleUploadedFile('Declaration.pdf', make_pdf(2), content_type='application/pdf'), self.user)
        self.assertEqual(d.settings_status, 'waiting')

    def test_the_worker_reads_its_effective_date_and_its_settings(self):
        d = self.doc('declaration', 'DeclarationOfCondo', settings_status='waiting')
        fake = FakeClaude(dates=self.dated('DeclarationOfCondo', '1989-03-05'), found={'DeclarationOfCondo': [late_fee(25)]})
        self.assertEqual(self.run_worker(fake), 1)
        d.refresh_from_db()
        self.assertEqual((d.settings_status, d.effective_date, d.effective_source), ('done', date(1989, 3, 5), 'read'))
        p = SettingProposal.objects.get(document=d)
        self.assertEqual((p.key, p.effective_date, p.status), ('late_fee', date(1989, 3, 5), 'proposed'))

    def test_a_file_that_cannot_carry_settings_is_marked_done_without_asking_claude(self):
        d = self.doc('exhibit', 'SurveyExhibit', settings_status='waiting')
        fake = FakeClaude()
        self.run_worker(fake)
        d.refresh_from_db()
        self.assertEqual(d.settings_status, 'done')
        self.assertEqual(fake.calls, [])

    def test_a_sorted_file_waits_until_what_it_holds_is_confirmed_then_each_part_gets_its_own_date(self):
        secs = [{'kind': 'declaration', 'title': 'Declaration', 'start': 1, 'end': 2}, {'kind': 'amendment', 'title': 'First Amendment', 'start': 3, 'end': 4, 'applies_to': 'declaration'}]
        d = self.doc('other', 'BinderFile', auto_sort=True, sort_status='proposed', sections=secs, settings_status='waiting')
        fake = FakeClaude(dates={'BinderFile': [{'page': 1, 'date': '1989-03-05', 'quote': 'declaration recorded'}, {'page': 3, 'date': '2015-06-01', 'quote': 'amendment recorded'}]},
                          found={'BinderFile': [late_fee(25, page=2), late_fee(50, page=4)]})
        self.assertEqual(self.run_worker(fake), 0)                    # not confirmed yet: it waits
        d.sort_status = 'confirmed'
        d.save(update_fields=['sort_status'])
        self.assertEqual(self.run_worker(fake), 1)
        d.refresh_from_db()
        self.assertEqual([s['effective_date'] for s in d.sections], ['1989-03-05', '2015-06-01'])
        live = SettingProposal.objects.get(document=d, status='proposed')
        self.assertEqual((live.value['flat'], live.effective_date), (50, date(2015, 6, 1)))
        old = SettingProposal.objects.get(document=d, status='superseded')
        self.assertEqual(old.value['flat'], 25)

    def test_a_failure_marks_the_document_failed_and_the_button_tries_it_again(self):
        d = self.doc('bylaws', 'BylawsDoc', settings_status='waiting')
        boom = mock.Mock()
        boom.messages.create.side_effect = RuntimeError('down')
        with mock.patch.object(documents, '_client', return_value=boom):
            documents.process_settings(limit=5)
        d.refresh_from_db()
        self.assertEqual(d.settings_status, 'failed')
        self.assertEqual(documents.queue_settings(self.prop), 1)
        d.refresh_from_db()
        self.assertEqual(d.settings_status, 'waiting')


class TheLatestDocumentWins(SettingsFromDocuments):
    def two(self, decl_date='1989-03-05', amend_date='2015-06-01'):
        decl = self.doc('declaration', 'DeclarationText', settings_status='waiting')
        amend = self.doc('amendment', 'AmendmentText', settings_status='waiting')
        dates = {}
        if decl_date:
            dates.update(self.dated('DeclarationText', decl_date))
        if amend_date:
            dates.update(self.dated('AmendmentText', amend_date))
        fake = FakeClaude(dates=dates, found={'DeclarationText': [late_fee(25)], 'AmendmentText': [late_fee(50)]})
        self.run_worker(fake)
        return decl, amend

    def test_the_amendment_after_the_declaration_stands_and_the_declaration_is_replaced(self):
        decl, amend = self.two()
        self.assertEqual(SettingProposal.objects.get(document=amend).status, 'proposed')
        old = SettingProposal.objects.get(document=decl)
        self.assertEqual(old.status, 'superseded')
        self.assertIn('AmendmentText', old.note)
        self.assertIn('June 1, 2015', old.note)

    def test_it_is_the_date_in_the_wording_not_the_upload_order(self):
        # the amendment was uploaded first but is dated earlier than the declaration: the declaration stands
        decl, amend = self.two(decl_date='2020-01-01', amend_date='2015-06-01')
        self.assertEqual(SettingProposal.objects.get(document=decl).status, 'proposed')
        self.assertEqual(SettingProposal.objects.get(document=amend).status, 'superseded')

    def test_a_document_with_no_date_loses_to_one_with_a_date(self):
        decl, amend = self.two(decl_date=None, amend_date='2015-06-01')
        self.assertEqual(SettingProposal.objects.get(document=amend).status, 'proposed')
        self.assertEqual(SettingProposal.objects.get(document=decl).status, 'superseded')

    def test_a_typed_date_is_kept_and_sorts_the_proposals_again(self):
        decl, amend = self.two()
        documents.set_effective_date(decl, date(2030, 1, 1))          # the declaration is really the later one
        self.assertEqual(SettingProposal.objects.get(document=decl).status, 'proposed')
        self.assertEqual(SettingProposal.objects.get(document=amend).status, 'superseded')
        decl.refresh_from_db()
        self.assertEqual((decl.effective_date, decl.effective_source), (date(2030, 1, 1), 'typed'))
        # reading the dates again does not replace a typed date
        fake = FakeClaude(dates=self.dated('DeclarationText', '1989-03-05'))
        with mock.patch.object(documents, '_client', return_value=fake):
            documents.read_effective_dates(decl)
        decl.refresh_from_db()
        self.assertEqual(decl.effective_date, date(2030, 1, 1))

    def test_a_setting_already_confirmed_from_a_later_document_is_not_proposed_from_an_earlier_one(self):
        amend = self.doc('amendment', 'AmendmentText', settings_status='waiting')
        self.run_worker(FakeClaude(dates=self.dated('AmendmentText', '2015-06-01'), found={'AmendmentText': [late_fee(50)]}))
        documents.confirm(SettingProposal.objects.get(document=amend), self.user)
        decl = self.doc('declaration', 'DeclarationText', settings_status='waiting')
        self.run_worker(FakeClaude(dates=self.dated('DeclarationText', '1989-03-05'), found={'DeclarationText': [late_fee(25)]}))
        old = SettingProposal.objects.get(document=decl)
        self.assertEqual(old.status, 'superseded')
        self.assertIn('Already set', old.note)

    def test_confirm_all_applies_only_what_stands(self):
        self.two()
        n, problems = documents.confirm_all(self.prop, self.user)
        self.assertEqual((n, problems), (1, []))
        self.assertEqual(ledger.profile_for(self.prop).late_fee_flat, 50)
        self.assertEqual(SettingProposal.objects.filter(status='confirmed').count(), 1)

    def test_settings_of_different_kinds_each_stand(self):
        d = self.doc('declaration', 'DeclarationText', settings_status='waiting')
        fake = FakeClaude(dates=self.dated('DeclarationText', '1989-03-05'),
                          found={'DeclarationText': [late_fee(25), {'key': 'grace_days', 'days': 10, 'page': 1, 'quote': 'ten days'}, {'key': 'interest_rate', 'percent': 18, 'page': 1, 'quote': '18%'}]})
        self.run_worker(fake)
        self.assertEqual(SettingProposal.objects.filter(document=d, status='proposed').count(), 3)
        n, _ = documents.confirm_all(self.prop, self.user)
        self.assertEqual(n, 3)
        self.assertEqual(ledger.profile_for(self.prop).late_fee_grace_days, 10)


class TheButtonsOnTheSettingsPage(SettingsFromDocuments):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        self.url = reverse('accounting:settings', args=[self.prop.pk])

    def test_read_all_queues_the_documents_not_read_yet_and_says_so(self):
        old = self.doc('declaration', 'OldDeclaration', settings_status='')            # uploaded before this existed: never asked
        done = self.doc('bylaws', 'DoneBylaws', settings_status='done')
        exhibit = self.doc('exhibit', 'ExhibitA', settings_status='')
        r = self.client.post(self.url, {'action': 'read_all_settings'}, follow=True)
        self.assertContains(r, 'Reading 1 document for settings')
        old.refresh_from_db(), done.refresh_from_db(), exhibit.refresh_from_db()
        self.assertEqual((old.settings_status, done.settings_status, exhibit.settings_status), ('waiting', 'done', ''))
        self.assertContains(r, 'Read all documents for settings')

    def test_the_page_shows_the_effective_date_and_asks_for_a_missing_one(self):
        d = self.doc('declaration', 'DeclarationText', settings_status='done')
        r = self.client.get(self.url)
        self.assertContains(r, 'Effective date needed')
        self.client.post(self.url, {'action': 'set_effective_date', 'doc': d.pk, 'section': '', 'effective_date': '1989-03-05'})
        r = self.client.get(self.url)
        self.assertContains(r, 'in effect Mar 5, 1989')
        self.assertContains(r, '(typed)')

    def test_confirm_all_on_the_page_sets_the_latest_values(self):
        decl = self.doc('declaration', 'DeclarationText', settings_status='waiting')
        amend = self.doc('amendment', 'AmendmentText', settings_status='waiting')
        self.run_worker(FakeClaude(dates={**self.dated('DeclarationText', '1989-03-05'), **self.dated('AmendmentText', '2015-06-01')},
                                   found={'DeclarationText': [late_fee(25)], 'AmendmentText': [late_fee(50)]}))
        r = self.client.get(self.url)
        self.assertContains(r, 'Confirm all')
        self.assertContains(r, 'Replaced by a later document')
        r = self.client.post(self.url, {'action': 'confirm_all'}, follow=True)
        self.assertContains(r, '1 setting confirmed')
        self.assertEqual(ledger.profile_for(self.prop).late_fee_flat, 50)

    def test_the_worker_step_is_part_of_the_document_run(self):
        d = self.doc('declaration', 'DeclarationText', settings_status='waiting')
        fake = FakeClaude(dates=self.dated('DeclarationText', '1989-03-05'), found={'DeclarationText': [late_fee(25)]})
        with mock.patch.object(documents, '_client', return_value=fake):
            docreader.process_waiting(limit=0)
        d.refresh_from_db()
        self.assertEqual(d.settings_status, 'done')
