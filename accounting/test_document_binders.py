"""A big PDF of governing documents: held as hidden parts, sorted as one file, second copies left out, then saved as one file per document. And 'Split into separate documents' for a small sorted file."""
import io
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from pypdf import PdfReader, PdfWriter

from . import binders, docreader, documents
from .models import AssociationDocument, DocumentPage
from .test_collections_close import STORAGES
from .tests import make_assoc


def make_pdf(pages):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def page_count(doc):
    doc.file.open('rb')
    try:
        return len(PdfReader(io.BytesIO(doc.file.read())).pages)
    finally:
        doc.file.close()


SECTIONS = [
    {'kind': 'declaration', 'title': 'Declaration of Condominium (recorded 1989)', 'start': 1, 'end': 5, 'confidence': 0.9},
    {'kind': 'articles', 'title': 'Articles of Incorporation', 'start': 6, 'end': 8, 'confidence': 0.9},
    {'kind': 'amendment', 'title': 'First Amendment (recorded 1994)', 'start': 9, 'end': 10, 'confidence': 0.8, 'applies_to': 'declaration'},
    {'kind': 'exhibit', 'title': 'Survey', 'start': 11, 'end': 11, 'confidence': 0.8, 'parent_page': 9},
    {'kind': 'other', 'title': 'Cover sheet', 'start': 12, 'end': 12, 'confidence': 0.4},
]


def text_for(n):
    return f'page {n} ' + ' '.join(f'w{n}x{i}' for i in range(30))


