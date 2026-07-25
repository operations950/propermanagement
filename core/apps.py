from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = 'core'

    def ready(self):
        # DB-backed secret overrides (see core/app_settings.py) — applied
        # once at process start so every settings.X read anywhere in the
        # app picks up whatever's been set via /admin-tools/ without
        # needing the literal env var. Guarded internally for the case
        # this runs before the AppSetting table exists yet (e.g. a bare
        # `manage.py migrate` on a brand new database).
        from . import app_settings
        app_settings.apply_overrides()
