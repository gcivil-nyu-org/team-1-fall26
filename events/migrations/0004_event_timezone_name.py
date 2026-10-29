from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0003_event_workspace"),
    ]

    operations = [
        migrations.AddField(
            model_name="event",
            name="timezone_name",
            field=models.CharField(default="UTC", max_length=64),
        ),
    ]
