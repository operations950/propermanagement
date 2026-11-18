"""The document sorter's saves, run in the background.

Saving a confirmed big upload as one file per document (binders.finalize), or splitting a sorted file (binders.split_sorted), reads and cuts every page and stores every file. For a long
governing-documents binder that is longer than the web server waits for one request (gunicorn gives a request 90 seconds, and kills the worker after that, leaving the save half-done). So the
person's click only records a DocumentJob and starts a thread; the page then shows how far it is and tells them how it ended.

Hosting facts this is built around: one gunicorn process with threads, no queue worker, a restart on every deploy. So:
  * The job is a row. A page reload, a second browser tab or a restart never loses it.
  * The work runs on a daemon thread started after the row is committed. A restart kills the thread, never the row: a job with no sign of life for 15 minutes (the heartbeat is written as each
    document is cut) is called stopped, the upload goes back to the review screen with the sorted list kept, and the same button saves it again. The person can also say "it looks stuck" after 2 minutes.
  * Only one job per upload at a time, enforced by the database (a unique constraint on the active ones), so a double click or two tabs cannot start two.
  * Nothing is ever stored twice: binders.finalize / split_sorted store every document in one transaction, mark them with the upload's id, and on a retry that finds them already there only clean up.
  * Tests (and any caller that wants it) can set settings.DOCUMENT_JOBS_INLINE = True to run the job inside the call, so the outcome is deterministic.
"""
import logging
import threading
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.db import IntegrityError, close_old_connections, connections, transaction
from django.utils import timezone

from .documents import DocumentError
from .models import AssociationDocument, DocumentJob

logger = logging.getLogger(__name__)

STALE = timedelta(minutes=15)             # an active job with no heartbeat for this long has stopped (the site was restarted, or the save hung)
STALE_QUEUED = timedelta(minutes=2)       # a job still "waiting to start" this long never got its thread
STUCK_PATIENCE = timedelta(minutes=2)     # what "it looks stuck: start over" asks for
STOPPED = 'It stopped before it finished (the site was restarted, or the save took too long). Nothing was lost: press the button again.'
STILL_GOING = 'That is already being saved. It finishes on its own: this page shows how far it is.'


def inline():
    return bool(getattr(settings, 'DOCUMENT_JOBS_INLINE', False))


# ---------------------------------------------------------------- asking for a job

def active_job(kind, target_id):
    return DocumentJob.objects.filter(kind=kind, target_id=target_id, status__in=DocumentJob.ACTIVE).first()


def submit(association, kind, target_id, title, user=None, prepare=None):
    """Record a job and start it. `prepare` (optional) runs in the same transaction as the record, so if it raises (the list changed, the file cannot be split) there is no job. Raises
    DocumentError in plain words when the same save is already running."""
    recover_stale(association)                                  # a save the site was restarted under must not block the next try
    try:
        with transaction.atomic():
            job = DocumentJob.objects.create(association=association, kind=kind, target_id=target_id, title=(title or '')[:200], created_by=user if getattr(user, 'pk', None) else None,
                                             message='Waiting to start', heartbeat_at=timezone.now())
            if prepare is not None:
                prepare()
    except IntegrityError:
        raise DocumentError(STILL_GOING)
    _start(job)
    return job


def _start(job):
    if inline():
        run(job.pk)
        return
    thread = threading.Thread(target=_thread_main, args=(job.pk,), daemon=True, name=f'docjob-{job.pk}')
    transaction.on_commit(thread.start)                          # after the row is committed (at once, outside a wider transaction), so the thread can see it


def _thread_main(job_id):
    close_old_connections()
    try:
        run(job_id)
    finally:
        connections.close_all()                                  # this thread's own connection: a thread that ends must not leave one open (the pool of a long-lived server would leak)


# ---------------------------------------------------------------- doing the work

