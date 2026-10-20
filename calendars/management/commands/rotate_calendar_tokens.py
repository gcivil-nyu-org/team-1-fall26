from django.core.management.base import BaseCommand
from django.db import transaction

from calendars.models import CalendarConnection


class Command(BaseCommand):
    help = (
        "Re-encrypt stored calendar tokens with the first key in FIELD_ENCRYPTION_KEYS. "
        "Run it after adding a new key to the front of the list, then remove the old key."
    )

    def handle(self, *args, **options):
        count = 0
        with transaction.atomic():
            for connection in CalendarConnection.objects.select_for_update().iterator():
                # Loading decrypts with any key; saving encrypts with the newest one.
                connection.save(update_fields=["access_token", "refresh_token"])
                count += 1
        self.stdout.write(self.style.SUCCESS(f"Re-encrypted tokens for {count} calendar connection(s)."))
