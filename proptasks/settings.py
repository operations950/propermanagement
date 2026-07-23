"""
Django settings for proptasks project.
"""

import os
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / '.env')


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ('1', 'true', 'yes', 'on')


SECRET_KEY = os.environ.get('SECRET_KEY', 'django-insecure-dev-key-change-me')

DEBUG = env_bool('DEBUG', True)

ALLOWED_HOSTS = [h.strip() for h in os.environ.get('ALLOWED_HOSTS', '127.0.0.1,localhost').split(',') if h.strip()]

# Railway (and most PaaS hosts) sit behind a reverse proxy that terminates
# HTTPS and forwards plain HTTP internally — without this, Django thinks
# every request is insecure and CSRF/redirect logic misbehaves.
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# Django's CSRF check requires the exact scheme+host of any origin that can
# POST here. Derived from ALLOWED_HOSTS so there's one setting to update per
# environment rather than two — localhost/127.0.0.1 are excluded since https
# doesn't apply to local dev.
CSRF_TRUSTED_ORIGINS = [f'https://{h}' for h in ALLOWED_HOSTS if h not in ('127.0.0.1', 'localhost')]

if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True


INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    'core',
    'tickets',
    'vendorportal',
    'messaging',
    'intake',
    'supplies',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'proptasks.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'proptasks.wsgi.application'


# Local dev: SQLite (see the timeout note below). Production (Railway):
# set DATABASE_URL (Railway's Postgres plugin does this automatically) and
# this switches to Postgres — a real fix for the concurrent-write problem
# the SQLite timeout below only papers over, and required anyway since
# Railway's filesystem isn't reliably persistent across deploys.
if os.environ.get('DATABASE_URL'):
    DATABASES = {'default': dj_database_url.config(conn_max_age=600)}
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
            # SQLite's default is to raise "database is locked" immediately on
            # any write contention. With the background scheduler, manual CLI
            # runs, and the dev server all potentially writing at once, that
            # happens often enough to matter. This makes SQLite wait up to 20s
            # for a lock to clear before raising, which is enough for our write
            # volume — proper fix if this app ever needs real concurrent write
            # throughput is Postgres, not a longer timeout.
            'OPTIONS': {'timeout': 20},
        }
    }


AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]


LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'America/Chicago'
USE_I18N = True
USE_TZ = True


STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'  # collectstatic's output — served by WhiteNoise in production

STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage'},
}

MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'
# NOTE for production: Railway's filesystem is ephemeral (wiped on every
# deploy/restart), so vendor-uploaded photos in MEDIA_ROOT won't survive
# unless a persistent Volume is mounted there in the Railway dashboard, or
# this is switched to S3-compatible storage. Not set up yet — see the
# deployment notes.

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
    }
}

LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'dashboard'
LOGOUT_REDIRECT_URL = 'login'


# --- Email (follow-up messages) ---
# Console backend whenever EMAIL_HOST isn't set — including in production —
# so an unconfigured SMTP setup fails safely (logged, visible in FollowUpLog)
# instead of the "Report Resolution" button erroring on a live site because
# EMAIL_HOST_USER/PASSWORD were never filled in. Real sends need EMAIL_HOST
# set explicitly, independent of DEBUG.
if os.environ.get('EMAIL_HOST'):
    EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
else:
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

EMAIL_HOST = os.environ.get('EMAIL_HOST', '')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', '587'))
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
EMAIL_USE_TLS = env_bool('EMAIL_USE_TLS', True)
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', 'noreply@example.com')

# --- SMS (follow-up messages) ---
# 'log' just writes to the FollowUpLog + logger, no real send, until a real
# provider (e.g. Twilio) is configured here.
SMS_PROVIDER = os.environ.get('SMS_PROVIDER', 'log')

# --- Vendor completion link ---
VENDOR_TOKEN_EXPIRY_DAYS = int(os.environ.get('VENDOR_TOKEN_EXPIRY_DAYS', '30'))

