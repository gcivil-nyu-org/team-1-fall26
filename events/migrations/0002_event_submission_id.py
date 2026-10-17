from uuid import uuid4

from django.db import migrations, models


def populate_submission_ids(apps, schema_editor):
    Event = apps.get_model("events", "Event")
    events = Event.objects.using(schema_editor.connection.alias)
    for event in events.filter(submission_id__isnull=True).iterator():
        events.filter(pk=event.pk).update(submission_id=uuid4())


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="event",
            name="submission_id",
            field=models.UUIDField(null=True, editable=False),
        ),
        migrations.RunPython(populate_submission_ids, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="event",
            name="submission_id",
            field=models.UUIDField(default=uuid4, unique=True, editable=False),
        ),
    ]
