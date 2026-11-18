"""Issue #11: the family hears about a failed import, even one nobody was watching."""

from datetime import datetime, timedelta, timezone
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from google.auth.exceptions import RefreshError

from accounts.models import Workspace

from .. import google
from ..models import CalendarConnection
from ..sync import record_sync_failure, sync_connection
from .test_core import TEST_KEY, FakeCalendarService

REVOKED = CalendarConnection.SyncError.ACCESS_REVOKED
FAILED = CalendarConnection.SyncError.FAILED
COMMAND_SYNC = "calendars.management.commands.sync_calendars.sync_connection"
STOPPED = "Google Calendar stopped syncing"
PANEL_REVOKED = "because Google no longer accepts Syncmily’s access"
PANEL_FAILED = "The last import failed"


@override_settings(FIELD_ENCRYPTION_KEYS=[TEST_KEY])
class SyncFailureTests(TestCase):
    now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="caregiver")
        self.workspace = Workspace.objects.create(
            name="Our family", primary_admin=self.user
        )
        self.connection = CalendarConnection.objects.create(
            user=self.user,
            workspace=self.workspace,
            provider=CalendarConnection.Provider.GOOGLE,
            access_token="ya29.access",
            refresh_token="1//refresh",
            token_expires_at=self.now + timedelta(hours=1),
            scopes=" ".join(google.SCOPES),
        )
        self.family_url = reverse("accounts:workspace_detail", args=[self.workspace.pk])
        self.week_url = reverse("events:workspace_list", args=[self.workspace.pk])

    def run_command(self, error):
        with patch(COMMAND_SYNC, side_effect=error), self.assertRaises(CommandError):
            call_command("sync_calendars", stdout=StringIO(), stderr=StringIO())
        self.connection.refresh_from_db()

    def test_scheduled_sync_records_revoked_access(self):
        self.run_command(RefreshError("invalid_grant"))

        self.assertEqual(self.connection.sync_error, REVOKED)
        self.assertIsNotNone(self.connection.sync_failed_at)

    def test_scheduled_sync_records_other_failures(self):
        self.run_command(ValueError("Google is down"))

        self.assertEqual(self.connection.sync_error, FAILED)

    def test_paused_connections_are_not_marked(self):
        self.connection.auto_sync = False
        self.connection.save()
        with patch(COMMAND_SYNC, side_effect=ValueError("boom")):
            call_command("sync_calendars", stdout=StringIO(), stderr=StringIO())
        self.connection.refresh_from_db()

        self.assertEqual(self.connection.sync_error, "")

    def test_import_now_button_records_the_failure(self):
        self.client.force_login(self.user)
        with patch("calendars.views.sync_connection", side_effect=RefreshError("x")):
            self.client.post(reverse("calendars:google_sync"))
        self.connection.refresh_from_db()

        self.assertEqual(self.connection.sync_error, REVOKED)

    def test_a_successful_import_clears_the_problem(self):
        record_sync_failure(self.connection, RefreshError("x"), now=self.now)
        with patch("calendars.sync._service", return_value=FakeCalendarService([[]])):
            sync_connection(self.connection, now=self.now + timedelta(minutes=15))
        self.connection.refresh_from_db()

        self.assertEqual(self.connection.sync_error, "")
        self.assertIsNone(self.connection.sync_failed_at)

    def test_recording_keeps_tokens_saved_meanwhile(self):
        # Another process refreshed the token after this copy was loaded.
        CalendarConnection.objects.filter(pk=self.connection.pk).update(
            access_token="ya29.newer"
        )
        record_sync_failure(self.connection, ValueError("boom"))
        self.connection.refresh_from_db()

        self.assertEqual(self.connection.access_token, "ya29.newer")
        self.assertEqual(self.connection.sync_error, FAILED)

    def test_revoked_access_is_announced_on_every_page(self):
        record_sync_failure(self.connection, RefreshError("x"))
        self.client.force_login(self.user)

        response = self.client.get(self.week_url)
        self.assertContains(response, STOPPED)
        self.assertContains(response, f'href="{self.family_url}"')

        # The family page says it once, next to the Reconnect controls.
        response = self.client.get(self.family_url)
        self.assertNotContains(response, STOPPED)
        self.assertContains(response, PANEL_REVOKED, count=1)

    def test_a_passing_failure_shows_only_on_the_family_page(self):
        record_sync_failure(self.connection, ValueError("boom"))
        self.client.force_login(self.user)

        self.assertNotContains(self.client.get(self.week_url), STOPPED)
        response = self.client.get(self.family_url)
        self.assertContains(response, PANEL_FAILED)
        self.assertContains(response, "Syncmily tries again every 15 minutes")

    def test_no_notice_when_syncing_works(self):
        self.client.force_login(self.user)

        self.assertNotContains(self.client.get(self.week_url), STOPPED)
        self.assertNotContains(self.client.get(self.family_url), PANEL_FAILED)

    def test_only_the_calendar_owner_is_told(self):
        record_sync_failure(self.connection, RefreshError("x"))
        other = get_user_model().objects.create_user(username="other-parent")
        self.client.force_login(other)

        response = self.client.get(reverse("events:choose_workspace"))
        self.assertNotContains(response, STOPPED)
