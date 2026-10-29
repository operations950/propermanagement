"""Read the association documents that are waiting (each is read once and its words kept), then sort the ones that were uploaded without saying what they are.

Runs every minute from the scheduler; safe to run by hand and from two places at once.

  python manage.py read_documents
"""
from django.core.management.base import BaseCommand

from accounting import docreader


class Command(BaseCommand):
    help = 'Read waiting association documents once and sort them.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=3, help='how many documents to read in one run')

    def handle(self, *args, **options):
        n = docreader.process_waiting(limit=options['limit'], log=self.stdout.write)
        if n:
            self.stdout.write(f'{n} document(s) read.')
