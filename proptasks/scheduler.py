"""Runs recurring-ticket generation and the daily supply digest on a timer
in-process — same shape as the vending-refund project's scheduler.py.

Reactive/AI ticket intake (Gmail polling, Quo call/text classification,
shared-calendar polling, Airbnb/VRBO booking polling) was fully reversed —
see the removal in this same change — so this module no longer schedules
any job that creates a Ticket automatically from an external source.
Quo contact sync/thread-linking are kept: they enrich Contact records and
communication history, not ticket creation, and stay valuable on their
own."""
import logging
import os
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from django.conf import settings

logger = logging.getLogger(__name__)
_scheduler = None


def _run_command(name):
    from django.core.management import call_command

    try:
        call_command(name)
    except Exception:
        logger.exception('%s failed', name)


def _run_sync_quo_contacts():
    _run_command('sync_quo_contacts')


def _run_link_quo_contact_threads():
    _run_command('link_quo_contact_threads')


def _run_generate_recurring_tickets():
    _run_command('generate_recurring_tickets')


def _run_resume_expired_wait_steps():
    _run_command('resume_expired_wait_steps')


def _run_sync_quickbooks_financials():
    _run_command('sync_quickbooks_financials')


def _run_generate_scheduled_visits():
    _run_command('generate_scheduled_visits')


def _run_sync_onsite_calendar():
    _run_command('sync_onsite_calendar')


def start():
    global _scheduler
    if _scheduler is not None:
        return

    # Avoid starting twice under the dev-server autoreloader, which forks a
    # child process (RUN_MAIN=true) after the initial parent process runs
    # AppConfig.ready() once already.
    if settings.DEBUG and os.environ.get('RUN_MAIN') != 'true':
        return

    _scheduler = BackgroundScheduler(daemon=True)

    _scheduler.add_job(
        _run_generate_recurring_tickets, 'interval',
        minutes=settings.RECURRING_TICKET_INTERVAL_MINUTES, next_run_time=datetime.now(),
    )
    _scheduler.add_job(
        _run_command, 'interval', minutes=settings.SUPPLY_DIGEST_INTERVAL_MINUTES,
        next_run_time=datetime.now(), args=['daily_supply_digest'],
    )
    _scheduler.add_job(_run_sync_quo_contacts, 'interval', minutes=settings.QUO_CONTACT_SYNC_INTERVAL_MINUTES)
    _scheduler.add_job(
        _run_link_quo_contact_threads, 'interval', minutes=settings.QUO_CONTACT_LINK_INTERVAL_MINUTES,
    )
    _scheduler.add_job(
        _run_resume_expired_wait_steps, 'interval', minutes=settings.PROCESS_WAIT_CHECK_INTERVAL_MINUTES,
    )
    _scheduler.add_job(
        _run_sync_quickbooks_financials, 'interval', minutes=settings.QUICKBOOKS_SYNC_INTERVAL_MINUTES,
    )
    _scheduler.add_job(
        _run_generate_scheduled_visits, 'interval',
        minutes=settings.ONSITE_GENERATE_VISITS_INTERVAL_MINUTES, next_run_time=datetime.now(),
    )
    _scheduler.add_job(
        _run_sync_onsite_calendar, 'interval', minutes=settings.ONSITE_CALENDAR_SYNC_INTERVAL_MINUTES,
    )

    _scheduler.start()
    logger.info('PropTasks scheduler started.')
