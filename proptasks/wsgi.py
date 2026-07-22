"""
WSGI config for proptasks project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.0/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'proptasks.settings')

application = get_wsgi_application()

# Started here — not in an AppConfig.ready() hook — because ready() fires
# for EVERY manage.py invocation (migrate, collectstatic, createsuperuser,
# poll_quo, ...), which would otherwise spin up the full background
# scheduler as a side effect of one-off commands in production (DEBUG=False
# skips the dev-only autoreload guard that happens to mask this in local
# runserver use). This module only loads for the actual running server
# (runserver or gunicorn), which is exactly the one process that should
# run it.
from django.conf import settings  # noqa: E402

if settings.RUN_SCHEDULER:
    from proptasks import scheduler

    scheduler.start()