@override_settings(STORAGES=STORAGES, DOCUMENT_JOBS_INLINE=True)       # the saves run inside the click here, so the outcome is certain; accounting/test_document_jobs.py covers the thread
class BinderBase(TestCase):
    def setUp(self):
        self.prop = make_assoc()
        self.user = get_user_model().objects.create_superuser('boss', 'b@example.com', 'pw12345678')
        self.data = make_pdf(12)

    def upload(self, pages=12):
        data = make_pdf(pages)
        with mock.patch.object(documents, 'STORE_LIMIT', len(data) // 3):
            made, problems = documents.upload_many(self.prop, [SimpleUploadedFile('Binder.pdf', data, content_type='application/pdf')], self.user)
        self.assertEqual(problems, [])
        return made[0]

    def read_parts(self, head, per_page=text_for):
        """What the reading job does: keep the words of every page and mark each part read."""
        offset = 0
        for part in binders.parts_of(head.binder_id):
            n = page_count(part)
            DocumentPage.objects.bulk_create([DocumentPage(document=part, number=i + 1, text=per_page(offset + i + 1), tags=f'tag{offset + i + 1}') for i in range(n)])
            part.page_count, part.text_status = n, 'ready'
            part.save(update_fields=['page_count', 'text_status'])
            offset += n

    def ready_binder(self, sections=SECTIONS, per_page=text_for):
        head = self.upload()
        self.read_parts(head, per_page)
        with mock.patch.object(docreader, 'sort_pages', return_value=([dict(s) for s in sections], 'proposed')):
            binders.process()
        head.refresh_from_db()
        return head


class BinderStartTests(BinderBase):
    def test_a_big_pdf_becomes_hidden_staging_parts_not_documents(self):
        head = self.upload()
        parts = binders.parts_of(head.binder_id)
        self.assertGreaterEqual(len(parts), 3)
        self.assertTrue(all(p.is_staging and p.binder_name == 'Binder' for p in parts))
        self.assertEqual([p.binder_order for p in parts], list(range(len(parts))))
        self.assertEqual(head.binder_state, 'reading')
        self.assertEqual(sum(p.page_count or page_count(p) for p in parts), 12)
        rows = documents.checklist(self.prop)
        self.assertEqual([r for r in rows if r['kind'] == 'unplaced'], [])
        self.assertTrue(all(r['doc'] is None for r in rows))
        files = documents.files(self.prop)
        self.assertEqual(len(files), 1)                       # one row for the whole upload, not one for each part
        self.assertTrue(files[0]['binder'] and files[0]['state'] == 'reading')

    def test_a_small_pdf_is_uploaded_as_it_always_was(self):
        made, _ = documents.upload_many(self.prop, [SimpleUploadedFile('Small.pdf', make_pdf(2), content_type='application/pdf')], self.user)
        self.assertFalse(made[0].is_staging)
        self.assertTrue(made[0].auto_sort)

    def test_a_big_file_that_is_not_a_pdf_is_still_refused(self):
        with mock.patch.object(documents, 'STORE_LIMIT', 10):
            made, problems = documents.upload_many(self.prop, [SimpleUploadedFile('scan.png', b'x' * 50, content_type='image/png')], self.user)
        self.assertEqual(made, [])
        self.assertIn('not a PDF', problems[0])

    def test_the_binder_waits_until_every_part_is_read(self):
        head = self.upload()
        with mock.patch.object(docreader, 'sort_pages') as sorter:
            binders.process()
        sorter.assert_not_called()
        head.refresh_from_db()
        self.assertEqual(head.binder_state, 'reading')

    def test_a_part_that_cannot_be_read_fails_the_binder_with_its_reason(self):
        head = self.upload()
        part = binders.parts_of(head.binder_id)[1]
        part.text_status, part.read_note = 'failed', 'not a PDF this program can open'
        part.save(update_fields=['text_status', 'read_note'])
        binders.process()
        head.refresh_from_db()
        self.assertEqual(head.binder_state, 'failed')
        self.assertIn('not a PDF', head.read_note)


class BinderSortingTests(BinderBase):
    def test_the_whole_binder_is_sorted_once_with_page_numbers_running_straight_through(self):
        head = self.upload()
        self.read_parts(head)
        seen = []
        with mock.patch.object(docreader, 'sort_pages', side_effect=lambda pages, name: (seen.append(([p.number for p in pages], name)) or [dict(s) for s in SECTIONS], 'proposed')):
            binders.process()
            binders.process()                                  # a second run does nothing more
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0], (list(range(1, 13)), 'Binder'))
        head.refresh_from_db()
        self.assertEqual(head.binder_state, 'needs_confirm')
        self.assertEqual([s['kind'] for s in head.sections], ['declaration', 'articles', 'amendment', 'exhibit', 'other'])
        self.assertEqual(documents.files(self.prop)[0]['state'], 'needs_look')

    def test_claude_is_asked_once_through_the_real_sorter(self):
        head = self.upload()
        self.read_parts(head)
        block = SimpleNamespace(type='tool_use', name='report_documents', input={'documents': [
            {'kind': 'declaration', 'title': 'Declaration', 'start_page': 1, 'end_page': 7, 'confidence': 0.9},
            {'kind': 'bylaws', 'title': 'Bylaws', 'start_page': 8, 'end_page': 12, 'confidence': 0.9}]})
        client = mock.Mock()
        client.messages.create.return_value = mock.Mock(content=[block])
        with mock.patch('accounting.documents._client', return_value=client):
            binders.process()
        self.assertEqual(client.messages.create.call_count, 1)
        head.refresh_from_db()
        self.assertEqual([(s['kind'], s['start'], s['end']) for s in head.sections], [('declaration', 1, 7), ('bylaws', 8, 12)])

    def test_without_the_claude_connection_the_person_is_asked_to_say_what_it_holds(self):
        head = self.upload()
        self.read_parts(head)
        from django.test import override_settings as os_
        with os_(ANTHROPIC_API_KEY=''):
            binders.process()
        head.refresh_from_db()
        self.assertEqual((head.binder_state, head.sort_status), ('needs_confirm', 'manual'))
        self.assertEqual(len(head.sections), 1)
        self.assertIn('Say what it holds', head.read_note)


