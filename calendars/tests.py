from datetime import datetime, timedelta, timezone
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import IntegrityError, connection as db_connection, transaction
from google.auth.exceptions import RefreshError
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import Workspace
from events.models import Event

from . import google
from .models import CalendarConnection
from .sync import SyncResult, sync_connection
from .views import SESSION_KEY

TEST_KEY = Fernet.generate_key().decode()


class CalendarConnectionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="caregiver")
        self.workspace = Workspace.objects.create(name="Our family", primary_admin=self.user)

    def test_one_connection_per_provider_per_user(self):
        CalendarConnection.objects.create(user=self.user, provider=CalendarConnection.Provider.GOOGLE)

        with self.assertRaises(IntegrityError):
            CalendarConnection.objects.create(user=self.user, provider=CalendarConnection.Provider.GOOGLE)

    def test_defaults_to_primary_calendar(self):
        connection = CalendarConnection.objects.create(
            user=self.user,
            workspace=self.workspace,
            provider=CalendarConnection.Provider.GOOGLE,
            account_email="caregiver@example.com",
        )

        self.assertEqual(connection.calendar_id, "primary")
        self.assertEqual(str(connection), "Google Calendar (caregiver@example.com)")


class ImportedEventTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="caregiver")
        self.workspace = Workspace.objects.create(name="Our family", primary_admin=self.user)
        self.connection = CalendarConnection.objects.create(
            user=self.user,
            workspace=self.workspace,
            provider=CalendarConnection.Provider.GOOGLE,
        )
        self.starts_at = datetime(2026, 10, 20, 15, 30, tzinfo=timezone.utc)

    def create_event(self, **fields):
        return Event.objects.create(
            created_by=self.user,
            workspace=self.workspace,
            title="Soccer practice",
            starts_at=self.starts_at,
            ends_at=self.starts_at + timedelta(hours=1),
            **fields,
        )

    def test_syncmily_event_is_the_default_source(self):
        event = self.create_event()

        self.assertEqual(event.source, Event.Source.SYNCMILY)
        self.assertEqual(event.external_id, "")
        self.assertIsNone(event.calendar_connection)

    def test_imported_event_keeps_its_google_id(self):
        event = self.create_event(
            source=Event.Source.GOOGLE,
            calendar_connection=self.connection,
            external_id="abc123",
        )

        self.assertEqual(list(self.connection.events.all()), [event])

    def test_same_google_event_cannot_be_imported_twice(self):
        fields = {
            "source": Event.Source.GOOGLE,
            "calendar_connection": self.connection,
            "external_id": "abc123",
        }
        self.create_event(**fields)

        with self.assertRaises(IntegrityError):
            self.create_event(**fields)

    def test_imported_fields_must_match_source(self):
        invalid_cases = {
            "google event without id": {
                "source": Event.Source.GOOGLE,
                "calendar_connection": self.connection,
            },
            "google event without connection": {
                "source": Event.Source.GOOGLE,
                "external_id": "abc123",
            },
            "syncmily event with google id": {"external_id": "abc123"},
        }
        for case, fields in invalid_cases.items():
            with self.subTest(case=case):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    self.create_event(**fields)

    def test_disconnecting_removes_imported_events_only(self):
        own_event = self.create_event()
        self.create_event(
            source=Event.Source.GOOGLE,
            calendar_connection=self.connection,
            external_id="abc123",
        )

        self.connection.delete()

        self.assertEqual(list(Event.objects.all()), [own_event])


