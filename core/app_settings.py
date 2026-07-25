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
]


def apply_overrides():
    from django.conf import settings

    from .models import AppSetting

    try:
        rows = list(AppSetting.objects.all())
    except Exception:
        # Table doesn't exist yet (e.g. mid-migration on first deploy) —
        # nothing to override, env-var defaults stand.
        return
    for row in rows:
        setattr(settings, row.key, row.value)


def set_secret(key, value, user=None):
    from django.conf import settings

    from .models import AppSetting

    AppSetting.objects.update_or_create(key=key, defaults={'value': value, 'updated_by': user})
    setattr(settings, key, value)


def masked(value):
    if not value:
        return ''
    return f'{"•" * max(len(value) - 4, 0)}{value[-4:]}'