class SecondCopyTests(TestCase):
    def texts(self, a_words=400, b_changed=2, b_words=None):
        base = [f'term{i}' for i in range(a_words)]
        other = list(base[: b_words or a_words])
        for i in range(b_changed):
            other[(i * 3) % len(other)] = f'smudge{i}'
        return {1: ' '.join(base[:200]), 2: ' '.join(base[200:]), 3: ' '.join(other[:200]), 4: ' '.join(other[200:])}

    sections = [{'kind': 'declaration', 'title': 'Declaration', 'start': 1, 'end': 2, 'confidence': 0.9}, {'kind': 'declaration', 'title': 'Declaration again', 'start': 3, 'end': 4, 'confidence': 0.9}]

    def test_the_same_document_with_a_smudge_or_two_is_a_copy(self):
        marked = binders.mark_copies(self.sections, self.texts())
        self.assertTrue(marked[1]['leave_out'])
        self.assertEqual((marked[1]['dup_of'], marked[1]['dup_title']), (1, 'Declaration'))
        self.assertGreaterEqual(marked[1]['dup_score'], 98)
        self.assertNotIn('leave_out', marked[0])

    def test_a_document_that_only_looks_alike_is_not_a_copy(self):
        marked = binders.mark_copies(self.sections, self.texts(b_changed=60))         # 15% of the words differ
        self.assertNotIn('leave_out', marked[1])

    def test_a_long_document_needs_95_percent_and_a_short_one_nearly_all(self):
        # 1,400 words, 4% different: not a copy of a short one at 99%, but the long one passes at 95%
        long_marked = binders.mark_copies(self.sections, self.texts(a_words=1400, b_changed=40))
        self.assertTrue(long_marked[1].get('leave_out'), long_marked[1])
        short_marked = binders.mark_copies(self.sections, self.texts(a_words=400, b_changed=20))        # 5% different, short
        self.assertNotIn('leave_out', short_marked[1])

    def test_a_few_lines_are_never_called_a_copy(self):
        tiny = {1: 'a b c', 2: 'd', 3: 'a b c', 4: 'd'}
        self.assertNotIn('leave_out', binders.mark_copies(self.sections, tiny)[1])

    def test_surveys_and_other_pages_are_never_judged(self):
        secs = [{'kind': 'exhibit', 'title': 'Survey', 'start': 1, 'end': 2, 'confidence': 0.9}, {'kind': 'exhibit', 'title': 'Survey', 'start': 3, 'end': 4, 'confidence': 0.9}]
        self.assertNotIn('leave_out', binders.mark_copies(secs, self.texts())[1])

    def test_claude_saying_it_is_a_copy_lowers_the_bar_but_the_words_must_still_mostly_agree(self):
        hinted = [dict(self.sections[0]), dict(self.sections[1], repeat_of=1)]
        marked = binders.mark_copies(hinted, self.texts(b_changed=50))               # about 87% the same
        self.assertTrue(marked[1].get('leave_out'))
        different = binders.mark_copies(hinted, self.texts(b_changed=200))
        self.assertNotIn('leave_out', different[1])


class PlanTests(TestCase):
    def test_one_file_per_document_exhibits_with_their_parent_and_other_pages_grouped(self):
        plan, left = binders.plan_files(SECTIONS)
        self.assertEqual(left, [])
        self.assertEqual([(g['kind'], g['pages']) for g in plan], [('declaration', [1, 2, 3, 4, 5]), ('articles', [6, 7, 8]), ('amendment', [9, 10, 11]), ('other', [12])])
        self.assertEqual(plan[2]['title'], 'Amendment to the Declaration: First Amendment (recorded 1994)')

    def test_an_exhibit_with_no_parent_goes_with_the_document_before_it(self):
        secs = [{'kind': 'bylaws', 'title': 'Bylaws', 'start': 1, 'end': 3}, {'kind': 'exhibit', 'title': 'Plat', 'start': 4, 'end': 4}]
        self.assertEqual(binders.plan_files(secs)[0][0]['pages'], [1, 2, 3, 4])

    def test_a_copy_is_left_out_with_its_exhibits_unless_it_is_kept(self):
        secs = [{'kind': 'declaration', 'title': 'Declaration', 'start': 1, 'end': 3}, {'kind': 'declaration', 'title': 'Declaration', 'start': 4, 'end': 6, 'leave_out': True, 'dup_of': 1, 'dup_title': 'Declaration', 'dup_score': 99},
                {'kind': 'exhibit', 'title': 'Survey', 'start': 7, 'end': 7, 'parent_page': 4}]
        plan, left = binders.plan_files(secs)
        self.assertEqual([g['pages'] for g in plan], [[1, 2, 3]])
        self.assertEqual(left, [{'start': 4, 'end': 6, 'of': 'Declaration', 'score': 99}])
        secs[1]['keep'] = True
        plan, left = binders.plan_files(secs)
        self.assertEqual([(g['title'], g['pages']) for g in plan], [('Declaration', [1, 2, 3]), ('Declaration (second copy)', [4, 5, 6, 7])])
        self.assertEqual(left, [])

    def test_other_pages_in_two_stretches_are_two_files_named_by_their_pages(self):
        secs = [{'kind': 'other', 'title': 'Cover', 'start': 1, 'end': 1}, {'kind': 'bylaws', 'title': 'Bylaws', 'start': 2, 'end': 3}, {'kind': 'other', 'title': 'Minutes', 'start': 4, 'end': 5}]
        plan, _ = binders.plan_files(secs)
        self.assertEqual([g['title'] for g in plan], ['Other pages (pages 1-1)', 'Bylaws', 'Other pages (pages 4-5)'])


