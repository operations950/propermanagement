"""DB-backed overrides for API keys/secrets — lets an admin update them
from /admin-tools/ instead of editing settings.py or a Railway env var
directly. Scoped to a fixed, curated list (see SECRET_KEYS) rather than a
generic settings editor.

How it works: apply_overrides() copies every AppSetting row onto
django.conf.settings as a plain attribute, so every existing
settings.ANTHROPIC_API_KEY-style read anywhere in the app keeps working
completely unchanged — it just sees the DB value once one exists. Called
once at boot (AppConfig.ready()) and again immediately after every save
(set_secret) so the change takes effect without a restart. This relies on
the app running as a single process (see Procfile — gunicorn with no
--workers flag defaults to one), since each process would otherwise only
see the override it personally received.
"""
import logging

logger = logging.getLogger(__name__)

SECRET_KEYS = [
    ('ANTHROPIC_API_KEY', 'Anthropic (Claude) API key'),
    ('QUO_API_KEY', 'Quo API key'),
    ('QUO_WEBHOOK_TOKEN', 'Quo webhook URL token'),
    ('QUO_WEBHOOK_SIGNING_KEY', 'Quo webhook signing secret'),
    ('GOOGLE_PLACES_API_KEY', 'Google Places API key'),
    ('GOOGLE_OAUTH_CLIENT_ID', 'Google OAuth client ID'),
    ('GOOGLE_OAUTH_CLIENT_SECRET', 'Google OAuth client secret'),
    ('USPS_CLIENT_ID', 'USPS client ID'),
    ('USPS_CLIENT_SECRET', 'USPS client secret'),
    ('EMAIL_HOST', 'Email SMTP host (e.g. smtp.gmail.com)'),
    ('EMAIL_HOST_USER', 'Email SMTP username'),
    ('EMAIL_HOST_PASSWORD', 'Email SMTP password'),
    ('DEFAULT_FROM_EMAIL', 'Email "from" address'),
]


def _sanitize_secret(key, value):
    """Strips whitespace and drops non-ASCII characters — every one of
    these SECRET_KEYS values is a plain-ASCII API key/secret that ends up
    straight in an HTTP header (Authorization/x-api-key/etc.), and even one
    non-ASCII character anywhere in it crashes every call using it with a
    UnicodeEncodeError deep in the HTTP client, not a clean auth failure.
    Applied here (read time, every apply_overrides() call — i.e. every
    process boot) rather than only when a value is saved, so a value that
    got corrupted before this sanitization existed self-heals on the next
    deploy instead of silently persisting until someone happens to re-save
    it through the settings form."""
    if not value:
        return value
    cleaned = value.strip()
    ascii_only = cleaned.encode('ascii', 'ignore').decode('ascii')
    if ascii_only != cleaned:
        removed = len(cleaned) - len(ascii_only)
        bad_positions = [i for i, c in enumerate(cleaned) if ord(c) > 127]
        logger.warning(
            'AppSetting %r had %d non-ASCII character(s) stripped (original length %d, positions %s) — '
            'this was very likely the cause of API calls using it failing with a UnicodeEncodeError.',
            key, removed, len(cleaned), bad_positions[:20],
        )
    return ascii_only


def sanitized_setting(key):
    """Reads settings.<key> and sanitizes it on the spot — call this at the
    actual point of use (building an API client, etc.) instead of trusting
    settings.<key> is already clean. apply_overrides()/set_secret() only
    sanitize a value that came through the DB-backed AppSetting override;
    a key set directly as a Railway environment variable (settings.py's
    `os.environ.get(...)` default) never passes through either of those and
    could carry the exact same kind of corruption completely unnoticed."""
    from django.conf import settings

    return _sanitize_secret(key, getattr(settings, key, '') or '')


def _sync_email_backend():
    """settings.EMAIL_BACKEND is chosen once, in settings.py, from the raw
    EMAIL_HOST env var — before Django even loads AppConfig.ready(), let
    alone a DB-backed override. Setting EMAIL_HOST here (via apply_overrides
    or an admin save) would otherwise silently keep every send going through
    the console/no-op backend even once real SMTP credentials exist. Called
    after every place EMAIL_HOST might change."""
    from django.conf import settings

    settings.EMAIL_BACKEND = (
        'django.core.mail.backends.smtp.EmailBackend' if settings.EMAIL_HOST
        else 'django.core.mail.backends.console.EmailBackend'
    )


def apply_overrides():
    from django.conf import settings

    from .models import AppSetting

    try:
        rows = list(AppSetting.objects.all())
    except Exception:
        # Table doesn't exist yet (e.g. mid-migration on first deploy) —
        # nothing to override, env-var defaults stand.
        return
    secret_keys = dict(SECRET_KEYS)
    for row in rows:
        value = _sanitize_secret(row.key, row.value) if row.key in secret_keys else row.value
        setattr(settings, row.key, value)
    _sync_email_backend()


def set_secret(key, value, user=None):
    from django.conf import settings

    from .models import AppSetting

    value = _sanitize_secret(key, value) if key in dict(SECRET_KEYS) else value
    AppSetting.objects.update_or_create(key=key, defaults={'value': value, 'updated_by': user})
    setattr(settings, key, value)
    if key == 'EMAIL_HOST':
        _sync_email_backend()


def masked(value):
    if not value:
        return ''
    return f'{"•" * max(len(value) - 4, 0)}{value[-4:]}'
