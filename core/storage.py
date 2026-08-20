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


@deconstructible
class DocumentStorage:
    def __getattr__(self, name):
        return getattr(storages['documents'], name)