@override_settings(FIELD_ENCRYPTION_KEYS=[TEST_KEY])
class TokenEncryptionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="caregiver")
        self.connection = CalendarConnection.objects.create(
            user=self.user,
            provider=CalendarConnection.Provider.GOOGLE,
            access_token="ya29.access",
            refresh_token="1//refresh",
        )

    def raw_tokens(self):
        with db_connection.cursor() as cursor:
            cursor.execute(
                "SELECT access_token, refresh_token FROM calendars_calendarconnection WHERE id = %s",
                [self.connection.pk],
            )
            return cursor.fetchone()

    def test_tokens_are_stored_as_ciphertext(self):
        for raw in self.raw_tokens():
            self.assertTrue(raw.startswith("gAAAAA"))
            self.assertNotIn("ya29", raw)
            self.assertNotIn("1//", raw)

    def test_tokens_decrypt_when_loaded(self):
        connection = CalendarConnection.objects.get(pk=self.connection.pk)

        self.assertEqual(connection.access_token, "ya29.access")
        self.assertEqual(connection.refresh_token, "1//refresh")

    def test_tokens_never_appear_in_str_or_repr(self):
        for text in (str(self.connection), repr(self.connection)):
            self.assertNotIn("ya29", text)
            self.assertNotIn("1//", text)

    def test_rotation_re_encrypts_with_the_newest_key(self):
        new_key = Fernet.generate_key().decode()
        old_ciphertext = self.raw_tokens()

        with override_settings(FIELD_ENCRYPTION_KEYS=[new_key, TEST_KEY]):
            call_command("rotate_calendar_tokens", stdout=StringIO())
        self.assertNotEqual(self.raw_tokens(), old_ciphertext)

        with override_settings(FIELD_ENCRYPTION_KEYS=[new_key]):
            connection = CalendarConnection.objects.get(pk=self.connection.pk)
        self.assertEqual(connection.refresh_token, "1//refresh")

    def test_wrong_key_fails_loudly(self):
        with override_settings(FIELD_ENCRYPTION_KEYS=[Fernet.generate_key().decode()]):
            with self.assertRaises(ImproperlyConfigured):
                CalendarConnection.objects.get(pk=self.connection.pk)

    def test_missing_or_invalid_key_stops_production_startup(self):
        config = apps.get_app_config("calendars")
        for keys in ([], ["not-a-fernet-key"]):
            with self.subTest(keys=keys), override_settings(DEBUG=False, FIELD_ENCRYPTION_KEYS=keys):
                with self.assertRaises(ImproperlyConfigured):
                    config.ready()

    def test_missing_key_is_allowed_in_development(self):
        with override_settings(DEBUG=True, FIELD_ENCRYPTION_KEYS=[]):
            apps.get_app_config("calendars").ready()


