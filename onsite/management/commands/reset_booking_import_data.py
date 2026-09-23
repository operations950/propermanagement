"""Resets everything the Airbnb/VRBO CSV importer has ever derived, so a full re-upload of every
historical file rebuilds it from scratch on a clean slate — the "clear the cache and repull" the
user asked for after repeated reconciliation bugs left behind money data from earlier, partially-
buggy imports (stolen-match matching, missing pass-through lines, etc.) that a corrected re-upload
alone can't overwrite, because Booking's own "amounts only ever move up" rule (see
onsite/services/bookings.py::_save_amounts) means a corrected/lower figure from a fresh import can
never replace a wrong one already on file.

Deliberately does NOT delete Booking rows themselves, or touch anything on the QuickBooks/ledger
side (core.LedgerLine, core.MonthClose, core.ReconAcceptance, core.ReconTie are never touched by
this command) — per the explicit request: "I don't want to delete anything from QuickBooks."
Only Booking's own money fields (gross_amount, payout_amount, cleaning_fee, other_fees, tax_amount,
platform_fee, pass_through_amount, other_payout_amount, payout_date, payout_status, amount_source)
are reset to blank; every structural field (property, unit, guest, check_in/check_out, status,
external_uid, confirmed_real, ...) and every Visit/GuestRequest/Ticket linked to that booking is
left completely alone — Visit.booking is SET_NULL, not CASCADE, so nothing here can ever touch a
real cleaning's history even if a Booking row *were* deleted, but this command never deletes one
regardless. PayoutLine and PayoutBatch rows ARE deleted outright (they exist purely to be rebuilt
by re-importing) — --include-import-batches additionally clears the ImportBatch upload log (pure
history of past uploads, not read by anything else) if a clean "Recent Imports" list is also wanted.

Backs up everything it changes to a timestamped JSON file first (same restorable-via-loaddata
pattern as tickets.wipe_recurring_tickets), and defaults to a dry run — pass --apply to actually
change anything. Re-run safely: nothing here is auto-run from Procfile, this is a one-time reset a
human reviews the counts for and chooses to run, exactly like wipe_recurring_tickets.

After running with --apply, re-upload every historical Airbnb/VRBO file again (in date order) at
/onsite/import/ — the corrected import logic rebuilds every reservation's payout/pass-through/
resolution figures from what's actually in those files, this time without any earlier bug's leftover
data in the way. Existing ReconTie rows (manual deposit-to-reservation ties) for still-OPEN months
reference specific dated payout lines that this command deletes — re-check those after reimporting
(a re-import of the identical historical data should recreate the same dated lines, but a tie is
only as good as what it's currently pointing at); a CLOSED month's own frozen numbers are
untouched either way, since MonthClose freezes its totals independently of live Booking data."""
import json
from pathlib import Path

from django.conf import settings
from django.core import serializers
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from ...models import Booking, ImportBatch, PayoutBatch, PayoutLine

BACKUP_DIR = Path(settings.BASE_DIR) / 'backups'

MONEY_FIELDS = [
    'gross_amount', 'payout_amount', 'cleaning_fee', 'other_fees', 'tax_amount', 'platform_fee',
    'pass_through_amount', 'other_payout_amount', 'payout_date', 'payout_status', 'amount_source',
]
BLANK_STRING_FIELDS = {'payout_status', 'amount_source'}   # everything else in MONEY_FIELDS blanks to None


