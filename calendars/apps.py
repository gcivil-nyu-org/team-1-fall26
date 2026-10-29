from django.apps import AppConfig
from django.conf import settings


class CalendarsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "calendars"

    def ready(self):
        # Outside development, refuse to start without valid encryption keys.
        if not settings.DEBUG:
            from .fields import get_fernet

            get_fernet()