class FinalizeTests(BinderBase):
    def test_confirming_saves_one_file_per_document_with_the_words_carried_over_and_removes_the_staging_parts(self):
        head = self.ready_binder()
        choices = [(s['kind'], s['title'], s.get('applies_to')) for s in head.sections]
        made, left = binders.confirm(head, choices, set(), self.user)
        self.assertEqual(left, [])
        self.assertEqual([(d.kind, d.name, d.page_count) for d in made], [
            ('declaration', 'Declaration of Condominium (recorded 1989)', 5), ('articles', 'Articles of Incorporation', 3),
            ('amendment', 'Amendment to the Declaration: First Amendment (recorded 1994)', 3), ('other', 'Other pages', 1)])
        self.assertEqual([page_count(d) for d in made], [5, 3, 3, 1])              # the real pages were cut, not just the numbers
        self.assertFalse(any(d.is_staging or d.auto_sort for d in made))
        amendment = made[2]
        self.assertEqual([p.text for p in amendment.pages.order_by('number')], [text_for(9), text_for(10), text_for(11)])
        self.assertEqual([p.tags for p in amendment.pages.order_by('number')], ['tag9', 'tag10', 'tag11'])
        self.assertEqual(made[2].text_status, 'ready')
        self.assertFalse(AssociationDocument.objects.filter(is_staging=True).exists())          # the hidden parts are gone
        names = [r['doc'].name for r in documents.checklist(self.prop) if r['doc']]
        self.assertIn('Declaration of Condominium (recorded 1989)', names)
        self.assertEqual(documents.files(self.prop)[0]['state'], 'ready')

    def test_a_section_that_spans_two_parts_is_stitched_from_both(self):
        head = self.ready_binder([{'kind': 'bylaws', 'title': 'Bylaws', 'start': 1, 'end': 12, 'confidence': 0.9}])
        made, _ = binders.confirm(head, [('bylaws', 'Bylaws', None)], set(), self.user)
        self.assertEqual(len(made), 1)
        self.assertEqual(page_count(made[0]), 12)
        self.assertEqual([p.text for p in made[0].pages.order_by('number')], [text_for(n) for n in range(1, 13)])

    def test_the_edits_the_person_made_are_what_is_saved(self):
        head = self.ready_binder()
        choices = [('declaration', 'Our Declaration', None), ('bylaws', 'Bylaws (corrected)', None), ('amendment', 'Hurricane amendment', 'bylaws'), ('exhibit', 'Survey', None), ('other', 'Cover', None)]
        made, _ = binders.confirm(head, choices, set(), self.user)
        self.assertEqual([(d.kind, d.name) for d in made[:3]], [('declaration', 'Our Declaration'), ('bylaws', 'Bylaws (corrected)'), ('amendment', 'Amendment to the Bylaws: Hurricane amendment')])

    def test_a_second_copy_is_left_out_and_keep_it_anyway_saves_it(self):
        words = [f'term{i}' for i in range(300)]
        per_page = lambda n: ' '.join(words[(n - 1) % 6 * 50:(n - 1) % 6 * 50 + 50]) if n <= 12 else ''
        secs = [{'kind': 'declaration', 'title': 'Declaration', 'start': 1, 'end': 6, 'confidence': 0.9}, {'kind': 'declaration', 'title': 'Declaration (again)', 'start': 7, 'end': 12, 'confidence': 0.9, 'repeat_of': 1}]
        head = self.ready_binder(secs, per_page)
        self.assertTrue(head.sections[1]['leave_out'])
        self.assertEqual(head.sections[1]['dup_score'], 100)
        html = self.client_page(head)
        self.assertIn('Left out: a copy of Declaration (100% the same)', html)
        self.assertIn('Keep it anyway', html)
        made, left = binders.confirm(head, [('declaration', 'Declaration', None), ('declaration', 'Declaration (again)', None)], set(), self.user)
        self.assertEqual([d.name for d in made], ['Declaration'])
        self.assertEqual([(x['start'], x['end'], x['score']) for x in left], [(7, 12, 100)])

    def test_keeping_the_copy_anyway_saves_it_as_a_second_copy(self):
        words = [f'term{i}' for i in range(300)]
        per_page = lambda n: ' '.join(words[(n - 1) % 6 * 50:(n - 1) % 6 * 50 + 50])
        secs = [{'kind': 'declaration', 'title': 'Declaration', 'start': 1, 'end': 6, 'confidence': 0.9}, {'kind': 'declaration', 'title': 'Declaration (recorded)', 'start': 7, 'end': 12, 'confidence': 0.9}]
        head = self.ready_binder(secs, per_page)
        made, left = binders.confirm(head, [('declaration', 'Declaration', None), ('declaration', 'Declaration (recorded)', None)], {1}, self.user)
        self.assertEqual(left, [])
        self.assertEqual([d.name for d in made], ['Declaration', 'Declaration (recorded) (second copy)'])

    def test_one_document_that_is_itself_too_big_is_cut_into_parts_at_page_boundaries(self):
        head = self.ready_binder([{'kind': 'declaration', 'title': 'Declaration', 'start': 1, 'end': 12, 'confidence': 0.9}])
        one = len(self.data) // 3
        with mock.patch.object(documents, 'STORE_LIMIT', one * 2):                 # the whole declaration no longer fits
            made, _ = binders.confirm(head, [('declaration', 'Declaration', None)], set(), self.user)
        self.assertGreaterEqual(len(made), 2)
        self.assertEqual([d.name for d in made][:2], [f'Declaration (part 1 of {len(made)})', f'Declaration (part 2 of {len(made)})'])
        self.assertEqual(sum(d.page_count for d in made), 12)
        self.assertEqual([p.text for d in made for p in d.pages.order_by('number')], [text_for(n) for n in range(1, 13)])
        self.assertTrue(all(d.kind == 'declaration' for d in made))

    def test_a_failure_leaves_every_staging_part_and_saves_nothing(self):
        head = self.ready_binder()
        before = AssociationDocument.objects.filter(is_staging=True).count()
        choices = [(s['kind'], s['title'], s.get('applies_to')) for s in head.sections]
        with mock.patch.object(binders, '_cut_and_store', side_effect=RuntimeError('storage is down')):
            with self.assertRaises(documents.DocumentError) as e:
                binders.confirm(head, choices, set(), self.user)
        self.assertIn('Your sorted list is kept', str(e.exception))
        self.assertEqual(AssociationDocument.objects.filter(is_staging=True).count(), before)
        self.assertFalse(AssociationDocument.objects.filter(is_staging=False).exists())
        head.refresh_from_db()
        self.assertEqual(head.binder_state, 'needs_confirm')                 # back on the review screen with the sort intact, not "could not be read"
        self.assertTrue(head.sections)
        # and it can be tried again
        made, _ = binders.finalize(head, self.user)
        self.assertEqual(len(made), 4)
        self.assertFalse(AssociationDocument.objects.filter(is_staging=True).exists())

    def test_a_very_long_document_title_can_always_be_saved(self):
        title = 'Castlewood Town Villas Rules and Regulations (revised 2-9-13) with every amendment and exhibit ' * 2
        head = self.ready_binder([{'kind': 'rules', 'title': title, 'start': 1, 'end': 12, 'confidence': 0.9}])
        made, _ = binders.confirm(head, [('rules', title, None)], set(), self.user)
        self.assertEqual(len(made), 1)
        self.assertLessEqual(len(made[0].file.name), 100)                    # the column holds 100 characters, whatever the title

    def test_a_partly_stored_save_is_rolled_back(self):
        head = self.ready_binder()
        real = documents._store
        calls = []

        def flaky(*a, **kw):
            calls.append(1)
            if len(calls) == 3:
                raise RuntimeError('boom')
            return real(*a, **kw)
        with mock.patch.object(documents, '_store', side_effect=flaky):
            with self.assertRaises(documents.DocumentError):
                binders.finalize(head, self.user)
        self.assertFalse(AssociationDocument.objects.filter(is_staging=False).exists())
        self.assertTrue(AssociationDocument.objects.filter(is_staging=True).exists())

    def test_only_one_save_runs_at_a_time(self):
        head = self.ready_binder()
        AssociationDocument.objects.filter(pk=head.pk).update(binder_state='finalizing')
        head.refresh_from_db()
        with self.assertRaises(documents.DocumentError):
            binders.finalize(head, self.user)

    def test_a_save_that_stopped_halfway_becomes_ready_to_try_again(self):
        from datetime import timedelta

        from django.utils import timezone
        head = self.ready_binder()
        AssociationDocument.objects.filter(pk=head.pk).update(binder_state='finalizing', read_started_at=timezone.now() - timedelta(minutes=30))
        binders.process()
        head.refresh_from_db()
        self.assertEqual(head.binder_state, 'needs_confirm')

    def test_throwing_a_binder_away_removes_every_part(self):
        head = self.upload()
        binders.cancel(head)
        self.assertFalse(AssociationDocument.objects.filter(is_staging=True).exists())

    def client_page(self, head):
        user = get_user_model().objects.create_superuser('boss2', 'b2@example.com', 'pw12345678')
        self.client.force_login(user)
        return self.client.get(reverse('accounting:settings', args=[self.prop.pk])).content.decode()