@override_settings(
    FIELD_ENCRYPTION_KEYS=[TEST_KEY],
    GOOGLE_CLIENT_ID="client-id",
    GOOGLE_CLIENT_SECRET="client-secret",
    GOOGLE_REDIRECT_URI="",
)
class GoogleConnectionViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="caregiver")
        self.other_user = get_user_model().objects.create_user(username="neighbour")
        self.workspace = Workspace.objects.create(name="Our family", primary_admin=self.user)
        self.workspace_url = reverse("accounts:workspace_detail", args=[self.workspace.pk])
        self.client.force_login(self.user)

    def credentials(self, **overrides):
        fields = {
            "token": "ya29.access",
            "refresh_token": "1//refresh",
            "expiry": datetime(2026, 10, 20, 18, 0),
            "granted_scopes": google.SCOPES,
        }
        fields.update(overrides)
        return SimpleNamespace(**fields)

    def start_connection(self):
        with patch("calendars.views.google.authorization_url") as authorization_url:
            authorization_url.return_value = ("https://accounts.google.com/consent", "state-123", "verifier")
            response = self.client.post(reverse("calendars:google_connect", args=[self.workspace.pk]))
        return response, authorization_url

    def callback(self, query, credentials=None, email="caregiver@example.com", error=None):
        with patch("calendars.views.google.exchange_code") as exchange_code, patch(
            "calendars.views.sync_connection", return_value=SyncResult()
        ):
            exchange_code.side_effect = error
            exchange_code.return_value = (credentials or self.credentials(), email)
            response = self.client.get(reverse("calendars:google_callback"), query)
        return response, exchange_code

    def message_texts(self, response):
        return [str(message) for message in get_messages(response.wsgi_request)]

    def test_connect_redirects_to_google_and_remembers_the_request(self):
        response, authorization_url = self.start_connection()

        self.assertRedirects(response, "https://accounts.google.com/consent", fetch_redirect_response=False)
        authorization_url.assert_called_once_with("http://testserver/calendars/google/callback/")
        self.assertEqual(
            self.client.session[SESSION_KEY],
            {"state": "state-123", "code_verifier": "verifier", "workspace_id": str(self.workspace.pk)},
        )

    def test_connect_requires_post_and_workspace_admin(self):
        url = reverse("calendars:google_connect", args=[self.workspace.pk])
        self.assertEqual(self.client.get(url).status_code, 405)

        self.client.force_login(self.other_user)
        self.assertEqual(self.client.post(url).status_code, 404)

    @override_settings(GOOGLE_CLIENT_ID="", GOOGLE_CLIENT_SECRET="")
    def test_connect_explains_when_google_is_not_configured(self):
        response = self.client.post(reverse("calendars:google_connect", args=[self.workspace.pk]))

        self.assertRedirects(response, self.workspace_url)
        self.assertIn("Google Calendar isn’t set up on this server yet.", self.message_texts(response))

    def test_callback_saves_encrypted_connection(self):
        self.start_connection()
        response, exchange_code = self.callback({"state": "state-123", "code": "auth-code"})

        self.assertRedirects(response, self.workspace_url)
        exchange_code.assert_called_once_with(
            "http://testserver/calendars/google/callback/", "auth-code", "verifier"
        )
        connection = CalendarConnection.objects.get(user=self.user)
        self.assertEqual(connection.workspace, self.workspace)
        self.assertEqual(connection.account_email, "caregiver@example.com")
        self.assertEqual(connection.refresh_token, "1//refresh")
        self.assertEqual(connection.token_expires_at, datetime(2026, 10, 20, 18, 0, tzinfo=timezone.utc))
        self.assertIn("Google Calendar connected for caregiver@example.com.", self.message_texts(response))
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_reconnecting_updates_the_existing_connection(self):
        for refresh_token in ("1//first", "1//second"):
            self.start_connection()
            self.callback({"state": "state-123", "code": "auth-code"}, self.credentials(refresh_token=refresh_token))

        self.assertEqual(CalendarConnection.objects.get(user=self.user).refresh_token, "1//second")

    def test_callback_rejects_a_mismatched_state(self):
        self.start_connection()
        response, exchange_code = self.callback({"state": "forged", "code": "auth-code"})

        self.assertRedirects(response, reverse("events:choose_workspace"))
        exchange_code.assert_not_called()
        self.assertFalse(CalendarConnection.objects.exists())

    def test_callback_without_a_started_connection_is_rejected(self):
        response, exchange_code = self.callback({"state": "state-123", "code": "auth-code"})

        self.assertRedirects(response, reverse("events:choose_workspace"))
        exchange_code.assert_not_called()

    def test_failures_are_explained_and_nothing_is_saved(self):
        cases = {
            "declined": (
                {"error": "access_denied"},
                {},
                "Google Calendar wasn’t connected because access was declined.",
            ),
            "exchange error": (
                {"code": "auth-code"},
                {"error": ValueError("bad code")},
                "Google couldn’t connect your calendar. Please try again.",
            ),
            "calendar unticked": (
                {"code": "auth-code"},
                {"credentials": self.credentials(granted_scopes=google.SCOPES[:2])},
                "Syncmily needs permission to see your calendar. Connect again and tick the calendar box on Google’s screen.",
            ),
            "no refresh token": (
                {"code": "auth-code"},
                {"credentials": self.credentials(refresh_token=None)},
                "Google didn’t allow ongoing access. Please try connecting again.",
            ),
        }
        for case, (query, callback_kwargs, message) in cases.items():
            with self.subTest(case=case):
                self.start_connection()
                response, _ = self.callback({"state": "state-123", **query}, **callback_kwargs)

                self.assertRedirects(response, self.workspace_url)
                self.assertIn(message, self.message_texts(response))
                self.assertFalse(CalendarConnection.objects.exists())

    def test_disconnect_revokes_and_deletes_imported_events(self):
        connection = CalendarConnection.objects.create(
            user=self.user,
            workspace=self.workspace,
            provider=CalendarConnection.Provider.GOOGLE,
            refresh_token="1//refresh",
        )
        starts_at = datetime(2026, 10, 20, 15, 30, tzinfo=timezone.utc)
        Event.objects.create(
            created_by=self.user,
            workspace=self.workspace,
            title="Imported",
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=1),
            source=Event.Source.GOOGLE,
            calendar_connection=connection,
            external_id="abc123",
        )

        with patch("calendars.views.google.revoke", return_value=True) as revoke:
            response = self.client.post(reverse("calendars:google_disconnect"))

        revoke.assert_called_once_with("1//refresh")
        self.assertRedirects(response, self.workspace_url)
        self.assertFalse(CalendarConnection.objects.exists())
        self.assertFalse(Event.objects.exists())
        self.assertIn("Google Calendar disconnected.", self.message_texts(response))

    def test_disconnect_warns_when_google_does_not_confirm(self):
        CalendarConnection.objects.create(
            user=self.user,
            workspace=self.workspace,
            provider=CalendarConnection.Provider.GOOGLE,
            refresh_token="1//refresh",
        )

        with patch("calendars.views.google.revoke", return_value=False):
            response = self.client.post(reverse("calendars:google_disconnect"))

        self.assertFalse(CalendarConnection.objects.exists())
        self.assertIn("myaccount.google.com/permissions", self.message_texts(response)[0])

    def test_workspace_page_shows_connect_then_disconnect(self):
        response = self.client.get(self.workspace_url)
        self.assertContains(response, "Connect Google Calendar")

        CalendarConnection.objects.create(
            user=self.user,
            workspace=self.workspace,
            provider=CalendarConnection.Provider.GOOGLE,
            account_email="caregiver@example.com",
            refresh_token="1//refresh",
        )
        response = self.client.get(self.workspace_url)
        self.assertContains(response, "Connected as caregiver@example.com.")
        self.assertContains(response, "Disconnect Google Calendar")
        self.assertNotContains(response, "1//refresh")


    def test_switching_accounts_revokes_the_old_one_and_drops_its_events(self):
        old = CalendarConnection.objects.create(
            user=self.user, workspace=self.workspace, provider=CalendarConnection.Provider.GOOGLE,
            account_email="old@example.com", refresh_token="1//old",
        )
        Event.objects.create(
            created_by=self.user, workspace=self.workspace, title="From the old account",
            starts_at=datetime(2030, 1, 1, 9, tzinfo=timezone.utc), ends_at=datetime(2030, 1, 1, 10, tzinfo=timezone.utc),
            source=Event.Source.GOOGLE, calendar_connection=old, external_id="old-event",
        )
        self.start_connection()
        with patch("calendars.views.google.revoke", return_value=True) as revoke:
            self.callback({"state": "state-123", "code": "auth-code"}, self.credentials(refresh_token="1//new"),
                          email="new@example.com")

        revoke.assert_called_once_with("1//old")
        connection = CalendarConnection.objects.get(user=self.user)
        self.assertEqual((connection.account_email, connection.refresh_token), ("new@example.com", "1//new"))
        self.assertFalse(Event.objects.filter(external_id="old-event").exists())

    def test_reconnecting_the_same_account_keeps_its_access(self):
        CalendarConnection.objects.create(
            user=self.user, workspace=self.workspace, provider=CalendarConnection.Provider.GOOGLE,
            account_email="caregiver@example.com", refresh_token="1//old",
        )
        self.start_connection()
        with patch("calendars.views.google.revoke") as revoke:
            self.callback({"state": "state-123", "code": "auth-code"})
        revoke.assert_not_called()

    def test_connect_returns_to_a_local_next_page_only(self):
        schedule = reverse("events:list") + "?view=week"
        for next_url, expected in ((schedule, schedule), ("https://evil.example/", self.workspace_url)):
            with self.subTest(next_url=next_url):
                with patch("calendars.views.google.authorization_url") as authorization_url:
                    authorization_url.return_value = ("https://accounts.google.com/consent", "state-123", "verifier")
                    self.client.post(reverse("calendars:google_connect", args=[self.workspace.pk]), {"next": next_url})
                response, _ = self.callback({"state": "state-123", "code": "auth-code"})
                self.assertRedirects(response, expected, fetch_redirect_response=False)

    def test_schedule_offers_connect_then_change_account(self):
        schedule = reverse("events:list")
        connect = reverse("calendars:google_connect", args=[self.workspace.pk])
        response = self.client.get(schedule)
        self.assertContains(response, "Connect Google Calendar")
        self.assertContains(response, f'action="{connect}"')

        CalendarConnection.objects.create(
            user=self.user, workspace=self.workspace, provider=CalendarConnection.Provider.GOOGLE,
            account_email="caregiver@example.com", refresh_token="1//refresh",
        )
        response = self.client.get(schedule)
        self.assertContains(response, "caregiver@example.com")
        self.assertContains(response, "Change Google Account")
        self.assertNotContains(response, "1//refresh")

    def test_schedule_hides_the_button_from_people_who_run_no_family(self):
        self.client.force_login(self.other_user)
        response = self.client.get(reverse("events:list"))
        self.assertNotContains(response, "Connect Google Calendar")

