from datetime import datetime, timedelta, timezone

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase

from accounts.models import Workspace
from events.models import Event

from .models import CalendarConnection


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