class SplitASortedFileTests(BinderBase):
    def sorted_file(self):
        doc = documents._store(self.prop, 'other', 'Combined', SimpleUploadedFile('Combined.pdf', make_pdf(6), content_type='application/pdf'), self.user,
                               auto_sort=True, sort_status='confirmed', text_status='ready', page_count=6,
                               sections=[{'kind': 'declaration', 'title': 'Declaration', 'start': 1, 'end': 3, 'confidence': 0.9}, {'kind': 'bylaws', 'title': 'Bylaws', 'start': 4, 'end': 6, 'confidence': 0.9}])
        DocumentPage.objects.bulk_create([DocumentPage(document=doc, number=n, text=text_for(n), tags=f'tag{n}') for n in range(1, 7)])
        return doc

    def test_it_makes_separate_documents_and_removes_the_original_only_after(self):
        doc = self.sorted_file()
        made, left = binders.split_sorted(doc, self.user)
        self.assertEqual([(d.kind, d.name, d.page_count) for d in made], [('declaration', 'Declaration', 3), ('bylaws', 'Bylaws', 3)])
        self.assertEqual([p.text for p in made[1].pages.order_by('number')], [text_for(4), text_for(5), text_for(6)])
        self.assertFalse(AssociationDocument.objects.filter(pk=doc.pk).exists())

    def test_a_failure_leaves_the_original_untouched(self):
        doc = self.sorted_file()
        with mock.patch.object(binders, '_cut_and_store', side_effect=RuntimeError('storage is down')):
            with self.assertRaises(documents.DocumentError):
                binders.split_sorted(doc, self.user)
        self.assertTrue(AssociationDocument.objects.filter(pk=doc.pk).exists())
        self.assertEqual(AssociationDocument.objects.filter(property=self.prop).count(), 1)

    def test_a_file_that_is_not_sorted_and_confirmed_cannot_be_split(self):
        doc = self.sorted_file()
        AssociationDocument.objects.filter(pk=doc.pk).update(sort_status='proposed')
        doc.refresh_from_db()
        with self.assertRaises(documents.DocumentError):
            binders.split_sorted(doc, self.user)

    def test_one_document_has_nothing_to_separate(self):
        doc = self.sorted_file()
        AssociationDocument.objects.filter(pk=doc.pk).update(sections=[{'kind': 'bylaws', 'title': 'Bylaws', 'start': 1, 'end': 6, 'confidence': 0.9}])
        doc.refresh_from_db()
        with self.assertRaises(documents.DocumentError):
            binders.split_sorted(doc, self.user)


