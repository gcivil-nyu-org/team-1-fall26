from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone as django_timezone

from accounts.models import Membership, Workspace
from calendars.models import CalendarConnection

from ..models import Event

NEW_YORK = ZoneInfo("America/New_York")


class ChangeTimeFromEventPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.admin = User.objects.create_user(username="Ana", password="FamilyTest2026!")
        cls.parent = User.objects.create_user(username="Mateo", password="x")
        cls.kid = User.objects.create_user(username="Lucia", password="x")
        cls.workspace = Workspace.objects.create(name="Reyes", primary_admin=cls.admin)
        for user, role in (
            (cls.parent, Membership.Role.PARENT),
            (cls.kid, Membership.Role.KID),
        ):
            Membership.objects.create(
                workspace=cls.workspace,
                user=user,
                role=role,
                invited_by=cls.admin,
                accepted_at=django_timezone.now(),
            )

    def setUp(self):
        self.starts = datetime(2026, 11, 20, 16, 0, tzinfo=NEW_YORK)
        self.event = Event.objects.create(
            created_by=self.admin,
            workspace=self.workspace,
            title="Soccer",
            starts_at=self.starts,
            ends_at=self.starts + timedelta(hours=1),
            timezone_name="America/New_York",
        )
        self.client.force_login(self.admin)

    def change(self, starts_at, ends_at, user=None):
        if user:
            self.client.force_login(user)
        return self.client.post(
            reverse("events:change_time", args=[self.event.pk]),
            {"starts_at": starts_at, "ends_at": ends_at},
        )

    def test_page_shows_the_time_as_a_change_form_with_current_values(self):
        response = self.client.get(reverse("events:detail", args=[self.event.pk]))

        self.assertContains(response, 'class="when-edit"')
        self.assertContains(response, 'value="2026-11-20T16:00"')
        self.assertContains(response, 'value="2026-11-20T17:00"')
        self.assertContains(response, "Delete event")

    def test_changing_the_time_uses_the_events_own_time_zone(self):
        response = self.change("2026-11-20T18:30", "2026-11-20T20:00")

        self.assertRedirects(response, reverse("events:detail", args=[self.event.pk]))
        self.event.refresh_from_db()
        self.assertEqual(
            self.event.starts_at, datetime(2026, 11, 20, 18, 30, tzinfo=NEW_YORK)
        )
        self.assertEqual(
            self.event.ends_at, datetime(2026, 11, 20, 20, 0, tzinfo=NEW_YORK)
        )

    def test_end_must_be_after_start_and_both_are_needed(self):
        for starts_at, ends_at in (
            ("2026-11-20T18:00", "2026-11-20T17:00"),
            ("2026-11-20T18:00", ""),
            ("not a time", "2026-11-20T19:00"),
        ):
            with self.subTest(starts_at=starts_at, ends_at=ends_at):
                response = self.change(starts_at, ends_at)
                self.assertEqual(response.status_code, 302)
                self.event.refresh_from_db()
                self.assertEqual(self.event.starts_at, self.starts)

    def test_other_parents_can_change_but_kids_cannot(self):
        self.assertEqual(
            self.change(
                "2026-11-20T17:00", "2026-11-20T18:00", self.parent
            ).status_code,
            302,
        )
        self.event.refresh_from_db()
        self.assertEqual(self.event.starts_at.hour, 22)  # 17:00 New York in UTC

        self.client.force_login(self.kid)
        self.assertEqual(
            self.change("2026-11-20T09:00", "2026-11-20T10:00").status_code, 404
        )
        page = self.client.get(reverse("events:detail", args=[self.event.pk]))
        self.assertNotContains(page, "Delete event")
        self.assertNotContains(page, 'class="when-edit"')

    def test_google_calendar_events_stay_read_only(self):
        connection = CalendarConnection.objects.create(
            user=self.admin,
            workspace=self.workspace,
            provider=CalendarConnection.Provider.GOOGLE,
        )
        Event.objects.filter(pk=self.event.pk).update(
            source=Event.Source.GOOGLE,
            calendar_connection=connection,
            external_id="g-1",
        )

        self.assertEqual(
            self.change("2026-11-20T09:00", "2026-11-20T10:00").status_code, 404
        )
        page = self.client.get(reverse("events:detail", args=[self.event.pk]))
        self.assertNotContains(page, 'class="when-edit"')
