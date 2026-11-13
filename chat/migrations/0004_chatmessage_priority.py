from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0003_remove_chatmessage_household_id_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="chatmessage",
            name="priority",
            field=models.CharField(
                choices=[("normal", "Normal"), ("high", "High")],
                default="normal",
                max_length=6,
            ),
        ),
    ]
