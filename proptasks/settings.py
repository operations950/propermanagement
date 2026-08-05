"""
Django settings for proptasks project.
"""

import os
import socket
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / '.env')

# Railway's containers have no outbound IPv6 route, but smtp.gmail.com (and
# many other mail hosts) resolve to an IPv6 address as well as an IPv4 one —
# whichever DNS/getaddrinfo returns first is what smtplib tries first, and an
# IPv6 attempt with no route out fails immediately with OSError: [Errno 101]
# Network is unreachable, before ever falling back to the IPv4 address that
# would have worked. Forcing every unspecified-family lookup in this process
# to IPv4 sidesteps that — safe process-wide since nothing here needs IPv6,
# and callers that explicitly ask for a specific family are left alone.
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if family == 0:
        family = socket.AF_INET
    return _orig_getaddrinfo(host, port, family, type, proto, flags)


socket.getaddrinfo = _ipv4_only_getaddrinfo


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
    # Not an installed app: cloudinary_storage's own collectstatic command
    # override breaks WhiteNoise-based static collection outright (its
    # copy_file is a no-op unless static files are *also* served from
    # Cloudinary, which they aren't here) — we only need the storage
    # backend CLASS referenced by dotted path in STORAGES below, which
    # doesn't require app registration. See git history for the incident.
    'cloudinary',

    'core',
    'tickets',
    'vendorportal',
    'messaging',
    'intake',
    'supplies',
    'processes',
    'onsite',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'core.middleware.TimezoneMiddleware',
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


# EmailOrUsernameModelBackend (core/auth_backends.py) lets the login form's
# single field take either an email or a legacy username — see its
# docstring. ModelBackend stays listed as a fallback (harmless, same lookup
# django.contrib.auth otherwise does by default) rather than removing it.
AUTHENTICATION_BACKENDS = [
    'core.auth_backends.EmailOrUsernameModelBackend',
    'django.contrib.auth.backends.ModelBackend',
]

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]


LANGUAGE_CODE = 'en-us'

# Was 'America/Chicago' — the business and every property are in South
# Florida, so anything rendered via timezone.localtime() (ticket due
# dates, calendar events, message timestamps — used throughout the app)
# was showing an hour early against Eastern wall-clock time. A staff
# member's own StaffProfile.timezone (see core.middleware.TimezoneMiddleware)
# overrides this default per-request once they set one.
TIME_ZONE = 'America/New_York'
USE_I18N = True
USE_TZ = True


STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'  # collectstatic's output — served by WhiteNoise in production

CLOUDINARY_CLOUD_NAME = os.environ.get('CLOUDINARY_CLOUD_NAME', '')
CLOUDINARY_API_KEY = os.environ.get('CLOUDINARY_API_KEY', '')
CLOUDINARY_API_SECRET = os.environ.get('CLOUDINARY_API_SECRET', '')
CLOUDINARY_STORAGE = {
    'CLOUD_NAME': CLOUDINARY_CLOUD_NAME,
    'API_KEY': CLOUDINARY_API_KEY,
    'API_SECRET': CLOUDINARY_API_SECRET,
}

# Railway's filesystem is ephemeral (wiped on every deploy/restart), so
# anything saved to local MEDIA_ROOT in production won't survive — this
# affects every FileField/ImageField app-wide (ticket attachments, contact/
# property documents, process attachments, vendor and on-site-visit
# photos), not just one feature. Cloudinary-backed storage is used whenever
# all three CLOUDINARY_* vars are set (see /admin-tools/ or .env.example);
# left blank, this falls back to plain local FileSystemStorage exactly as
# before, which is fine for local dev but loses uploads on every production
# deploy/restart until those vars are set.
if CLOUDINARY_CLOUD_NAME and CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET:
    STORAGES = {
        'default': {'BACKEND': 'cloudinary_storage.storage.MediaCloudinaryStorage'},
        'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage'},
    }
else:
    STORAGES = {
        'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
        'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage'},
    }

MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

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
# TLS (STARTTLS, port 587) is the default; set EMAIL_USE_SSL=true + EMAIL_PORT=465
# and EMAIL_USE_TLS=false as Railway env vars to try implicit-SSL instead — some
# hosts that block 587 outbound leave 465 open. Mutually exclusive per Django's
# own SMTP backend, which picks SMTP_SSL over SMTP purely based on this flag.
EMAIL_USE_TLS = env_bool('EMAIL_USE_TLS', True)
EMAIL_USE_SSL = env_bool('EMAIL_USE_SSL', False)
# Without this, a socket that can't reach the SMTP host (wrong port, or the
# host silently dropping the connection instead of refusing it — Railway and
# several other PaaS providers block outbound SMTP for anti-spam reasons)
# blocks forever. That leaves the request stuck until gunicorn's own worker
# timeout kills the entire process out from under it — which is what
# happened in production (WORKER TIMEOUT on POST /tickets/<id>/followup/
# email/) rather than send_mail's own try/except ever getting a chance to
# catch a normal, fast, loggable failure.
EMAIL_TIMEOUT = int(os.environ.get('EMAIL_TIMEOUT', '10'))
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
# Separate, wider window for the one-off/periodic import_gmail_contacts command — building a contact
# base benefits from more history than the live per-thread ticket pipeline needs.
GMAIL_CONTACT_IMPORT_DAYS = int(os.environ.get('GMAIL_CONTACT_IMPORT_DAYS', '90'))
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
# How often classify_quo_conversations re-judges conversations with new
# local activity (message capture itself is real-time via the Quo webhook —
# this is just how often the "does this need a ticket" AI pass re-runs, kept
# slower/decoupled on purpose so a conversation gets a chance to develop
# before being judged, and so Claude isn't re-run on every single message).
QUO_CLASSIFY_INTERVAL_MINUTES = int(os.environ.get('QUO_CLASSIFY_INTERVAL_MINUTES', '120'))
# How often sync_quo_contacts checks Quo's saved contact list for brand-new
# contacts and edits to already-approved ones — a person's own info changes
# far less often than conversation content, so this defaults to once a day
# rather than riding the classify interval.
QUO_CONTACT_SYNC_INTERVAL_MINUTES = int(os.environ.get('QUO_CONTACT_SYNC_INTERVAL_MINUTES', '1440'))
# The Quo line to send a contact's very first message from, when they have no
# existing thread yet (see messaging.services.send_via_quo) — an established
# thread still always sends from whichever line that contact already talks
# to, this is only the "we're initiating, not replying" fallback.
QUO_DEFAULT_FROM_NUMBER = os.environ.get('QUO_DEFAULT_FROM_NUMBER', '+15615996300')
# When set, QuoAdapter.pull() only scans conversations on this one phone line
# (Quo's own phoneNumberId) instead of every line the account owns — see
# core/views.py::admin_tools for the staff-facing picker. Blank means "scan
# every line," the original/default behavior.
QUO_SCAN_PHONE_NUMBER_ID = os.environ.get('QUO_SCAN_PHONE_NUMBER_ID', '')
# How often link_quo_contact_threads runs — a cheap global conversation crawl
# that fills in QuoThreadState rows purely by phone-number match, so a
# contact's text history is discoverable (contact/property "view
# conversation", ticket detail's Contractor Communication box) even if the
# regular Quo poller's own cursor never happened to cover that thread.
QUO_CONTACT_LINK_INTERVAL_MINUTES = int(os.environ.get('QUO_CONTACT_LINK_INTERVAL_MINUTES', '120'))
# How often resume_expired_wait_steps checks for WAIT_TIMER process steps
# whose configured duration has elapsed — a wait step isn't time-critical
# to the minute, so this runs on a coarser cadence than the intake polls.
PROCESS_WAIT_CHECK_INTERVAL_MINUTES = int(os.environ.get('PROCESS_WAIT_CHECK_INTERVAL_MINUTES', '60'))
GOOGLE_CALENDAR_CREDENTIALS_PATH = os.environ.get('GOOGLE_CALENDAR_CREDENTIALS_PATH', '')
AIRBNB_API_KEY = os.environ.get('AIRBNB_API_KEY', '')
VRBO_API_KEY = os.environ.get('VRBO_API_KEY', '')

# Per-staff Google Calendar OAuth (core/google_calendar.py, core/views.py).
# Blank = the "Connect Google Calendar" button safely no-ops with a message
# instead of erroring, same pattern as the other future-integration keys.
GOOGLE_OAUTH_CLIENT_ID = os.environ.get('GOOGLE_OAUTH_CLIENT_ID', '')
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get('GOOGLE_OAUTH_CLIENT_SECRET', '')

# oauthlib raises a Warning (caught as a token-exchange failure) if Google's
# token response echoes back scopes in a different order/form than requested
# (e.g. "email" -> "https://www.googleapis.com/auth/userinfo.email") — a
# known google-auth-oauthlib quirk, not an actual problem. Must be set
# before google_auth_oauthlib.flow.Flow is ever imported (core/google_calendar.py,
# intake/gmail_auth.py both do that lazily inside build_flow(), well after
# this module has already loaded), so this env var is the only fix that
# reliably lands before that first import.
os.environ.setdefault('OAUTHLIB_RELAX_TOKEN_SCOPE', '1')

