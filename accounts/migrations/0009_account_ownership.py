import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0008_workspace_home"),
        ("auth", "0012_alter_user_first_name_max_length"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="EmailAddress",
            fields=[
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        primary_key=True,
                        related_name="email_address",
                        serialize=False,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                ("email", models.EmailField(blank=True, default="", max_length=254)),
                ("verified_at", models.DateTimeField(blank=True, null=True)),
                ("sent_at", models.DateTimeField(blank=True, null=True)),
                ("released_at", models.DateTimeField(blank=True, null=True)),
            ],
        ),
        migrations.AddField(
            model_name="googleidentity",
            name="came_with_account",
            field=models.BooleanField(db_default=False, default=False),
        ),
        migrations.AddField(
            model_name="googleidentity",
            name="revoked_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="membership",
            name="invite_nonce",
            field=models.CharField(
                blank=True, db_default="", default="", editable=False, max_length=32
            ),
        ),
        migrations.CreateModel(
            name="UsernameHistory",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("old", models.CharField(max_length=150)),
                ("new", models.CharField(max_length=150)),
                ("changed_at", models.DateTimeField(db_index=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="username_history",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-changed_at"],
            },
        ),
    ]
