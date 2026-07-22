from django.apps import AppConfig


class IntakeConfig(AppConfig):
    name = 'intake'

    # Scheduler startup lives in proptasks/wsgi.py, not here — ready() fires
    # for every manage.py command (migrate, collectstatic, ...), not just
    # the running server. See wsgi.py's comment for the full rationale.
