from django.apps import AppConfig


class SyncmilyConfig(AppConfig):
    """The project package as an app, so its template tags, models and management
    commands load like any other app's."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "syncmily"
    label = "syncmily"

    def ready(self):
        from . import logs

        logs.install()