class GoogleHelperTests(TestCase):
    @override_settings(GOOGLE_CLIENT_ID="client-id", GOOGLE_CLIENT_SECRET="client-secret")
    def test_authorization_url_asks_for_read_only_offline_access_with_pkce(self):
        url, state, code_verifier = google.authorization_url("https://syncmily.example/calendars/google/callback/")

        self.assertTrue(url.startswith("https://accounts.google.com/o/oauth2/auth?"))
        for part in (
            "client_id=client-id",
            "access_type=offline",
            "prompt=select_account+consent",
            "code_challenge_method=S256",
            f"state={state}",
            "calendar.readonly",
        ):
            self.assertIn(part, url)
        self.assertNotIn("client-secret", url)
        self.assertGreaterEqual(len(code_verifier), 43)

    def test_revoke_reports_network_failure(self):
        with patch("calendars.google.requests.post", side_effect=google.requests.ConnectionError):
            self.assertFalse(google.revoke("1//refresh"))


class FakeCalendarService:
    """Stands in for googleapiclient's Calendar service, one page per list call."""

    def __init__(self, pages, time_zone="America/New_York"):
        self.pages = list(pages)
        self.time_zone = time_zone
        self.list_kwargs = []

    def events(self):
        return self

    def list(self, **kwargs):
        self.list_kwargs.append(kwargs)
        return SimpleNamespace(execute=lambda: {"timeZone": self.time_zone, "items": self.pages.pop(0)})

    def list_next(self, request, response):
        return self.list() if self.pages else None