def run(job_id):
    """Do one job from start to finish and record how it ended. Never raises."""
    from . import binders
    job = DocumentJob.objects.filter(pk=job_id).first()
    if job is None or not DocumentJob.objects.filter(pk=job_id, status='queued').update(status='running', started_at=timezone.now(), heartbeat_at=timezone.now(), message='Starting'):
        return                                                   # gone, or somebody else already took it
    job.refresh_from_db()

    def progress(done, total, message):
        DocumentJob.objects.filter(pk=job_id).update(done_count=done, total_count=total, message=(message or '')[:200], heartbeat_at=timezone.now())

    try:
        if job.kind == DocumentJob.Kind.BINDER_SAVE:
            head = AssociationDocument.objects.get(pk=job.target_id, is_staging=True, binder_order=0, property=job.association)
            made, left_out = binders.finalize(head, job.created_by, progress=progress)
        elif job.kind == DocumentJob.Kind.SPLIT_SORTED:
            doc = AssociationDocument.objects.get(pk=job.target_id, is_staging=False, property=job.association)
            made, left_out = binders.split_sorted(doc, job.created_by, progress=progress)
        else:
            raise DocumentError('Unknown kind of save.')
    except AssociationDocument.DoesNotExist:
        _finish(job_id, failed='That upload is no longer there: it may already have been saved or removed.')
    except Exception as exc:
        if not isinstance(exc, DocumentError):
            logger.exception('The document job %s failed', job_id)
        _finish(job_id, failed=str(exc) if isinstance(exc, DocumentError) else 'It could not be saved just now. Your upload is kept: press the button to try again.')
    else:
        _finish(job_id, result={'created': len(made), 'left_out': len(left_out)})


def _finish(job_id, result=None, failed=''):
    fields = {'finished_at': timezone.now(), 'heartbeat_at': timezone.now(), 'seen': False}
    if failed:
        fields.update(status='failed', error=failed[:300], message='')
    else:
        fields.update(status='done', error='', message='Saved', result=result or {})
    DocumentJob.objects.filter(pk=job_id).update(**fields)       # even over "stopped": a save that really finished is a finished save


# ---------------------------------------------------------------- a save that stopped

def recover_stale(association=None, patience=None, job_id=None):
    """Call active jobs with no sign of life stopped, and put an upload that was left 'saving' back on the review screen. Returns the jobs it stopped. `patience` shortens the wait
    (the person pressed "it looks stuck"); `job_id` limits it to one job."""
    now = timezone.now()
    out = []
    jobs = DocumentJob.objects.filter(status__in=DocumentJob.ACTIVE)
    if association is not None:
        jobs = jobs.filter(association=association)
    if job_id is not None:
        jobs = jobs.filter(pk=job_id)
    for job in jobs:
        limit = (patience or STALE_QUEUED) if job.status == 'queued' else (patience or STALE)
        if (job.heartbeat_at or job.created_at) >= now - limit:
            continue
        if not DocumentJob.objects.filter(pk=job.pk, status__in=DocumentJob.ACTIVE).update(status='failed', error=STOPPED, message='', finished_at=now, seen=False):
            continue
        if job.kind == DocumentJob.Kind.BINDER_SAVE:
            AssociationDocument.objects.filter(pk=job.target_id, binder_state='finalizing').update(binder_state='needs_confirm', read_note='Saving the documents stopped halfway. Press the button again.')
        out.append(job)
    return out


# ---------------------------------------------------------------- for the screen

def payload(association):
    """What the page polls: the active jobs and how far they are, and whether anything is still going. Also calls a dead job dead."""
    recover_stale(association)
    jobs = DocumentJob.objects.filter(association=association, status__in=DocumentJob.ACTIVE)
    return {'active': bool(jobs), 'jobs': [{'id': j.pk, 'kind': j.kind, 'target': j.target_id, 'status': j.status, 'label': j.label, 'done': j.done_count, 'total': j.total_count} for j in jobs]}


def announce(request, association):
    """Tell the person, once, how each finished job ended (the page shows it as a message). Anything not finished is left for later."""
    for job in DocumentJob.objects.filter(association=association, status__in=('done', 'failed'), seen=False).order_by('finished_at'):
        if job.status == 'failed':
            messages.error(request, job.error or 'The save did not finish.')
        else:
            made, left = (job.result or {}).get('created', 0), (job.result or {}).get('left_out', 0)
            note = f' Left out {left} second cop{"ies" if left != 1 else "y"}.' if left else ''
            if job.kind == DocumentJob.Kind.SPLIT_SORTED:
                messages.success(request, f'Split into {made} separate document{"s" if made != 1 else ""}; the combined file is removed.{note}')
            else:
                messages.success(request, f'Saved as {made} separate document{"s" if made != 1 else ""}.{note}')
        DocumentJob.objects.filter(pk=job.pk).update(seen=True)
