"""Database backstops for usernames and emails (spec 3.2 rule 4, 3.1, 8.5).

Unique indexes on lower(username) and on lower(email) where the email is not empty.
They are built CONCURRENTLY, so auth_user stays writable while they build; that
needs a migration outside a transaction (atomic = False).

The preflight stops the migration when two usernames differ only in letter case:
the owner renames one of them first (`manage.py report_username_policy` lists
them).
"""

from django.db import migrations
from django.db.models import Count
from django.db.models.functions import Lower


def preflight(apps, schema_editor):
    User = apps.get_model("auth", "User")
    clashes = list(
        User.objects.annotate(name=Lower("username"))
        .values("name")
        .annotate(holders=Count("pk"))
        .filter(holders__gt=1)
        .values_list("name", flat=True)
    )
    if clashes:
        raise RuntimeError(
            f"{len(clashes)} usernames differ only in letter case. Rename one of each"
            " pair (manage.py report_username_policy lists them), then migrate again."
        )


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ("accounts", "0011_one_account_per_email"),
    ]

    operations = [
        migrations.RunPython(preflight, migrations.RunPython.noop),
        migrations.RunSQL(
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "
            "accounts_user_username_lower_uniq ON auth_user (lower(username));",
            "DROP INDEX CONCURRENTLY IF EXISTS accounts_user_username_lower_uniq;",
        ),
        migrations.RunSQL(
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "
            "accounts_user_email_lower_uniq ON auth_user (lower(email)) "
            "WHERE email <> '';",
            "DROP INDEX CONCURRENTLY IF EXISTS accounts_user_email_lower_uniq;",
        ),
    ]