@override_settings(FIELD_ENCRYPTION_KEYS=[TEST_KEY])
class CalendarSyncTests(TestCase):
    now = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="caregiver")
        self.workspace = Workspace.objects.create(name="Our family", primary_admin=self.user)
        self.connection = CalendarConnection.objects.create(
            user=self.user,
            workspace=self.workspace,
            provider=CalendarConnection.Provider.GOOGLE,
            access_token="ya29.access",
            refresh_token="1//refresh",
            token_expires_at=self.now + timedelta(hours=1),
            scopes=" ".join(google.SCOPES),
        )

    def sync(self, *pages):
        service = FakeCalendarService(pages)
        with patch("calendars.sync._service", return_value=service):
            result = sync_connection(self.connection, now=self.now)
        return result, service

    def timed(self, event_id, summary="Soccer practice", start="2026-10-21T15:30:00-04:00",
              end="2026-10-21T16:30:00-04:00", **extra):
        return {
            "id": event_id,
            "status": "confirmed",
            "summary": summary,
            "start": {"dateTime": start, "timeZone": "America/New_York"},
            "end": {"dateTime": end, "timeZone": "America/New_York"},
            **extra,
        }

    def test_imports_timed_and_all_day_events_into_the_workspace(self):
        all_day = {"id": "holiday", "summary": "Thanksgiving", "start": {"date": "2026-11-26"},
                   "end": {"date": "2026-11-27"}}
        result, service = self.sync([self.timed("soccer", location="School field")], [all_day])

        self.assertEqual((result.created, result.updated, result.deleted, result.skipped), (2, 0, 0, 0))
        soccer = Event.objects.get(external_id="soccer")
        self.assertEqual(soccer.workspace, self.workspace)
        self.assertEqual(soccer.created_by, self.user)
        self.assertEqual(soccer.source, Event.Source.GOOGLE)
        self.assertEqual(soccer.location, "School field")
        self.assertEqual(soccer.starts_at, datetime(2026, 10, 21, 19, 30, tzinfo=timezone.utc))
        self.assertEqual(soccer.timezone_name, "America/New_York")
        holiday = Event.objects.get(external_id="holiday")
        self.assertEqual(holiday.starts_at, datetime(2026, 11, 26, 5, 0, tzinfo=timezone.utc))
        self.assertEqual(holiday.ends_at - holiday.starts_at, timedelta(days=1))

        first_call = service.list_kwargs[0]
        self.assertEqual(first_call["calendarId"], "primary")
        self.assertTrue(first_call["singleEvents"])
        self.assertEqual(first_call["timeMin"], "2026-09-20T12:00:00+00:00")
        self.assertEqual(first_call["timeMax"], "2027-04-18T12:00:00+00:00")
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.last_synced_at, self.now)

    def test_resync_updates_changes_and_does_not_duplicate(self):
        self.sync([self.timed("soccer")])
        result, _ = self.sync([self.timed("soccer", summary="Soccer final")])
        unchanged, _ = self.sync([self.timed("soccer", summary="Soccer final")])

        self.assertEqual((result.created, result.updated), (0, 1))
        self.assertEqual((unchanged.created, unchanged.updated), (0, 0))
        self.assertEqual(Event.objects.get().title, "Soccer final")

    def test_cancelled_and_missing_events_are_removed(self):
        self.sync([self.timed("soccer"), self.timed("piano")])
        result, _ = self.sync([{"id": "soccer", "status": "cancelled"}])

        self.assertEqual(result.deleted, 2)
        self.assertFalse(Event.objects.exists())

    def test_events_outside_the_window_are_kept(self):
        self.sync([self.timed("old", start="2026-01-05T10:00:00Z", end="2026-01-05T11:00:00Z")])
        self.sync([])

        self.assertTrue(Event.objects.filter(external_id="old").exists())

    def test_syncmily_events_are_never_touched(self):
        own = Event.objects.create(
            created_by=self.user,
            workspace=self.workspace,
            title="Dentist",
            starts_at=self.now,
            ends_at=self.now + timedelta(hours=1),
        )
        self.sync([])

        self.assertTrue(Event.objects.filter(pk=own.pk).exists())

    def test_zero_length_and_untitled_events(self):
        reminder = self.timed("reminder", end="2026-10-21T15:30:00-04:00")
        untitled = self.timed("untitled", summary="  ")
        result, _ = self.sync([reminder, untitled])

        self.assertEqual(result.skipped, 1)
        self.assertEqual(Event.objects.get().title, "(No title)")

    def test_refreshed_access_token_is_saved_encrypted(self):
        service = FakeCalendarService([[]])

        def refreshing_service(credentials):
            credentials.token = "ya29.new"
            credentials.expiry = datetime(2026, 10, 20, 14, 0)
            return service

        with patch("calendars.sync._service", side_effect=refreshing_service):
            sync_connection(self.connection, now=self.now)

        connection = CalendarConnection.objects.get(pk=self.connection.pk)
        self.assertEqual(connection.access_token, "ya29.new")
        self.assertEqual(connection.token_expires_at, datetime(2026, 10, 20, 14, 0, tzinfo=timezone.utc))

    def test_sync_now_button_reports_results_and_failures(self):
        self.client.force_login(self.user)
        url = reverse("calendars:google_sync")
        workspace_url = reverse("accounts:workspace_detail", args=[self.workspace.pk])
        cases = {
            "success": ({"return_value": SyncResult()}, "Google Calendar events imported: 0 added"),
            "revoked": ({"side_effect": RefreshError("revoked")}, "Disconnect and connect Google Calendar again."),
            "api error": ({"side_effect": ValueError("boom")}, "We couldn’t import your Google Calendar events."),
        }
        for case, (behaviour, message) in cases.items():
            with self.subTest(case=case), patch("calendars.views.sync_connection", **behaviour):
                response = self.client.post(url)

                self.assertRedirects(response, workspace_url)
                texts = [str(m) for m in get_messages(response.wsgi_request)]
                self.assertTrue(any(message in text for text in texts), texts)

    def test_imported_events_appear_in_the_family_schedule(self):
        self.sync([self.timed("soccer")])
        self.client.force_login(self.user)

        response = self.client.get(
            reverse("events:workspace_list", args=[self.workspace.pk]),
            # The day view shows the event whatever today's date is ("upcoming" depends on the clock).
            {"view": "day", "date": "2026-10-21", "timezone": "America/New_York"},
        )

        self.assertContains(response, "Soccer practice")
        self.assertContains(response, "From Google Calendar")

    def test_sync_command_reports_each_connection(self):
        out = StringIO()
        with patch("calendars.management.commands.sync_calendars.sync_connection", return_value=SyncResult()):
            call_command("sync_calendars", stdout=out)

        self.assertIn(f"Connection {self.connection.pk}: 0 added", out.getvalue())


class SignedOutRedirectTests(TestCase):
    def test_calendar_pages_send_signed_out_users_to_the_app_login(self):
        response = self.client.get(reverse("calendars:google_callback"))

        self.assertRedirects(
            response,
            f"{reverse('accounts:login')}?next={reverse('calendars:google_callback')}",
            fetch_redirect_response=False,
        )
