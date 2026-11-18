from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from accounts.models import Workspace

from ..models import CalendarConnection
from ..sync import SyncResult


class AutoSyncToggleTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="caregiver")
        self.workspace = Workspace.objects.create(
            name="Our family", primary_admin=self.user
        )
        self.workspace_url = reverse(
            "accounts:workspace_detail", args=[self.workspace.pk]
        )
        self.connection = CalendarConnection.objects.create(
            user=self.user,
            workspace=self.workspace,
            provider=CalendarConnection.Provider.GOOGLE,
            account_email="caregiver@example.com",
            refresh_token="1//refresh",
        )
        self.client.force_login(self.user)

    def toggle(self, value):
        return self.client.post(
            reverse("calendars:google_auto_sync"), {"auto_sync": value}
        )

    def messages_for(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]

    def test_new_connections_sync_automatically(self):
        self.assertTrue(self.connection.auto_sync)

    def test_caregiver_can_pause_and_resume(self):
        response = self.toggle("off")
        self.assertRedirects(response, self.workspace_url)
        self.connection.refresh_from_db()
        self.assertFalse(self.connection.auto_sync)
        self.assertIn("paused", self.messages_for(response)[0])

        response = self.toggle("on")
        self.connection.refresh_from_db()
        self.assertTrue(self.connection.auto_sync)
        self.assertIn("is on", self.messages_for(response)[0])

    def test_setting_is_shown_on_the_family_page(self):
        response = self.client.get(self.workspace_url)
        self.assertContains(response, 'role="switch" aria-checked="true"')
        self.assertContains(response, "<b>On.</b>")

        self.toggle("off")
        response = self.client.get(self.workspace_url)
        self.assertContains(response, 'role="switch" aria-checked="false"')
        self.assertContains(response, "<b>Paused.</b>")
        self.assertContains(response, "Import events now")

    def test_bad_value_changes_nothing(self):
        response = self.toggle("maybe")
        self.connection.refresh_from_db()
        self.assertTrue(self.connection.auto_sync)
        self.assertIn("Choose", self.messages_for(response)[0])

    def test_requires_post_and_a_connection(self):
        response = self.client.get(reverse("calendars:google_auto_sync"))
        self.assertEqual(response.status_code, 405)

        other = get_user_model().objects.create_user(username="neighbour")
        self.client.force_login(other)
        self.assertEqual(self.toggle("off").status_code, 404)
        self.connection.refresh_from_db()
        self.assertTrue(self.connection.auto_sync)

    def test_scheduled_import_skips_paused_calendars(self):
        other = get_user_model().objects.create_user(username="parent2")
        active = CalendarConnection.objects.create(
            user=other,
            workspace=self.workspace,
            provider=CalendarConnection.Provider.GOOGLE,
        )
        self.connection.auto_sync = False
        self.connection.save()

        out = StringIO()
        with patch(
            "calendars.management.commands.sync_calendars.sync_connection",
            return_value=SyncResult(),
        ) as sync:
            call_command("sync_calendars", stdout=out)
        synced = [call.args[0].pk for call in sync.call_args_list]
        self.assertEqual(synced, [active.pk])
        self.assertIn("Skipped 1 paused", out.getvalue())

    def test_manual_import_still_works_while_paused(self):
        self.toggle("off")
        with patch(
            "calendars.views.sync_connection", return_value=SyncResult()
        ) as sync:
            response = self.client.post(reverse("calendars:google_sync"))
        self.assertRedirects(response, self.workspace_url)
        sync.assert_called_once()
