"""Fill ChatMessage.sender_name from each sender's current username (H-LOGIC-16).

Old messages get today's name, the best snapshot left. Reversing does nothing:
reversing 0005 drops the column.
"""

from django.db import migrations
from django.db.models import OuterRef, Subquery
from django.db.models.functions import Left


def backfill(apps, schema_editor):
    ChatMessage = apps.get_model("chat", "ChatMessage")
    User = ChatMessage._meta.get_field("sender").related_model
    username = User.objects.filter(pk=OuterRef("sender_id")).values("username")[:1]
    ChatMessage.objects.filter(sender_name="").update(
        sender_name=Left(Subquery(username), 30)
    )


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0005_chatmessage_sender_name"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
