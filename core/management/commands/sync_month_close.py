"""Make the month-end close's compliance items (one per step, due on its working day) and finish the ones whose steps are done. Safe to run often."""
from django.core.management.base import BaseCommand

from core import month_close_project


class Command(BaseCommand):
    help = 'Make and finish the month-end close items on the compliance screen.'

    def handle(self, *args, **options):
        made, finished = month_close_project.sync()
        if made or finished:
            self.stdout.write(f'month-end close: {made} items made, {finished} finished')