class Command(BaseCommand):
    help = (
        "Resets Booking's own money fields to blank and deletes every PayoutLine/PayoutBatch row, "
        "so re-uploading every historical Airbnb/VRBO file rebuilds the platform-side numbers "
        "cleanly — never touches Booking rows themselves, any linked Visit/Ticket, or anything on "
        "the QuickBooks/ledger side. Backs up what it changes to a JSON file first. Dry run by "
        "default — pass --apply to actually make the changes."
    )

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Actually make the changes (default is a dry-run report only).')
        parser.add_argument('--source', choices=['airbnb', 'vrbo', 'both'], default='both', help='Limit to one platform (default: both).')
        parser.add_argument('--property', type=int, dest='property_id', help='Limit to one Property id (default: every property).')
        parser.add_argument('--include-import-batches', action='store_true', help='Also delete the ImportBatch upload log (pure history, safe to clear, kept by default).')

    def handle(self, *args, **options):
        sources = ['airbnb', 'vrbo'] if options['source'] == 'both' else [options['source']]

        bookings_qs = Booking.objects.filter(source__in=sources)
        if options['property_id']:
            bookings_qs = bookings_qs.filter(property_id=options['property_id'])
        bookings = list(bookings_qs)
        # Only bookings that actually carry SOMETHING to reset — an untouched booking (never had
        # any amount imported for it) isn't worth writing to the backup file or the change count.
        bookings_with_money = [b for b in bookings if any(getattr(b, f) not in (None, '') for f in MONEY_FIELDS)]

        payout_lines = PayoutLine.objects.filter(booking__in=bookings)
        payout_batches = PayoutBatch.objects.filter(source__in=sources)
        import_batches = ImportBatch.objects.filter(source__in=sources) if options['include_import_batches'] else ImportBatch.objects.none()
        if options['include_import_batches'] and options['property_id']:
            import_batches = import_batches.filter(property_id=options['property_id'])

        self.stdout.write(f'  Booking money fields to reset: {len(bookings_with_money)} (of {len(bookings)} {"/".join(sources)} bookings total)')
        self.stdout.write(f'  PayoutLine rows to delete: {payout_lines.count()}')
        self.stdout.write(f'  PayoutBatch rows to delete: {payout_batches.count()}')
        if options['include_import_batches']:
            self.stdout.write(f'  ImportBatch rows to delete: {import_batches.count()}')
        else:
            self.stdout.write('  ImportBatch upload log: kept (pass --include-import-batches to clear it too)')

        if not bookings_with_money and not payout_lines.exists() and not payout_batches.exists() and not import_batches.exists():
            self.stdout.write(self.style.SUCCESS('Nothing to reset — already clean.'))
            return

        if not options['apply']:
            self.stdout.write(self.style.WARNING('Dry run — nothing changed, no backup written. Pass --apply to actually reset this data.'))
            return

        BACKUP_DIR.mkdir(exist_ok=True)
        backup_path = BACKUP_DIR / f'reset_booking_import_data_{timezone.now():%Y%m%d_%H%M%S}.json'
        backup_rows = list(bookings_with_money) + list(payout_lines) + list(payout_batches) + list(import_batches)
        backup_path.write_text(serializers.serialize('json', backup_rows, indent=2))
        self.stdout.write(f'Backed up {len(backup_rows)} row(s) (their state BEFORE this reset) to {backup_path}')

        with transaction.atomic():
            payout_line_count = payout_lines.count()
            payout_batch_count = payout_batches.count()
            import_batch_count = import_batches.count()
            payout_lines.delete()
            payout_batches.delete()
            import_batches.delete()
            for b in bookings_with_money:
                for field in MONEY_FIELDS:
                    setattr(b, field, '' if field in BLANK_STRING_FIELDS else None)
            Booking.objects.bulk_update(bookings_with_money, MONEY_FIELDS)

        self.stdout.write(self.style.SUCCESS(
            f'Reset complete: {len(bookings_with_money)} booking(s) had their money fields cleared, '
            f'{payout_line_count} PayoutLine row(s) and {payout_batch_count} PayoutBatch row(s) deleted'
            + (f', {import_batch_count} ImportBatch row(s) deleted' if options['include_import_batches'] else '')
            + '. Re-upload every historical Airbnb/VRBO file again to rebuild it — see this command\'s own docstring for details.'
        ))