# --- Background scheduler (APScheduler, in-process) ---
RUN_SCHEDULER = env_bool('RUN_SCHEDULER', True)
RECURRING_TICKET_INTERVAL_MINUTES = int(os.environ.get('RECURRING_TICKET_INTERVAL_MINUTES', '30'))
FAKE_POLL_INTERVAL_MINUTES = int(os.environ.get('FAKE_POLL_INTERVAL_MINUTES', '5'))
SUPPLY_DIGEST_INTERVAL_MINUTES = int(os.environ.get('SUPPLY_DIGEST_INTERVAL_MINUTES', '1440'))
# The fake/demo adapter (intake/adapters/fake.py) simulates events against
# made-up properties ("Sunset Villa", etc). Now that real property data
# exists, it's off by default — flip on only for demo/dev purposes.
RUN_FAKE_ADAPTER = env_bool('RUN_FAKE_ADAPTER', False)

# --- Future integrations (not wired live yet; read here so adapters/config
# have one place to look once credentials exist) ---
# Gmail auth is OAuth-based (intake/gmail_auth.py, GmailInboxToken), not a
# static credentials file — see GOOGLE_OAUTH_CLIENT_ID/SECRET below.
GMAIL_INITIAL_SYNC_DAYS = int(os.environ.get('GMAIL_INITIAL_SYNC_DAYS', '14'))
GMAIL_POLL_INTERVAL_MINUTES = int(os.environ.get('GMAIL_POLL_INTERVAL_MINUTES', '10'))
QUO_API_KEY = os.environ.get('QUO_API_KEY', '')
# On the very first sync (no cursor yet), only look back this many days
# instead of pulling the entire account history — a business with years of
# call/text history would otherwise re-fetch and re-classify everything on
# day one. Later polls are incremental from the last successful run.
QUO_INITIAL_SYNC_DAYS = int(os.environ.get('QUO_INITIAL_SYNC_DAYS', '7'))
# How often poll_quo runs, independent of the fake/demo adapter's interval —
# Quo is a live customer-facing SMS line, so it deserves its own cadence
# rather than piggybacking on FAKE_POLL_INTERVAL_MINUTES.
QUO_POLL_INTERVAL_MINUTES = int(os.environ.get('QUO_POLL_INTERVAL_MINUTES', '5'))
GOOGLE_CALENDAR_CREDENTIALS_PATH = os.environ.get('GOOGLE_CALENDAR_CREDENTIALS_PATH', '')
AIRBNB_API_KEY = os.environ.get('AIRBNB_API_KEY', '')
VRBO_API_KEY = os.environ.get('VRBO_API_KEY', '')

# Per-staff Google Calendar OAuth (core/google_calendar.py, core/views.py).
# Blank = the "Connect Google Calendar" button safely no-ops with a message
# instead of erroring, same pattern as the other future-integration keys.
GOOGLE_OAUTH_CLIENT_ID = os.environ.get('GOOGLE_OAUTH_CLIENT_ID', '')
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get('GOOGLE_OAUTH_CLIENT_SECRET', '')

# Used by intake/thread_classifier.py to read a full Quo conversation thread
# before deciding whether it's actionable. Blank = classification no-ops.
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

# Max upload size for vendor-submitted completion photos (bytes)
VENDOR_UPLOAD_MAX_BYTES = 10 * 1024 * 1024
VENDOR_UPLOAD_ALLOWED_CONTENT_TYPES = ['image/jpeg', 'image/png', 'image/webp', 'image/heic']

# Surface our own app's logger.info() calls on the console — without this,
# a long-running sync (e.g. poll_quo's first full historical backfill) is a
# silent black box until it finishes, since Django's default logging config
# only shows WARNING+ on the root logger.
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {'class': 'logging.StreamHandler'},
    },
    'loggers': {
        'intake': {'handlers': ['console'], 'level': 'INFO', 'propagate': False},
        'messaging': {'handlers': ['console'], 'level': 'INFO', 'propagate': False},
        'proptasks': {'handlers': ['console'], 'level': 'INFO', 'propagate': False},
    },
}
