from django.core.management.base import BaseCommand, CommandError

from calendars.models import CalendarConnection
from calendars.sync import sync_connection


class Command(BaseCommand):
    help = (
        "Import Google Calendar events for every connection. Schedule it, e.g. every 15"
        " minutes."
    )

    def handle(self, *args, **options):
        failures = 0
        connections = CalendarConnection.objects.select_related("user", "workspace")
        for connection in connections:
            try:
                result = sync_connection(connection)
            except Exception as error:
                failures += 1
                self.stderr.write(
                    f"Connection {connection.pk}: failed ({type(error).__name__})"
                )
            else:
                self.stdout.write(f"Connection {connection.pk}: {result}")
        if failures:
            raise CommandError(f"{failures} connection(s) failed.")
