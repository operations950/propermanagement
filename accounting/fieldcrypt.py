"""Encryption for the few secrets the books keep in the database (today: a bank account's full account number, which a check needs).

The value is stored as a Fernet token (AES-128-CBC with an HMAC), never as the digits. The key comes from the FIELD_ENCRYPTION_KEY setting (an environment variable): either a Fernet key
(`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`) or any long random phrase, which is stretched into one. When that is not set, the key is
derived from the site's SECRET_KEY, so nothing has to be set up; but then the numbers can only be read as long as SECRET_KEY stays the same. Set FIELD_ENCRYPTION_KEY once in production
and never change it: a token made with another key cannot be read (`decrypt` then returns None and the screen asks for the number to be typed again).
"""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _key():
    raw = (getattr(settings, 'FIELD_ENCRYPTION_KEY', '') or '').strip()
    if raw:
        try:
            Fernet(raw.encode())
            return raw.encode()
        except (ValueError, TypeError):
            return base64.urlsafe_b64encode(hashlib.sha256(('field-encryption:' + raw).encode()).digest())
    return base64.urlsafe_b64encode(hashlib.sha256(('field-encryption-from-secret-key:' + settings.SECRET_KEY).encode()).digest())


def encrypt(text):
    """The text as a token to store. Empty text stays empty."""
    text = (text or '').strip()
    return Fernet(_key()).encrypt(text.encode()).decode() if text else ''


def decrypt(token):
    """The text a token holds; '' for an empty token; None when it cannot be read (a different key, or a damaged value)."""
    token = (token or '').strip()
    if not token:
        return ''
    try:
        return Fernet(_key()).decrypt(token.encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        return None
