"""Existing accounts keep working under the new email-ownership rules (spec 10.1).

* Every account that already has an email counts as verified from the day it
  joined, so no current family loses invites.
* A Google identity created within a minute of its account made that account
  (a Google sign-up). Password changes keep those links and drop links added
  later from the profile page.

Both steps only fill in what is missing, so running them twice changes nothing.
The reverse removes exactly what the forward step added.
"""

from datetime import timedelta

from django.db import migrations
from django.db.models import F

SIGN_UP_WINDOW = timedelta(minutes=1)


def backfill(apps, schema_editor):
    User = apps.get_model("auth", "User")
    EmailAddress = apps.get_model("accounts", "EmailAddress")
    GoogleIdentity = apps.get_model("accounts", "GoogleIdentity")

    missing = (
        User.objects.exclude(email="")
        .filter(email_address__isnull=True)
        .values_list("pk", "email", "date_joined")
    )
    EmailAddress.objects.bulk_create(
        [
            EmailAddress(user_id=pk, email=email, verified_at=joined)
            for pk, email, joined in missing.iterator()
        ],
        batch_size=500,
        ignore_conflicts=True,
    )
    GoogleIdentity.objects.filter(
        came_with_account=False,
        created_at__lt=F("user__date_joined") + SIGN_UP_WINDOW,
    ).update(came_with_account=True)


def undo(apps, schema_editor):
    EmailAddress = apps.get_model("accounts", "EmailAddress")
    GoogleIdentity = apps.get_model("accounts", "GoogleIdentity")
    EmailAddress.objects.filter(
        sent_at__isnull=True, verified_at=F("user__date_joined")
    ).delete()
    GoogleIdentity.objects.update(came_with_account=False)


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0009_account_ownership"),
    ]

    operations = [migrations.RunPython(backfill, undo)]
