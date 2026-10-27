"""Loads St Andrews GLEN into the association books as placeholder data (see accounting/seed_glen.py). Safe to run on every deploy.

It is chained into the Procfile, so it must never stop a deploy: any problem is logged and skipped (the load is one transaction, so nothing half-done is kept).
"""
import logging

from django.core.management.base import BaseCommand

from accounting import seed_glen

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Load St Andrews GLEN (owners, balances, the September bank statement) into the association books.'

    def handle(self, *args, **options):
        try:
            seed_glen.seed(log=self.stdout.write)
        except Exception:
            logger.exception('Could not load St Andrews GLEN into the association books')
            self.stdout.write('St Andrews GLEN could not be loaded; see the log. Nothing was changed.')
