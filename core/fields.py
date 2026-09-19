"""EncryptedTextField — a TextField whose value is AES-encrypted in the
database and transparently decrypted on load, so every existing
`token.refresh_token`-style read/write elsewhere in the app keeps working
unchanged while the stored value is ciphertext.

Added for Intuit's QuickBooks security requirements
(developer.intuit.com → publish-app → security-requirements): "Encrypt and
store the refresh token and realmID in persistent memory. Encrypt the
refresh token with a symmetric algorithm (3DES or AES). AES is preferred.
Store your AES key in your app, in a separate configuration file."

Uses Fernet (the `cryptography` library's AES-128-CBC + HMAC-SHA256
authenticated encryption — AES, and tamper-evident on top). The key is NOT
stored in the database next to the data it protects: it comes from the
TOKEN_ENCRYPTION_KEY environment variable (a Railway variable, i.e. outside
the DB), falling back to a key derived from Django's SECRET_KEY (also an
environment variable) so encryption works the moment this deploys, with no
extra setup. Either way the practical consequence of changing that key is
the same and is harmless here: stored tokens can no longer be decrypted, so
the affected integration just needs to be reconnected (QuickBooks already
requires that about every 100 days regardless).

Values are stored as "enc1:<fernet token>". A value with no such prefix is
treated as legacy plaintext and returned as-is, then gets encrypted the
next time its row is saved — so adding this to a table that already has
rows can't lock anyone out of an existing connection."""
import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models

logger = logging.getLogger(__name__)

_PREFIX = 'enc1:'


def _fernet():
    configured = getattr(settings, 'TOKEN_ENCRYPTION_KEY', '') or ''
    # Any string works as TOKEN_ENCRYPTION_KEY — it's hashed down to a valid
    # 32-byte key, so nobody has to generate a special format by hand. The
    # domain-separation prefix keeps this derived key distinct from any other
    # use of the same underlying secret.
    material = configured or settings.SECRET_KEY
    digest = hashlib.sha256(b'proptasks-token-encryption-v1:' + material.encode('utf-8')).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_value(plaintext):
    return _PREFIX + _fernet().encrypt(plaintext.encode('utf-8')).decode('ascii')


def decrypt_value(stored):
    """Plaintext for a stored value. Legacy (un-prefixed) values pass
    through unchanged. A value that can't be decrypted (the key changed)
    comes back as '' with an error logged — callers already treat an empty
    token as "not connected / needs reconnecting"."""
    if not stored or not stored.startswith(_PREFIX):
        return stored
    try:
        return _fernet().decrypt(stored[len(_PREFIX):].encode('ascii')).decode('utf-8')
    except InvalidToken:
        logger.error(
            'Could not decrypt a stored token — TOKEN_ENCRYPTION_KEY (or SECRET_KEY) has changed since it '
            'was saved. The integration needs to be reconnected.',
        )
        return ''


class EncryptedTextField(models.TextField):
    """See module docstring. Not queryable by value (each encryption uses a
    fresh random IV, so equal plaintexts never produce equal ciphertexts) —
    nothing here filters on these columns."""

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if value is None or value == '':
            return value
        return encrypt_value(value)

    def from_db_value(self, value, expression, connection):
        if value is None:
            return value
        return decrypt_value(value)
