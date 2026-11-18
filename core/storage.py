"""A stable stand-in for storages['documents'] (see proptasks/settings.py's
STORAGES) to hand to a FileField's `storage=` kwarg.

Using `storage=storages['documents']` directly would work at request-serving
time (a real request always uses the live model class, which re-resolves
that lookup), but Django's migration autodetector deconstructs the actual
*resolved* Storage instance into each migration's frozen historical state —
so whichever environment happened to run `makemigrations` (local dev's
FileSystemStorage vs. production's Cloudinary-backed RawMediaCloudinaryStorage)
gets baked in, and running `makemigrations` anywhere else forever after
shows a noisy phantom diff for the same field, over and over.

DocumentStorage sidesteps that: it always deconstructs to a fixed,
argument-free `core.storage.DocumentStorage()` regardless of environment,
and only resolves storages['documents'] lazily, per attribute access, at
actual use time (via __getattr__) — so the choice of backend still comes
from settings.STORAGES exactly as before, it just never leaks into a
migration file.

Deliberately does NOT subclass Storage: Storage itself defines real
methods (save(), get_available_name(), the deliberately-unimplemented
exists()/delete()/url()/open() stubs meant for a real backend to
override) — inheriting them means Python's normal attribute lookup finds
those (raising NotImplementedError, or in save()'s case calling straight
into the unimplemented exists()) before __getattr__ ever gets a chance to
step in, since __getattr__ only fires when normal lookup finds nothing at
all. A bare object has none of that, so every single call Django actually
needs (save/open/url/exists/delete/path/size/generate_filename/...)
correctly falls through to __getattr__ and reaches the real backend."""
from django.core.files.storage import storages
from django.utils.deconstruct import deconstructible


MAX_STORED_NAME = 80        # a FileField column holds 100 characters and the storage adds a random suffix (about 12) to a name that is taken: stay well under


def clamp_stored_name(name, limit=MAX_STORED_NAME):
    """A stored file name (folders and all) no longer than `limit`: the file's own name is shortened, never its folder or its extension. A name that cannot be made to fit (a folder
    alone over the limit) is returned as it is."""
    name = str(name or '')
    if len(name) <= limit:
        return name
    folder, _, base = name.rpartition('/')
    stem, dot, ext = base.rpartition('.')
    if not dot or len(ext) > 8:
        stem, ext = base, ''
    else:
        ext = '.' + ext
    room = limit - len(folder) - (1 if folder else 0) - len(ext)
    if room < 8:
        return name
    stem = stem[:room].rstrip(' ._-') or 'file'
    return f'{folder}/{stem}{ext}' if folder else f'{stem}{ext}'


@deconstructible
class DocumentStorage:
    def generate_filename(self, filename):
        """Every upload field (documents, bills, statements, leases, attachments) names its file through here, so no name can be longer than the database column holds."""
        return clamp_stored_name(storages['documents'].generate_filename(filename))

    def save(self, name, content, max_length=None):
        """Store a file. A photo over about 10 MB (Cloudinary's limit) is shrunk to a JPEG that fits first (core/imaging.py); the name that comes back may end in .jpg for that reason."""
        from core.imaging import shrink_photo
        name, content = shrink_photo(name, content)
        return storages['documents'].save(name, content, max_length=max_length)

    def __getattr__(self, name):
        return getattr(storages['documents'], name)
