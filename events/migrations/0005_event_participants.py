from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("events", "0004_event_timezone_name"),
        ("accounts", "0002_membership"),
    ]

    operations = [
        migrations.AddField(
            model_name="event",
            name="participants",
            field=models.ManyToManyField(
                to=settings.AUTH_USER_MODEL,
                related_name="family_events",
                blank=True,
            ),
        ),
    ]