class SettingsScreenTests(BinderBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        self.url = reverse('accounting:settings', args=[self.prop.pk])

    def test_the_uploader_is_a_drop_zone_with_the_limit_and_the_two_labels(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn('id="doc-drop"', html)
        self.assertIn('Drag files here', html)
        self.assertIn(f'data-limit="{documents.STORE_LIMIT}"', html)
        self.assertIn("'Split into separate documents'", html)
        self.assertIn("'Upload'", html)
        self.assertIn('name="files" id="doc-files" multiple', html)
        self.assertIn('addEventListener(\'drop\'', html)

    def test_the_post_of_a_big_pdf_starts_a_binder(self):
        data = make_pdf(12)
        with mock.patch.object(documents, 'STORE_LIMIT', len(data) // 3):
            r = self.client.post(self.url, {'action': 'upload_many', 'files': SimpleUploadedFile('Big.pdf', data, content_type='application/pdf')}, follow=True)
        self.assertContains(r, 'read as one file, sorted into its documents')
        head = AssociationDocument.objects.get(is_staging=True, binder_order=0)
        self.assertEqual(head.binder_name, 'Big')
        self.assertContains(r, 'Sorting your upload')

    def test_the_confirm_screen_and_the_buttons_work_through_the_page(self):
        head = self.ready_binder()
        html = self.client.get(self.url).content.decode()
        self.assertIn('This file holds 5 documents', html)
        self.assertIn('Looks right: save as separate documents', html)
        form = {'action': 'binder_confirm', 'doc': head.pk}
        for i, s in enumerate(head.sections):
            form.update({f'kind_{i}': s['kind'], f'title_{i}': s['title'], f'applies_{i}': s.get('applies_to', '')})
        r = self.client.post(self.url, form, follow=True)
        self.assertContains(r, 'Saved as 4 separate documents')
        self.assertFalse(AssociationDocument.objects.filter(is_staging=True).exists())

    def test_a_failed_save_shows_a_plain_message_and_keeps_the_upload(self):
        head = self.ready_binder()
        form = {'action': 'binder_confirm', 'doc': head.pk}
        for i, s in enumerate(head.sections):
            form.update({f'kind_{i}': s['kind'], f'title_{i}': s['title'], f'applies_{i}': ''})
        with mock.patch.object(binders, '_cut_and_store', side_effect=RuntimeError('down')):
            r = self.client.post(self.url, form, follow=True)
        self.assertContains(r, 'Your sorted list is kept')
        self.assertContains(r, 'Needs a look')
        self.assertNotContains(r, 'Could not be read')
        self.assertTrue(AssociationDocument.objects.filter(is_staging=True).exists())

    def test_the_split_button_shows_only_for_a_sorted_file_and_works(self):
        doc = SplitASortedFileTests.sorted_file(self)
        html = self.client.get(self.url).content.decode()
        self.assertIn('Split into separate documents', html.split('id="doc-drop"')[1].split('</form>')[1])
        r = self.client.post(self.url, {'action': 'split_sorted', 'doc': doc.pk}, follow=True)
        self.assertContains(r, 'Split into 2 separate documents')
        self.assertFalse(AssociationDocument.objects.filter(pk=doc.pk).exists())

    def test_removing_a_big_upload_removes_every_hidden_part(self):
        head = self.upload()
        self.client.post(self.url, {'action': 'delete_doc', 'doc': head.pk})
        self.assertFalse(AssociationDocument.objects.filter(is_staging=True).exists())

    def test_staging_parts_do_not_count_as_the_management_contract_or_confuse_questions(self):
        self.upload()
        self.assertFalse(documents.has_contract(self.prop))
        self.assertEqual(documents._docs(self.prop).count(), 0)
