"""Knowing when email cannot be sent, and telling the admins.

When a send fails because the mailbox is not connected, or Google has cancelled its sign-in (a password change does that), the failure is recorded here. Admins then see a banner at the top
of every page, with a Reconnect button, until it is fixed: a reconnect (intake/views.py::gmail_callback) or a send that works again closes it. The person whose email did not go gets a plain
message instead of an error page (core/middleware.py::MailNotConnectedMiddleware). A problem in here must never take a page or a send down.
"""
import logging

from django.core.cache import cache
from django.utils import timezone

from .models import MailProblem

logger = logging.getLogger(__name__)
CACHE_KEY = 'mailproblem:open'
CACHE_SECONDS = 30


def _forget():
    cache.delete(CACHE_KEY)


def record_failure(mailbox_email, reason, detail='', user=None):
    """Open (or add to) the problem for a mailbox. Returns it, or None if it could not be saved."""
    try:
        problem = MailProblem.objects.filter(mailbox_email=mailbox_email or '', resolved_at__isnull=True).first()
        if problem is None:
            problem = MailProblem.objects.create(mailbox_email=mailbox_email or '', reason=reason, detail=(detail or '')[:300], last_user=user if getattr(user, 'pk', None) else None)
        else:
            problem.fail_count += 1
            problem.last_failed_at = timezone.now()
            problem.reason = reason
            if detail:
                problem.detail = detail[:300]
            if getattr(user, 'pk', None):
                problem.last_user = user
            problem.save(update_fields=['fail_count', 'last_failed_at', 'reason', 'detail', 'last_user'])
        _forget()
        return problem
    except Exception:
        logger.exception('Mail problem could not be recorded')
        return None


def note_user(user):
    """The person whose email did not go (the send itself does not know who asked)."""
    try:
        if getattr(user, 'pk', None):
            MailProblem.objects.filter(resolved_at__isnull=True).update(last_user=user)
            _forget()
    except Exception:
        logger.exception('Mail problem user could not be noted')


def resolve(mailbox_email=None):
    """Close the open problem (for one mailbox, or every one). Safe to call all the time."""
    try:
        qs = MailProblem.objects.filter(resolved_at__isnull=True)
        if mailbox_email:
            qs = qs.filter(mailbox_email__in=[mailbox_email, ''])
        n = qs.update(resolved_at=timezone.now())
        if n:
            _forget()
        return n
    except Exception:
        logger.exception('Mail problem could not be closed')
        return 0


def open_problem():
    """The open problem to show admins, or None. Kept for 30 seconds so the banner costs almost nothing on a page."""
    cached = cache.get(CACHE_KEY)
    if cached is not None:
        return cached or None
    try:
        problem = MailProblem.objects.filter(resolved_at__isnull=True).order_by('-last_failed_at').first()
    except Exception:
        return None
    cache.set(CACHE_KEY, problem or False, CACHE_SECONDS)
    return problem


def note_success(mailbox_email):
    """A send worked: if a problem was open for that mailbox, it is over."""
    if cache.get(CACHE_KEY) is False:
        return
    resolve(mailbox_email)
