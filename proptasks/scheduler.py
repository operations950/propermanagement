"""Runs the reactive-intake polls, recurring-ticket generation, and the
daily supply digest on a timer in-process, since none of Gmail/Quo/Calendar/
Airbnb/VRBO offer a webhook we can just listen on — same shape as the
vending-refund project's scheduler.py."""
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


def _run_poll_fake():
    _run_command('poll_fake')


def _run_poll_gmail():
    _run_command('poll_gmail')


def _run_poll_quo():
    _run_command('poll_quo')


def _run_poll_calendar():
    _run_command('poll_calendar')


def _run_poll_airbnb():
    _run_command('poll_airbnb')


def _run_poll_vrbo():
    _run_command('poll_vrbo')


def _run_generate_recurring_tickets():
    _run_command('generate_recurring_tickets')


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

    if settings.RUN_FAKE_ADAPTER:
        _scheduler.add_job(
            _run_poll_fake, 'interval', minutes=settings.FAKE_POLL_INTERVAL_MINUTES, next_run_time=datetime.now(),
        )
    _scheduler.add_job(
        _run_generate_recurring_tickets, 'interval',
        minutes=settings.RECURRING_TICKET_INTERVAL_MINUTES, next_run_time=datetime.now(),
    )
    _scheduler.add_job(
        _run_command, 'interval', minutes=settings.SUPPLY_DIGEST_INTERVAL_MINUTES,
        next_run_time=datetime.now(), args=['daily_supply_digest'],
    )
    # Real-source polls run on the same cadence as the fake adapter (except
    # Quo and Gmail, which have their own dedicated intervals); they're
    # no-ops until their credentials are configured (see intake/adapters/*.py).
    _scheduler.add_job(_run_poll_gmail, 'interval', minutes=settings.GMAIL_POLL_INTERVAL_MINUTES)
    _scheduler.add_job(_run_poll_quo, 'interval', minutes=settings.QUO_POLL_INTERVAL_MINUTES)
    _scheduler.add_job(_run_poll_calendar, 'interval', minutes=settings.FAKE_POLL_INTERVAL_MINUTES)
    _scheduler.add_job(_run_poll_airbnb, 'interval', minutes=settings.FAKE_POLL_INTERVAL_MINUTES)
    _scheduler.add_job(_run_poll_vrbo, 'interval', minutes=settings.FAKE_POLL_INTERVAL_MINUTES)

    _scheduler.start()
    logger.info('PropTasks scheduler started.')
