from django.apps import AppConfig


class OnsiteConfig(AppConfig):
    name = 'onsite'

    def ready(self):
        from . import signals  # noqa: F401 — registers the Visit calendar-sync receivers
