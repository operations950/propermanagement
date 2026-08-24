"""One-time cleanup: rewrites every Contact.phone value that isn't already
in this app's XXX-XXX-XXXX shape (core.models.phone_validator) into that
shape — most concretely, the raw E.164 numbers (e.g. "+15551234567") that
leaked in from Quo's API before intake/classifier.py::_get_reporter_contact
was fixed to route through review instead of writing Contact rows directly
(see that function's own docstring for the full story). Existing Contact
rows created via that old bypass are exactly what this cleans up — going
forward, nothing writes an unnormalized phone onto a real Contact.

Also surfaces (never auto-merges) any case where normalizing a phone would
make it collide with another Contact's already-normalized number — that's
a real duplicate this command's job is to reveal, not fix; use the existing
/contacts/duplicates/ merge screen for that once it's visible.

Backs up every changed row's prior phone value to a timestamped JSON file
first. Dry-run by default; --apply required to actually write. A phone
that can't be confidently normalized (not a recognizable 10-digit US
number) is reported and left untouched rather than blanked out."""
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from messaging.services import _to_dash_format

from ...models import Contact, PHONE_REGEX

BACKUP_DIR = Path(settings.BASE_DIR) / 'backups'


class Command(BaseCommand):
    help = 'Rewrites every Contact.phone not already in XXX-XXX-XXXX format into that shape. Dry-run by default.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Actually write the change (default: dry run).')

    def handle(self, *args, **options):
        malformed = [c for c in Contact.objects.exclude(phone='') if not PHONE_REGEX.fullmatch(c.phone)]

        to_fix, unfixable = [], []
        for contact in malformed:
            normalized = _to_dash_format(contact.phone)
            if normalized:
                to_fix.append((contact, normalized))
            else:
                unfixable.append(contact)

        by_phone = {}
        for contact in Contact.objects.exclude(phone=''):
            by_phone.setdefault(contact.phone, []).append(contact)

        self.stdout.write(f'{len(to_fix)} row(s) to normalize:')
        for contact, normalized in to_fix:
            collision = [
                c for c in by_phone.get(normalized, []) if c.pk != contact.pk
            ]
            collision_note = f'  <-- would then match existing "{collision[0].name}" ({collision[0].phone})' if collision else ''
            self.stdout.write(f'  {contact.name!r}: {contact.phone!r} -> {normalized!r}{collision_note}')

        if unfixable:
            self.stdout.write(f'\n{len(unfixable)} row(s) NOT confidently normalizable — left untouched:')
            for contact in unfixable:
                self.stdout.write(f'  {contact.name!r}: {contact.phone!r}')

        if not to_fix:
            self.stdout.write(self.style.SUCCESS('\nNothing to normalize.'))
            return

        if not options['apply']:
            self.stdout.write(self.style.WARNING(f'\nDry run — {len(to_fix)} row(s) would be written. Re-run with --apply to write them.'))
            return

        backup_rows = [
            {'pk': contact.pk, 'name': contact.name, 'prior_phone': contact.phone, 'new_phone': normalized}
            for contact, normalized in to_fix
        ]
        BACKUP_DIR.mkdir(exist_ok=True)
        backup_path = BACKUP_DIR / f'normalize_contact_phones_{timezone.now():%Y%m%d_%H%M%S}.json'
        with open(backup_path, 'w') as f:
            json.dump(backup_rows, f, indent=2)

        for contact, normalized in to_fix:
            contact.phone = normalized
            contact.save(update_fields=['phone'])

        self.stdout.write(self.style.SUCCESS(f'\nNormalized {len(to_fix)} row(s). Backup of prior values: {backup_path}'))
        if unfixable:
            self.stdout.write(self.style.WARNING(f'{len(unfixable)} row(s) could not be normalized — review those manually.'))
