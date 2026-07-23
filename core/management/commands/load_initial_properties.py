"""Loads the real property list from core/fixtures/properties.json — but
only if the Property table is currently empty. Runs on every deploy (see
Procfile), same idempotent-by-design pattern as bootstrap_admin: safe to
re-run indefinitely because after the first successful load the table is
no longer empty, so every subsequent run is a no-op.

Deliberately does NOT keep re-syncing from the fixture on every deploy —
staff edit properties directly in production after the initial load
(address corrections, notes, deactivating a property, etc.), and
clobbering those edits back to a stale fixture snapshot on every future
deploy would be a real regression, not a convenience.
"""
from django.core.management import call_command
from django.core.management.base import BaseCommand

from core.models import Property


class Command(BaseCommand):
    help = 'Idempotently loads core/fixtures/properties.json if the Property table is empty.'

    def handle(self, *args, **options):
        if Property.objects.exists():
            self.stdout.write('Properties already exist — skipping initial load.')
            return
        call_command('loaddata', 'properties')
        self.stdout.write(self.style.SUCCESS(f'Loaded {Property.objects.count()} propert(y/ies).'))
