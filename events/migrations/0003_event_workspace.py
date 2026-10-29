import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
        ("events", "0002_event_submission_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="event",
            name="workspace",
            field=models.ForeignKey(
                to="accounts.workspace",
                on_delete=django.db.models.deletion.PROTECT,
                related_name="events",
                null=True,
                blank=True,
            ),
        ),
    ]