# Property address picker (core/places.py, core/usps.py) — separate from
# GOOGLE_OAUTH_CLIENT_ID/SECRET above (a Places API key, not an OAuth
# client). Blank = the property form falls back to plain manual address
# entry with no live suggestions and no USPS verification badge.
GOOGLE_PLACES_API_KEY = os.environ.get('GOOGLE_PLACES_API_KEY', '')
USPS_CLIENT_ID = os.environ.get('USPS_CLIENT_ID', '')
USPS_CLIENT_SECRET = os.environ.get('USPS_CLIENT_SECRET', '')

# Company Financials on the Owner Dashboard (core/quickbooks.py, core/views.py).
# Blank = the box shows a "Connect QuickBooks" prompt instead of erroring,
# same future-integration pattern as the keys above.
QUICKBOOKS_CLIENT_ID = os.environ.get('QUICKBOOKS_CLIENT_ID', '')
QUICKBOOKS_CLIENT_SECRET = os.environ.get('QUICKBOOKS_CLIENT_SECRET', '')
QUICKBOOKS_SYNC_INTERVAL_MINUTES = int(os.environ.get('QUICKBOOKS_SYNC_INTERVAL_MINUTES', str(60 * 24)))

# Local Weather box fallback location (used when the browser denies/lacks
# geolocation) — the office at 1045 E Atlantic Ave, Delray Beach, FL 33483.
OFFICE_LATITUDE = float(os.environ.get('OFFICE_LATITUDE', '26.4618'))
OFFICE_LONGITUDE = float(os.environ.get('OFFICE_LONGITUDE', '-80.0617'))

# Used by intake/thread_classifier.py to read a full Quo conversation thread
# before deciding whether it's actionable. Blank = classification no-ops.
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

# On-site Visits module (onsite app) — VisitRule generation and retrying
# pending Google Calendar pushes.
ONSITE_GENERATE_VISITS_INTERVAL_MINUTES = int(os.environ.get('ONSITE_GENERATE_VISITS_INTERVAL_MINUTES', str(60 * 24)))
ONSITE_CALENDAR_SYNC_INTERVAL_MINUTES = int(os.environ.get('ONSITE_CALENDAR_SYNC_INTERVAL_MINUTES', '30'))

# Owner Dashboard's "Gone quiet" panel thresholds — see
# tickets/services/owner_dashboard.py::gone_quiet. Settings, not literals,
# per that panel's own design brief.
OWNER_DASHBOARD_QUIET_DAYS = int(os.environ.get('OWNER_DASHBOARD_QUIET_DAYS', '7'))
OWNER_DASHBOARD_BLOCKED_QUIET_DAYS = int(os.environ.get('OWNER_DASHBOARD_BLOCKED_QUIET_DAYS', '30'))

# Supply reorder cart-state thresholds — see supplies/services.py's cart
# state table (build brief: "Supply reorder redesign"). Settings, not
# literals, per that brief.
SUPPLY_READING_STALE_DAYS = int(os.environ.get('SUPPLY_READING_STALE_DAYS', '14'))
SUPPLY_DELIVERY_EXPECTED_DAYS = int(os.environ.get('SUPPLY_DELIVERY_EXPECTED_DAYS', '5'))

# One shared Google Calendar every scheduled on-site visit gets pushed to —
# uses whichever staff member's own connected GOOGLE_OAUTH_CLIENT_ID/SECRET
# calendar has been shared access to this calendar (see
# onsite/google_calendar_push.py). Blank = pushes are skipped, no error.
GOOGLE_ONSITE_CALENDAR_ID = os.environ.get('GOOGLE_ONSITE_CALENDAR_ID', '')

# Max upload size for vendor-submitted completion photos/videos (bytes) — video
# needs real headroom over a plain photo, hence the bump from the original 10MB.
VENDOR_UPLOAD_MAX_BYTES = 75 * 1024 * 1024
VENDOR_UPLOAD_ALLOWED_CONTENT_TYPES = [
    'image/jpeg', 'image/png', 'image/webp', 'image/heic',
    'video/mp4', 'video/quicktime', 'video/webm',
]

# Process instance attachments (proof-of-completion uploads like a signed
# affidavit, or a photo of a physically-posted notice) — broader than the
# vendor portal's image/video-only list since these can be scanned documents.
PROCESS_ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024
PROCESS_ATTACHMENT_ALLOWED_CONTENT_TYPES = [
    'image/jpeg', 'image/png', 'image/webp', 'image/heic',
    'application/pdf',
    'application/msword',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/vnd.ms-excel',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
]

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
