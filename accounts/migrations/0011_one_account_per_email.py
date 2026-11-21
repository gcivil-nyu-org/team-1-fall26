"""One account per email address, before the unique index (spec 8.5, decision 9.3).

When several accounts share an address (in any letter case), the one that signed in
most recently keeps it; the others get an empty email and are listed in the
migration output for the owner. Run `manage.py migrate --plan` and read the list
before deploying.

The reverse does nothing: the blanked addresses are printed by the forward step, and
putting duplicates back would only break the unique index again.
"""

from django.db import migrations
from django.db.models import Count, F
from django.db.models.functions import Lower


def keep_one_holder(apps, schema_editor):
    User = apps.get_model("auth", "User")
    shared = (
        User.objects.exclude(email="")
        .annotate(address=Lower("email"))
        .values("address")
        .annotate(holders=Count("pk"))
        .filter(holders__gt=1)
        .values_list("address", flat=True)
    )
    for address in list(shared):
        holders = list(
            User.objects.annotate(address=Lower("email"))
            .filter(address=address)
            .order_by(
                F("last_login").desc(nulls_last=True),
                F("date_joined").desc(),
                "-pk",
            )
        )
        for user in holders[1:]:
            print(
                f"\n  accounts: user {user.pk} ({user.username}) lost a duplicate"
                f" email; user {holders[0].pk} keeps it."
            )
            User.objects.filter(pk=user.pk).update(email="")
            apps.get_model("accounts", "EmailAddress").objects.filter(
                user_id=user.pk
            ).update(email="")


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0010_backfill_account_ownership"),
    ]

    operations = [migrations.RunPython(keep_one_holder, migrations.RunPython.noop)]
