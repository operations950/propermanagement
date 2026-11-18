"""The document sorter's saves run in the background: a persisted job, a thread started after the row is committed, progress and a heartbeat, one job per upload, a stopped save recovered, and
nothing ever stored twice. The thread itself is not run in these tests (it would not see a test transaction): the same work runs inside the call (DOCUMENT_JOBS_INLINE), and the thread path is
checked by what it starts."""
from datetime import timedelta
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from . import binders, docjobs, documents
from .models import AssociationDocument, DocumentJob, DocumentPage
from .test_collections_close import STORAGES
from .test_document_binders import BinderBase, make_pdf, text_for


def form_for(head):
    form = {'action': 'binder_confirm', 'doc': head.pk}
    for i, s in enumerate(head.sections):
        form.update({f'kind_{i}': s['kind'], f'title_{i}': s['title'], f'applies_{i}': s.get('applies_to', '')})
    return form


class JobBase(BinderBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        self.url = reverse('accounting:settings', args=[self.prop.pk])
        self.status_url = reverse('accounting:document_jobs', args=[self.prop.pk])

    def sorted_file(self):
        doc = documents._store(self.prop, 'other', 'Combined', SimpleUploadedFile('Combined.pdf', make_pdf(6), content_type='application/pdf'), self.user,
                               auto_sort=True, sort_status='confirmed', text_status='ready', page_count=6,
                               sections=[{'kind': 'declaration', 'title': 'Declaration', 'start': 1, 'end': 3, 'confidence': 0.9}, {'kind': 'bylaws', 'title': 'Bylaws', 'start': 4, 'end': 6, 'confidence': 0.9}])
        DocumentPage.objects.bulk_create([DocumentPage(document=doc, number=n, text=text_for(n), tags=f'tag{n}') for n in range(1, 7)])
        return doc

    def active_job(self, head, status='running', beat_minutes=0, kind='binder_save'):
        job = DocumentJob.objects.create(association=self.prop, kind=kind, target_id=head.pk, title='x', status=status, message='Cutting', heartbeat_at=timezone.now() - timedelta(minutes=beat_minutes))
        return job


class SavingAsAJob(JobBase):
    def test_the_click_records_a_job_runs_it_and_tells_the_person_once_how_it_ended(self):
        head = self.ready_binder()
        r = self.client.post(self.url, form_for(head), follow=True)
        self.assertContains(r, 'Saving &quot;Binder&quot; as separate documents')            # what the click said
        self.assertContains(r, 'Saved as 4 separate documents.')                               # and how it ended, on the page that followed
        job = DocumentJob.objects.get()
        self.assertEqual((job.kind, job.status, job.target_id, job.title), ('binder_save', 'done', head.pk, 'Binder'))
        self.assertEqual(job.result, {'created': 4, 'left_out': 0})
        self.assertEqual((job.done_count, job.total_count), (4, 4))
        self.assertIsNotNone(job.started_at)
        self.assertIsNotNone(job.finished_at)
        self.assertEqual(job.created_by, self.user)
        self.assertFalse(AssociationDocument.objects.filter(is_staging=True).exists())
        again = self.client.get(self.url)
        self.assertNotContains(again, 'Saved as 4 separate documents.')                        # told once, not on every visit

    def test_the_documents_are_marked_with_the_upload_they_came_from(self):
        head = self.ready_binder()
        binder_id = head.binder_id
        self.client.post(self.url, form_for(head))
        made = AssociationDocument.objects.filter(is_staging=False)
        self.assertEqual(made.count(), 4)
        self.assertEqual({d.binder_id for d in made}, {binder_id})

    def test_progress_is_reported_as_each_document_is_cut_and_the_end_is_the_total(self):
        head = self.ready_binder()
        seen = []
        binders.confirm(head, [(s['kind'], s['title'], s.get('applies_to')) for s in head.sections], set(), self.user, progress=lambda d, t, m: seen.append((d, t, m)))
        self.assertEqual([(d, t) for d, t, _ in seen], [(0, 4), (1, 4), (2, 4), (3, 4), (4, 4)])
        self.assertTrue(seen[0][2].startswith('Cutting "'))
        self.assertEqual(seen[-1][2], 'Saving the files')

    @override_settings(DOCUMENT_JOBS_INLINE=False)
    def test_a_job_keeps_its_heartbeat_moving_while_it_runs(self):
        head = self.ready_binder()
        choices = [(s['kind'], s['title'], s.get('applies_to')) for s in head.sections]
        with mock.patch.object(docjobs.threading, 'Thread'):
            job = docjobs.submit(self.prop, 'binder_save', head.pk, 'Binder', self.user, prepare=lambda: binders.prepare_save(head, choices, set()))
        old = timezone.now() - timedelta(minutes=10)
        DocumentJob.objects.filter(pk=job.pk).update(heartbeat_at=old)
        docjobs.run(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, 'done')
        self.assertGreater(job.heartbeat_at, old + timedelta(minutes=9))

    def test_a_failure_is_recorded_in_plain_words_and_the_upload_is_kept_to_save_again(self):
        head = self.ready_binder()
        with mock.patch.object(binders, '_cut_and_store', side_effect=RuntimeError('storage is down')):
            r = self.client.post(self.url, form_for(head), follow=True)
        job = DocumentJob.objects.get()
        self.assertEqual(job.status, 'failed')
        self.assertIn('Your sorted list is kept', job.error)
        self.assertNotIn('storage is down', job.error)
        self.assertContains(r, 'Your sorted list is kept')
        self.assertContains(r, 'Needs a look')                                                 # back on the review screen, same button
        head.refresh_from_db()
        self.assertEqual(head.binder_state, 'needs_confirm')
        self.assertFalse(AssociationDocument.objects.filter(is_staging=False).exists())
        r2 = self.client.post(self.url, form_for(head), follow=True)                           # try again: a new job, which finishes
        self.assertContains(r2, 'Saved as 4 separate documents.')
        self.assertEqual(list(DocumentJob.objects.order_by('pk').values_list('status', flat=True)), ['failed', 'done'])

    def test_a_save_that_hits_something_unexpected_never_shows_the_raw_error(self):
        head = self.ready_binder()
        with mock.patch.object(binders, 'finalize', side_effect=ValueError('secret internals')):
            self.client.post(self.url, form_for(head))
        job = DocumentJob.objects.get()
        self.assertEqual(job.status, 'failed')
        self.assertNotIn('secret internals', job.error)
        self.assertIn('press the button to try again', job.error)

    def test_an_upload_that_is_gone_ends_the_job_with_a_plain_reason(self):
        head = self.ready_binder()
        job = self.active_job(head, status='queued')
        binders.cancel(head)
        docjobs.run(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, 'failed')
        self.assertIn('no longer there', job.error)

    def test_the_list_changed_is_refused_at_once_and_leaves_no_job(self):
        head = self.ready_binder()
        with self.assertRaises(documents.DocumentError):
            docjobs.submit(self.prop, 'binder_save', head.pk, 'Binder', self.user, prepare=lambda: binders.prepare_save(head, [('declaration', 'Only one', None)], set()))
        self.assertFalse(DocumentJob.objects.exists())
        head.refresh_from_db()
        self.assertEqual(head.binder_state, 'needs_confirm')


class OneJobAtATime(JobBase):
    def test_a_second_click_while_it_is_saving_does_not_start_a_second_job(self):
        head = self.ready_binder()
        job = self.active_job(head)
        AssociationDocument.objects.filter(pk=head.pk).update(binder_state='finalizing')
        r = self.client.post(self.url, form_for(head), follow=True)
        self.assertContains(r, 'already being saved')
        self.assertEqual(DocumentJob.objects.count(), 1)
        self.assertEqual(DocumentJob.objects.get().pk, job.pk)
        self.assertFalse(AssociationDocument.objects.filter(is_staging=False).exists())

    def test_the_database_itself_refuses_two_active_jobs_for_one_upload(self):
        head = self.ready_binder()
        self.active_job(head, status='queued')
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.active_job(head, status='running')
        # a finished one does not count: a new try may follow it, and a different upload may run alongside
        DocumentJob.objects.all().update(status='failed')
        self.active_job(head)
        other = self.sorted_file()
        self.active_job(other, kind='split_sorted')
        self.assertEqual(DocumentJob.objects.filter(status__in=DocumentJob.ACTIVE).count(), 2)

    def test_the_same_split_cannot_be_started_twice(self):
        doc = self.sorted_file()
        self.active_job(doc, kind='split_sorted')
        r = self.client.post(self.url, {'action': 'split_sorted', 'doc': doc.pk}, follow=True)
        self.assertContains(r, 'already being saved')
        self.assertEqual(DocumentJob.objects.count(), 1)
        self.assertTrue(AssociationDocument.objects.filter(pk=doc.pk).exists())

    def test_an_upload_that_is_being_saved_cannot_be_removed(self):
        head = self.ready_binder()
        self.active_job(head)
        r = self.client.post(self.url, {'action': 'delete_doc', 'doc': head.pk}, follow=True)
        self.assertContains(r, 'being saved right now')
        self.assertTrue(AssociationDocument.objects.filter(pk=head.pk).exists())


class AStoppedSave(JobBase):
    def test_a_job_with_no_heartbeat_for_15_minutes_is_stopped_and_the_upload_goes_back_to_review(self):
        head = self.ready_binder()
        job = self.active_job(head, beat_minutes=20)
        AssociationDocument.objects.filter(pk=head.pk).update(binder_state='finalizing')
        stopped = docjobs.recover_stale(self.prop)
        self.assertEqual([j.pk for j in stopped], [job.pk])
        job.refresh_from_db()
        head.refresh_from_db()
        self.assertEqual(job.status, 'failed')
        self.assertEqual(job.error, docjobs.STOPPED)
        self.assertEqual(head.binder_state, 'needs_confirm')
        self.assertTrue(head.sections)                                                         # the sorted list is kept
        self.assertIn('Press the button again', head.read_note)

    def test_a_job_that_is_alive_is_left_alone_however_long_the_save_began_ago(self):
        head = self.ready_binder()
        job = self.active_job(head, beat_minutes=3)
        DocumentJob.objects.filter(pk=job.pk).update(started_at=timezone.now() - timedelta(hours=2))
        self.assertEqual(docjobs.recover_stale(self.prop), [])
        job.refresh_from_db()
        self.assertEqual(job.status, 'running')

    def test_the_background_reader_does_not_cut_off_a_save_that_is_alive(self):
        head = self.ready_binder()
        self.active_job(head, beat_minutes=1)
        AssociationDocument.objects.filter(pk=head.pk).update(binder_state='finalizing', read_started_at=timezone.now() - timedelta(minutes=40))
        binders.process()
        head.refresh_from_db()
        self.assertEqual(head.binder_state, 'finalizing')

    def test_a_job_that_never_got_its_thread_is_stopped_after_two_minutes(self):
        head = self.ready_binder()
        young = self.active_job(head, status='queued', beat_minutes=1)
        self.assertEqual(docjobs.recover_stale(self.prop), [])
        DocumentJob.objects.filter(pk=young.pk).update(heartbeat_at=timezone.now() - timedelta(minutes=3))
        self.assertEqual(len(docjobs.recover_stale(self.prop)), 1)

    def test_opening_the_page_or_polling_calls_a_dead_job_dead_and_the_button_works_again(self):
        head = self.ready_binder()
        self.active_job(head, beat_minutes=30)
        AssociationDocument.objects.filter(pk=head.pk).update(binder_state='finalizing')
        data = self.client.get(self.status_url).json()
        self.assertFalse(data['active'])
        html = self.client.get(self.url).content.decode()
        self.assertIn('It stopped before it finished', html)
        self.assertIn('Looks right: save as separate documents', html)
        r = self.client.post(self.url, form_for(head), follow=True)
        self.assertContains(r, 'Saved as 4 separate documents.')

    def test_it_looks_stuck_after_two_minutes_and_the_person_can_stop_it_themselves(self):
        head = self.ready_binder()
        job = self.active_job(head, beat_minutes=5)
        AssociationDocument.objects.filter(pk=head.pk).update(binder_state='finalizing')
        html = self.client.get(self.url).content.decode()
        self.assertIn('It looks stuck: stop it', html)
        r = self.client.post(self.url, {'action': 'job_reset', 'job': job.pk}, follow=True)
        self.assertContains(r, 'Stopped. Your upload is kept')
        job.refresh_from_db()
        head.refresh_from_db()
        self.assertEqual((job.status, head.binder_state), ('failed', 'needs_confirm'))

    def test_stopping_a_job_that_is_still_moving_is_refused(self):
        head = self.ready_binder()
        job = self.active_job(head, beat_minutes=0)
        r = self.client.post(self.url, {'action': 'job_reset', 'job': job.pk}, follow=True)
        self.assertContains(r, 'still working')
        job.refresh_from_db()
        self.assertEqual(job.status, 'running')

    def test_a_save_that_really_finished_after_being_called_stopped_ends_as_done(self):
        head = self.ready_binder()
        job = self.active_job(head, beat_minutes=30)
        docjobs.recover_stale(self.prop)
        docjobs._finish(job.pk, result={'created': 4, 'left_out': 0})
        job.refresh_from_db()
        self.assertEqual((job.status, job.error), ('done', ''))


class NothingIsStoredTwice(JobBase):
    def test_a_save_that_stored_everything_but_was_cut_off_before_cleaning_up_only_cleans_up(self):
        head = self.ready_binder()
        plan, _ = binders.plan_files(head.sections)
        created = []
        binders._cut_and_store(self.prop, self.user, plan, binders.combined_pages(binders.parts_of(head.binder_id)), created, marker=head.binder_id)
        self.assertEqual(len(created), 4)                                                       # stored, parts still there: the site was restarted right here
        self.assertTrue(AssociationDocument.objects.filter(is_staging=True).exists())
        made, _ = binders.finalize(head, self.user)
        self.assertEqual(sorted(d.pk for d in made), sorted(d.pk for d in created))
        self.assertEqual(AssociationDocument.objects.filter(is_staging=False).count(), 4)      # not eight
        self.assertFalse(AssociationDocument.objects.filter(is_staging=True).exists())

    def test_the_same_goes_for_a_split_cut_off_before_the_original_was_removed(self):
        doc = self.sorted_file()
        pages, plan, _ = binders.prepare_split(doc)
        binders._cut_and_store(self.prop, self.user, plan, pages, [], marker=f'split:{doc.pk}')
        made, _ = binders.split_sorted(doc, self.user)
        self.assertEqual(len(made), 2)
        self.assertEqual(AssociationDocument.objects.filter(property=self.prop).count(), 2)    # the two, and the original is gone
        self.assertFalse(AssociationDocument.objects.filter(pk=doc.pk).exists())

    def test_a_run_that_fails_halfway_through_storing_leaves_nothing_and_a_retry_stores_each_once(self):
        head = self.ready_binder()
        real, calls = documents._store, []

        def flaky(*a, **kw):
            calls.append(1)
            if len(calls) == 3:
                raise RuntimeError('boom')
            return real(*a, **kw)
        with mock.patch.object(documents, '_store', side_effect=flaky):
            self.client.post(self.url, form_for(head))
        self.assertEqual(DocumentJob.objects.get().status, 'failed')
        self.assertFalse(AssociationDocument.objects.filter(is_staging=False).exists())
        self.client.post(self.url, form_for(head))
        self.assertEqual(AssociationDocument.objects.filter(is_staging=False).count(), 4)
        self.assertEqual(list(DocumentJob.objects.order_by('pk').values_list('status', flat=True)), ['failed', 'done'])


class SplittingAsAJob(JobBase):
    def test_the_split_button_records_a_job_and_the_page_reports_the_result_once(self):
        doc = self.sorted_file()
        r = self.client.post(self.url, {'action': 'split_sorted', 'doc': doc.pk}, follow=True)
        self.assertContains(r, 'Split into 2 separate documents')
        job = DocumentJob.objects.get()
        self.assertEqual((job.kind, job.status, job.result), ('split_sorted', 'done', {'created': 2, 'left_out': 0}))
        self.assertFalse(AssociationDocument.objects.filter(pk=doc.pk).exists())
        self.assertNotContains(self.client.get(self.url), 'Split into 2 separate documents')

    def test_a_file_that_cannot_be_split_is_refused_at_once_with_no_job(self):
        doc = self.sorted_file()
        AssociationDocument.objects.filter(pk=doc.pk).update(sort_status='proposed')
        r = self.client.post(self.url, {'action': 'split_sorted', 'doc': doc.pk}, follow=True)
        self.assertContains(r, 'Sort the file and confirm what it holds first')
        self.assertFalse(DocumentJob.objects.exists())

    def test_a_split_that_fails_keeps_the_original_and_says_so(self):
        doc = self.sorted_file()
        with mock.patch.object(binders, '_cut_and_store', side_effect=RuntimeError('down')):
            r = self.client.post(self.url, {'action': 'split_sorted', 'doc': doc.pk}, follow=True)
        self.assertContains(r, 'The original file is untouched')
        self.assertEqual(DocumentJob.objects.get().status, 'failed')
        self.assertTrue(AssociationDocument.objects.filter(pk=doc.pk).exists())

    def test_while_it_runs_the_row_shows_how_far_instead_of_the_button(self):
        doc = self.sorted_file()
        self.assertTrue(next(f for f in documents.files(self.prop) if f['doc'].pk == doc.pk)['splittable'])
        job = self.active_job(doc, kind='split_sorted')
        DocumentJob.objects.filter(pk=job.pk).update(done_count=1, total_count=2, message='Cutting "Bylaws"')
        row = next(f for f in documents.files(self.prop) if f['doc'].pk == doc.pk)
        self.assertFalse(row['splittable'])
        html = self.client.get(self.url).content.decode()
        self.assertIn(f'data-job="split_sorted-{doc.pk}"', html)
        self.assertIn('Splitting: Cutting &quot;Bylaws&quot; (1 of 2)', html)


class TheScreenFollowsTheJob(JobBase):
    def test_the_status_endpoint_reports_the_active_jobs_and_how_far_they_are(self):
        head = self.ready_binder()
        job = self.active_job(head)
        DocumentJob.objects.filter(pk=job.pk).update(done_count=2, total_count=4, message='Cutting "Bylaws"')
        data = self.client.get(self.status_url).json()
        self.assertTrue(data['active'])
        self.assertEqual(data['jobs'][0]['kind'], 'binder_save')
        self.assertEqual(data['jobs'][0]['target'], head.pk)
        self.assertEqual((data['jobs'][0]['done'], data['jobs'][0]['total']), (2, 4))
        self.assertEqual(data['jobs'][0]['label'], 'Cutting "Bylaws" (2 of 4)')
        DocumentJob.objects.all().update(status='done')
        self.assertFalse(self.client.get(self.status_url).json()['active'])

    def test_the_status_endpoint_is_for_staff_only(self):
        self.client.logout()
        self.assertNotEqual(self.client.get(self.status_url).status_code, 200)

    def test_the_page_polls_only_while_something_is_running_and_shows_the_chip(self):
        head = self.ready_binder()
        self.assertNotIn(self.status_url, self.client.get(self.url).content.decode())
        job = self.active_job(head)
        AssociationDocument.objects.filter(pk=head.pk).update(binder_state='finalizing')
        DocumentJob.objects.filter(pk=job.pk).update(done_count=1, total_count=4, message='Cutting "Declaration"')
        html = self.client.get(self.url).content.decode()
        self.assertIn(f'var url = "{self.status_url}"', html)
        self.assertIn(f'data-job="binder_save-{head.pk}"', html)
        self.assertIn('Saving: Cutting &quot;Declaration&quot; (1 of 4)', html)
        self.assertNotIn('name="action" value="delete_doc"', html.split('Saving: ')[1].split('</form>')[1])      # no Remove while it is being saved

    def test_the_polling_script_has_no_quote_that_breaks_it(self):
        head = self.ready_binder()
        self.active_job(head)
        html = self.client.get(self.url).content.decode()
        before, after = html.split('var url = "', 1)
        script = before[before.rindex('(function () {'):] + 'var url = "' + after.split('</script>')[0]
        self.assertIn("document.querySelector('[data-job=\"' + j.kind", script)
        self.assertEqual(script.count('{'), script.count('}'))
        self.assertEqual(script.count('('), script.count(')'))


class TheThread(JobBase):
    """Outside the tests the save runs on a daemon thread, started once the job row is committed."""

    @override_settings(DOCUMENT_JOBS_INLINE=False)
    def test_the_click_returns_at_once_and_starts_one_daemon_thread_after_the_commit(self):
        head = self.ready_binder()
        choices = [(s['kind'], s['title'], s.get('applies_to')) for s in head.sections]
        with mock.patch.object(docjobs.threading, 'Thread') as thread:
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                job = docjobs.submit(self.prop, 'binder_save', head.pk, 'Binder', self.user, prepare=lambda: binders.prepare_save(head, choices, set()))
            thread.assert_called_once()
            kwargs = thread.call_args.kwargs
            self.assertTrue(kwargs['daemon'])
            self.assertEqual(kwargs['target'], docjobs._thread_main)
            self.assertEqual(kwargs['args'], (job.pk,))
            thread.return_value.start.assert_not_called()                                       # not before the row is committed
            self.assertEqual(len(callbacks), 1)
            callbacks[0]()
            thread.return_value.start.assert_called_once()
        job.refresh_from_db()
        self.assertEqual(job.status, 'queued')                                                  # nothing was done inside the click
        self.assertFalse(AssociationDocument.objects.filter(is_staging=False).exists())
        head.refresh_from_db()
        self.assertEqual(head.binder_state, 'needs_confirm')

    @override_settings(DOCUMENT_JOBS_INLINE=False)
    def test_what_the_thread_runs_does_the_work_and_gives_its_database_connection_back(self):
        head = self.ready_binder()
        choices = [(s['kind'], s['title'], s.get('applies_to')) for s in head.sections]
        with mock.patch.object(docjobs.threading, 'Thread'):
            job = docjobs.submit(self.prop, 'binder_save', head.pk, 'Binder', self.user, prepare=lambda: binders.prepare_save(head, choices, set()))
        with mock.patch.object(docjobs, 'close_old_connections') as before, mock.patch.object(docjobs, 'connections') as handler:
            docjobs._thread_main(job.pk)
        before.assert_called_once()
        handler.close_all.assert_called_once()                                                  # a thread never leaves its own connection open
        job.refresh_from_db()
        self.assertEqual(job.status, 'done')
        self.assertEqual(AssociationDocument.objects.filter(is_staging=False).count(), 4)

    @override_settings(DOCUMENT_JOBS_INLINE=False)
    def test_a_job_is_only_taken_once_even_if_two_threads_run_it(self):
        head = self.ready_binder()
        choices = [(s['kind'], s['title'], s.get('applies_to')) for s in head.sections]
        with mock.patch.object(docjobs.threading, 'Thread'):
            job = docjobs.submit(self.prop, 'binder_save', head.pk, 'Binder', self.user, prepare=lambda: binders.prepare_save(head, choices, set()))
        docjobs.run(job.pk)
        docjobs.run(job.pk)                                                                     # the second finds it already taken
        self.assertEqual(AssociationDocument.objects.filter(is_staging=False).count(), 4)
        self.assertEqual(DocumentJob.objects.get().status, 'done')
