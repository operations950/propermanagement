"""One-time move of every file that's still sitting on the local media
volume (from before CLOUDINARY_CLOUD_NAME/API_KEY/API_SECRET were set —
see settings.py's STORAGES branch) over to Cloudinary, across every
FileField/ImageField in the app.

Deliberately skips this app's usual "back up to a gitignored JSON file
first" convention (see wipe_recurring_tickets.py, reset_supply_catalog.py)
for a concrete reason: the local volume that's being read from is the same
one that's completely full (OSError: [Errno 28] No space left on device),
so there's nowhere to write a backup file to even if we wanted one. Nothing
here deletes a database row or loses data on its own — reads from the old
location, writes to the new one, updates the field to point at the new
location. The optional --delete-local pass (a second, separate invocation)
only removes a local file after that same file's Cloudinary copy has
already been confirmed to exist.

Covers every FileField/ImageField in the app as of this build:
PropertyDocument.file, ContactDocument.file, TicketTemplateDocument.file,
TicketAttachment.file, ImportBatch.raw_file, Visit.signature_image,
VisitMedia.file, ProcessTemplateAttachment.file, ProcessAttachment.file."""
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage, default_storage
from django.core.management.base import BaseCommand, CommandError

FILE_FIELDS = [
    ('core', 'PropertyDocument', 'file'),
    ('core', 'ContactDocument', 'file'),
    ('tickets', 'TicketTemplateDocument', 'file'),
    ('tickets', 'TicketAttachment', 'file'),
    ('onsite', 'ImportBatch', 'raw_file'),
    ('onsite', 'Visit', 'signature_image'),
    ('onsite', 'VisitMedia', 'file'),
    ('processes', 'ProcessTemplateAttachment', 'file'),
    ('processes', 'ProcessAttachment', 'file'),
]


class Command(BaseCommand):
    help = (
        'Copies every file still on the local media volume to Cloudinary, '
        'and repoints each field at its new location. --apply to actually '
        'write anything; --delete-local (with --apply) also removes the '
        'now-copied local file, freeing volume space.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Actually copy files (default: dry run/report only).')
        parser.add_argument(
            '--delete-local', action='store_true',
            help='After a successful Cloudinary copy, delete the local file too. Only takes effect with --apply.',
        )

    def handle(self, *args, **options):
        if not (settings.CLOUDINARY_CLOUD_NAME and settings.CLOUDINARY_API_KEY and settings.CLOUDINARY_API_SECRET):
            raise CommandError(
                'CLOUDINARY_CLOUD_NAME/API_KEY/API_SECRET are not all set — nothing to migrate to. '
                'Set them first (see settings.py), which also switches default_storage to Cloudinary.'
            )

        apply_changes = options['apply']
        delete_local = options['delete_local']
        local_storage = FileSystemStorage(location=settings.MEDIA_ROOT)

        from django.apps import apps

        totals = {'seen': 0, 'blank': 0, 'already_migrated': 0, 'missing_locally': 0, 'copied': 0, 'failed': 0, 'deleted': 0}

        for app_label, model_name, field_name in FILE_FIELDS:
            model = apps.get_model(app_label, model_name)
            qs = model.objects.exclude(**{field_name: ''}).exclude(**{f'{field_name}__isnull': True})
            self.stdout.write(f'\n{model.__name__}.{field_name}: {qs.count()} row(s) with a value')

            for obj in qs.iterator():
                field_file = getattr(obj, field_name)
                name = field_file.name
                if not name:
                    totals['blank'] += 1
                    continue
                totals['seen'] += 1

                # Checked against the DESTINATION (Cloudinary), not the
                # source — this run's own copy uses the same name as the
                # original, so a rerun with --delete-local not yet applied
                # would otherwise still find the (untouched) local file and
                # copy it again under a suffixed name, silently duplicating
                # it on Cloudinary every time the command is re-run.
                if default_storage.exists(name):
                    totals['already_migrated'] += 1
                    continue

                if not local_storage.exists(name):
                    # Never actually landed locally in the first place —
                    # e.g. one of the disk-full upload failures from
                    # onsite/views.py's own error log, or a stale field
                    # value from some other cause.
                    totals['missing_locally'] += 1
                    continue

                if not apply_changes:
                    self.stdout.write(f'  would copy: {name}')
                    continue

                try:
                    with local_storage.open(name, 'rb') as f:
                        data = f.read()
                    new_name = default_storage.save(name, ContentFile(data))
                except Exception as exc:
                    totals['failed'] += 1
                    self.stderr.write(f'  FAILED {name}: {exc}')
                    continue

                field_file.name = new_name
                obj.save(update_fields=[field_name])
                totals['copied'] += 1
                self.stdout.write(f'  copied: {name} -> {new_name}')

                if delete_local:
                    try:
                        local_storage.delete(name)
                        totals['deleted'] += 1
                    except Exception as exc:
                        self.stderr.write(f'  copied but could not delete local {name}: {exc}')

        self.stdout.write('\n' + '-' * 40)
        for key, value in totals.items():
            self.stdout.write(f'{key}: {value}')
        if not apply_changes:
            self.stdout.write(self.style.WARNING('\nDry run — pass --apply to actually copy files.'))
