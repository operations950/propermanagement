"""One-time cleanup ahead of remapping every Airbnb/VRBO listing name to a
Unit now that core.Unit and PropertyListingName.unit exist (see the
"Associate Airbnb/VRBO listing names with units" build) — wipes every
core.PropertyListingName row so staff can remap each one fresh through the
(now unit-aware) resolution flows instead of hunting down and fixing each
existing unit-less row by hand.

Confirmed by a full-codebase reference sweep: nothing else holds a foreign
key onto PropertyListingName. Deleting these rows only clears the "listing
name -> property/unit" resolution mapping used by future booking imports —
no Booking/Visit/Ticket history is touched, since each of those already
carries its own resolved property/unit directly, set at import time. Any
Airbnb/VRBO listing name imported again after this runs simply comes back
as "unmatched" and routes through the unit-aware resolution screen in
onsite/templates/onsite/booking_import_preview.html (or gets re-added
manually from property_detail with a unit picked).

Backs up every deleted row to a timestamped JSON file first (see
BACKUP_DIR) — restorable with Django's loaddata if anything here ever
needs to be recovered. Dry-run by default; --apply is required to
actually delete."""
from pathlib import Path

from django.conf import settings
from django.core import serializers
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from core.models import PropertyListingName

BACKUP_DIR = Path(settings.BASE_DIR) / 'backups'


class Command(BaseCommand):
    help = (
        'Deletes every core.PropertyListingName row (all Airbnb/VRBO listing names) so they '
        'can be remapped to units from scratch. Backs up everything to a JSON file first. '
        'Dry-run by default — pass --apply to actually delete.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='Actually delete (and back up) — without this, only reports what would be deleted.',
        )

    def handle(self, *args, **options):
        listings = list(
            PropertyListingName.objects.select_related('property', 'unit')
            .order_by('property__name', 'platform', 'name'),
        )
        count = len(listings)

        if count == 0:
            self.stdout.write(self.style.SUCCESS('Nothing to clear — already empty.'))
            return

        by_platform = {}
        for ln in listings:
            by_platform.setdefault(ln.platform, 0)
            by_platform[ln.platform] += 1
        for platform, n in sorted(by_platform.items()):
            self.stdout.write(f'  {platform}: {n} listing name(s)')
        for ln in listings:
            unit_note = f' [unit: {ln.unit.label}]' if ln.unit_id else ''
            self.stdout.write(f'    - "{ln.name}" ({ln.get_platform_display()}) -> {ln.property.name}{unit_note}')

        if not options['apply']:
            self.stdout.write(self.style.WARNING(
                f'Dry run — {count} row(s) would be deleted. Nothing deleted, no backup written. '
                'Pass --apply to actually delete.',
            ))
            return

        BACKUP_DIR.mkdir(exist_ok=True)
        backup_path = BACKUP_DIR / f'clear_listing_names_{timezone.now():%Y%m%d_%H%M%S}.json'
        backup_path.write_text(serializers.serialize('json', listings, indent=2))
        self.stdout.write(f'Backed up {count} row(s) to {backup_path}')

        with transaction.atomic():
            PropertyListingName.objects.filter(pk__in=[ln.pk for ln in listings]).delete()

        self.stdout.write(self.style.SUCCESS(
            f'Cleared {count} listing name(s). Every Airbnb/VRBO listing name will come back as '
            '"unmatched" on the next import and can be remapped with a unit.',
        ))
