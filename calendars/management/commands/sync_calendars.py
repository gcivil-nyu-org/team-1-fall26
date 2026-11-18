from django.core.management.base import BaseCommand, CommandError

from calendars.models import CalendarConnection
from calendars.sync import record_sync_failure, sync_connection


class Command(BaseCommand):
    help = (
        "Import Google Calendar events for every connection with automatic sync on."
        " Schedule it, e.g. every 15 minutes."
    )

    def handle(self, *args, **options):
        failures = 0
        connections = CalendarConnection.objects.filter(auto_sync=True).select_related(
            "user", "workspace"
        )
        paused = CalendarConnection.objects.filter(auto_sync=False).count()
        if paused:
            self.stdout.write(f"Skipped {paused} paused connection(s).")
        for connection in connections:
            try:
                result = sync_connection(connection)
            except Exception as error:
                failures += 1
                record_sync_failure(connection, error)
                self.stderr.write(
                    f"Connection {connection.pk}: failed ({type(error).__name__})"
                )
            else:
                self.stdout.write(f"Connection {connection.pk}: {result}")
        if failures:
            raise CommandError(f"{failures} connection(s) failed.")
