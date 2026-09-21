"""Reads the guest rating from each Airbnb / VRBO link that is due (about monthly). Safe to run any time:
only links whose last reading is a month old (or that were never read) are opened, a few seconds apart."""
from django.core.management.base import BaseCommand

from core import listings


class Command(BaseCommand):
    help = 'Read the guest ratings of the Airbnb / VRBO listing links that are due.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=25, help='Most links to read in one run.')
        parser.add_argument('--pause', type=float, default=8, help='Seconds to wait between pages.')

    def handle(self, *args, **options):
        result = listings.run_due(limit=options['limit'], pause=options['pause'])
        self.stdout.write(f'Listing ratings: {result["read"]} read, {result["failed"]} could not be read.')
