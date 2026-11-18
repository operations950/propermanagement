"""Free the Railway files volume: every file still on it that Cloudinary already holds is deleted from it, and one Cloudinary does not hold yet is copied up first.

    manage.py cleanup_local_media                  (a report: sizes by folder, what is safe to delete, what would be copied, what nothing refers to; nothing is changed)
    manage.py cleanup_local_media --apply          (deletes the local copy of each file Cloudinary holds, the same size; copies the rest up, then deletes them)
    manage.py cleanup_local_media --apply --orphans   (also deletes files no database row refers to)

Unlike migrate_local_media_to_cloudinary (a fixed list of eight fields, and --delete-local only for what that run copied), this looks at EVERY file field in the app and at every file on the volume:
a file a database row names, that Cloudinary answers for with the same size, is safe to delete from the volume. A file whose Cloudinary copy is missing or a different size is copied up first (the
row is repointed if the stored name changes) and only then deleted. A file no row names is reported and kept unless --orphans is given. Safe to run again.

It refuses to run when Cloudinary is not set up (without it the "cloud" and the volume are the same place and everything would look safe to delete)."""
import os

from django.apps import apps
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db import models


def _mb(n):
    return f'{n / (1024 * 1024):,.1f} MB'


def file_fields():
    """[(model, field)] for every FileField / ImageField in the project."""
    out = []
    for model in apps.get_models():
        for field in model._meta.get_fields():
            if isinstance(field, models.FileField):
                out.append((model, field))
    return out


def referenced_names():
    """{stored name: [(model, field)]} over every database row that has a file."""
    refs = {}
    for model, field in file_fields():
        for name in model.objects.exclude(**{field.name: ''}).exclude(**{f'{field.name}__isnull': True}).values_list(field.name, flat=True).iterator():
            if name:
                refs.setdefault(name, []).append((model, field))
    return refs


def local_files(root):
    """[(relative name with forward slashes, size)] for every file under `root`."""
    out = []
    for folder, _, names in os.walk(root):
        for n in names:
            path = os.path.join(folder, n)
            try:
                out.append((os.path.relpath(path, root).replace(os.sep, '/'), os.path.getsize(path)))
            except OSError:
                continue
    return out


class Command(BaseCommand):
    help = 'Delete files from the local media volume that Cloudinary already holds (copying any it does not hold first). Report only unless --apply.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Actually copy and delete (without it, nothing is changed).')
        parser.add_argument('--orphans', action='store_true', help='With --apply, also delete files no database row refers to.')

    def handle(self, *args, **opts):
        if not (settings.CLOUDINARY_CLOUD_NAME and settings.CLOUDINARY_API_KEY and settings.CLOUDINARY_API_SECRET):
            raise CommandError('Cloudinary is not set up (CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET): nothing here would be safe to delete.')
        root = str(settings.MEDIA_ROOT)
        if not os.path.isdir(root):
            self.stdout.write(f'No media folder at {root}: nothing to do.')
            return
        apply, orphans = opts['apply'], opts['orphans']
        w = self.stdout.write
        files = local_files(root)
        total = sum(s for _, s in files)
        w(f'{len(files)} files on the volume, {_mb(total)} in all.')
        folders = {}
        for name, size in files:
            top = name.split('/')[0] if '/' in name else '(top level)'
            n, s = folders.get(top, (0, 0))
            folders[top] = (n + 1, s + size)
        for top, (n, s) in sorted(folders.items(), key=lambda kv: -kv[1][1])[:15]:
            w(f'    {top:40} {n:6} files {_mb(s):>12}')
        refs = referenced_names()
        safe, to_copy, orphan, mismatched = [], [], [], []
        for name, size in files:
            rows = refs.get(name)
            if not rows:
                orphan.append((name, size))
                continue
            storage = rows[0][1].storage
            try:
                held = storage.exists(name)
                there = storage.size(name) if held else None
            except Exception as exc:
                self.stderr.write(f'  could not ask Cloudinary about {name}: {exc}')
                to_copy.append((name, size, rows))
                continue
            if held and (there is None or there == size):
                safe.append((name, size))
            elif held:
                mismatched.append((name, size, rows))
                to_copy.append((name, size, rows))
            else:
                to_copy.append((name, size, rows))
        w('')
        w(f'Safe to delete (Cloudinary holds the same file): {len(safe)} files, {_mb(sum(s for _, s in safe))}')
        w(f'Not on Cloudinary yet (would be copied up, then deleted): {len(to_copy)} files, {_mb(sum(s for _, s, _ in to_copy))}' + (f' ({len(mismatched)} are there with a different size)' if mismatched else ''))
        w(f'Nothing in the database refers to them (kept unless --orphans): {len(orphan)} files, {_mb(sum(s for _, s in orphan))}')
        for name, size in sorted(orphan, key=lambda x: -x[1])[:10]:
            w(f'    orphan {_mb(size):>10}  {name}')
        if not apply:
            w('Report only: nothing was changed. Add --apply to free the space.')
            return
        freed = copied = failed = 0
        for name, size in safe:
            try:
                os.remove(os.path.join(root, name))
                freed += size
            except OSError as exc:
                failed += 1
                self.stderr.write(f'  could not delete {name}: {exc}')
        for name, size, rows in to_copy:
            storage = rows[0][1].storage
            try:
                with open(os.path.join(root, name), 'rb') as f:
                    data = f.read()
                new_name = storage.save(name, ContentFile(data))
                if new_name != name:
                    for model, field in rows:
                        model.objects.filter(**{field.name: name}).update(**{field.name: new_name})
                os.remove(os.path.join(root, name))
                freed += size
                copied += 1
            except Exception as exc:
                failed += 1
                self.stderr.write(f'  FAILED to copy {name}: {exc}')
        removed = 0
        if orphans:
            for name, size in orphan:
                try:
                    os.remove(os.path.join(root, name))
                    freed += size
                    removed += 1
                except OSError as exc:
                    failed += 1
                    self.stderr.write(f'  could not delete {name}: {exc}')
        w(f'Done: {len(safe)} deleted (already on Cloudinary), {copied} copied up and deleted, {removed} orphans deleted, {failed} failed; {_mb(freed)} freed.')
