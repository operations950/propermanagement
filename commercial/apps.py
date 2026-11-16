from django.apps import AppConfig


class CommercialConfig(AppConfig):
    name = 'commercial'

    def ready(self):
        from . import signals  # noqa: F401
