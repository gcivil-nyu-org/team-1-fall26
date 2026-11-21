from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import connection, models

APP_LABELS = ("accounts", "calendars", "chat", "checklists", "events")


def failing_rows():
    """Yield (model label, constraint name, rows that fail it) for every CHECK
    constraint the models declare. NOT (check) is NULL where the check passes on a
    NULL, so the count matches what Postgres would refuse."""
    schema_editor = connection.schema_editor()
    for model in apps.get_models():
        if model._meta.app_label not in APP_LABELS:
            continue
        for constraint in model._meta.constraints:
            if not isinstance(constraint, models.CheckConstraint):
                continue
            check = constraint._get_check_sql(model, schema_editor)
            table = schema_editor.quote_name(model._meta.db_table)
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT count(*) FROM {table} WHERE NOT ({check})")
                yield model._meta.label, constraint.name, cursor.fetchone()[0]


class Command(BaseCommand):
    help = (
        "Count the rows each database CHECK constraint would refuse (spec 8.5, 9.4)."
        " Run it before migrating, so the owner sees what the cleaning migrations"
        " will change. It reads only."
    )

    def handle(self, *args, **options):
        total = 0
        for label, name, count in failing_rows():
            total += count
            self.stdout.write(f"{label} {name}: {count}")
        self.stdout.write(f"Rows that would fail: {total}")
