import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0005_workspace_palette"),
        ("calendars", "0002_encrypt_oauth_tokens"),
        ("events", "0011_event_is_tentative"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="event",
            name="dropoff_driver",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="dropoff_events",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="event",
            name="pickup_driver",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="pickup_events",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="event",
            name="requires_transport",
            field=models.BooleanField(db_default=False, default=False),
        ),
        migrations.AddConstraint(
            model_name="event",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("dropoff_driver__isnull", True),
                        ("pickup_driver__isnull", True),
                    ),
                    models.Q(
                        ("requires_transport", True), ("workspace__isnull", False)
                    ),
                    _connector="OR",
                ),
                name="event_drivers_require_family_transport",
            ),
        ),
    ]
