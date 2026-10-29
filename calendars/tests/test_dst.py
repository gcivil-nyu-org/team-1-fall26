"""Google import around the US daylight-saving changes (2026-03-08 and 2026-11-01)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from accounts.models import Workspace
from events.models import Event

from .. import google
from ..models import CalendarConnection
from ..sync import FUTURE_DAYS, PAST_DAYS, sync_connection
from .test_core import TEST_KEY, FakeCalendarService

NEW_YORK = ZoneInfo("America/New_York")
UTC = timezone.utc


@override_settings(FIELD_ENCRYPTION_KEYS=[TEST_KEY])
class DaylightSavingSyncTests(TestCase):
    now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)

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

    def sync(self, *pages, now=None, calendar_zone="America/New_York"):
        service = FakeCalendarService(pages, time_zone=calendar_zone)
        with patch("calendars.sync._service", return_value=service):
            result = sync_connection(self.connection, now=now or self.now)
        return result, service

    def item(self, event_id, start, end, zone="America/New_York"):
        start_field, end_field = {"dateTime": start}, {"dateTime": end}
        if zone:
            start_field["timeZone"] = end_field["timeZone"] = zone
        return {
            "id": event_id,
            "status": "confirmed",
            "summary": event_id,
            "start": start_field,
            "end": end_field,
        }

    def all_day(self, event_id, start, end):
        return {
            "id": event_id,
            "summary": event_id,
            "start": {"date": start},
            "end": {"date": end},
        }

    def stored(self, external_id):
        return Event.objects.get(external_id=external_id)

    def test_ambiguous_hour_offsets_pick_the_right_instant(self):
        # 01:30 happens twice on Nov 1: first at -04:00 (EDT), again at -05:00 (EST).
        self.sync(
            [
                self.item(
                    "first", "2026-11-01T01:30:00-04:00", "2026-11-01T01:45:00-04:00"
                ),
                self.item(
                    "second", "2026-11-01T01:30:00-05:00", "2026-11-01T01:45:00-05:00"
                ),
            ]
        )
        first, second = self.stored("first"), self.stored("second")
        self.assertEqual(first.starts_at, datetime(2026, 11, 1, 5, 30, tzinfo=UTC))
        self.assertEqual(second.starts_at, datetime(2026, 11, 1, 6, 30, tzinfo=UTC))
        # Both render as 1:30 AM on the wall clock; only the stored instant tells them
        # apart.
        self.assertEqual(first.starts_at.astimezone(NEW_YORK).hour, 1)
        self.assertEqual(second.starts_at.astimezone(NEW_YORK).hour, 1)
        self.assertEqual(first.starts_at.astimezone(NEW_YORK).fold, 0)
        self.assertEqual(second.starts_at.astimezone(NEW_YORK).fold, 1)

    def test_event_spanning_the_change_keeps_its_real_duration(self):
        # 00:30 EDT to 03:30 EST is four real hours, although the wall clock moves
        # three.
        self.sync(
            [
                self.item(
                    "night", "2026-11-01T00:30:00-04:00", "2026-11-01T03:30:00-05:00"
                )
            ]
        )
        event = self.stored("night")
        self.assertEqual(event.ends_at - event.starts_at, timedelta(hours=4))
        self.assertEqual(event.timezone_name, "America/New_York")

    def test_event_spanning_the_spring_change_keeps_its_real_duration(self):
        # 01:30 EST to 03:30 EDT on Mar 8 is one real hour.
        self.sync(
            [
                self.item(
                    "spring", "2026-03-08T01:30:00-05:00", "2026-03-08T03:30:00-04:00"
                )
            ],
            now=datetime(2026, 3, 1, tzinfo=UTC),
        )
        event = self.stored("spring")
        self.assertEqual(event.ends_at - event.starts_at, timedelta(hours=1))

    def test_all_day_events_span_the_local_midnights(self):
        self.sync(
            [
                self.all_day("fall-back", "2026-11-01", "2026-11-02"),
                self.all_day("weekend", "2026-10-31", "2026-11-02"),
            ]
        )
        fall_back, weekend = self.stored("fall-back"), self.stored("weekend")
        self.assertEqual(
            fall_back.starts_at, datetime(2026, 11, 1, 4, tzinfo=UTC)
        )  # midnight EDT
        self.assertEqual(
            fall_back.ends_at, datetime(2026, 11, 2, 5, tzinfo=UTC)
        )  # midnight EST
        self.assertEqual(fall_back.ends_at - fall_back.starts_at, timedelta(hours=25))
        self.assertEqual(weekend.ends_at - weekend.starts_at, timedelta(hours=49))

    def test_all_day_event_on_the_spring_change_is_23_hours(self):
        self.sync(
            [self.all_day("spring", "2026-03-08", "2026-03-09")],
            now=datetime(2026, 3, 1, tzinfo=UTC),
        )
        event = self.stored("spring")
        self.assertEqual(event.ends_at - event.starts_at, timedelta(hours=23))

    def test_all_day_event_uses_the_calendar_zone(self):
        # A Los Angeles calendar's Nov 1 runs 07:00Z to 08:00Z the next day (also 25
        # hours).
        self.sync(
            [self.all_day("la-day", "2026-11-01", "2026-11-02")],
            calendar_zone="America/Los_Angeles",
        )
        event = self.stored("la-day")
        self.assertEqual(event.starts_at, datetime(2026, 11, 1, 7, tzinfo=UTC))
        self.assertEqual(event.ends_at, datetime(2026, 11, 2, 8, tzinfo=UTC))
        self.assertEqual(event.timezone_name, "America/Los_Angeles")

    def test_all_day_event_in_a_zone_without_daylight_saving(self):
        self.sync(
            [self.all_day("phoenix", "2026-11-01", "2026-11-02")],
            calendar_zone="America/Phoenix",
        )
        event = self.stored("phoenix")
        self.assertEqual(event.ends_at - event.starts_at, timedelta(hours=24))

    def test_recurring_instances_keep_the_wall_clock_across_the_change(self):
        # singleEvents=True expands "daily at 9:00 New York"; Google moves the offset,
        # not the hour.
        self.sync(
            [
                self.item(
                    "daily_20261031",
                    "2026-10-31T09:00:00-04:00",
                    "2026-10-31T10:00:00-04:00",
                ),
                self.item(
                    "daily_20261101",
                    "2026-11-01T09:00:00-05:00",
                    "2026-11-01T10:00:00-05:00",
                ),
            ]
        )
        before, after = self.stored("daily_20261031"), self.stored("daily_20261101")
        self.assertEqual(before.starts_at.astimezone(NEW_YORK).hour, 9)
        self.assertEqual(after.starts_at.astimezone(NEW_YORK).hour, 9)
        self.assertEqual(after.starts_at - before.starts_at, timedelta(hours=25))
        self.assertEqual(
            before.ends_at - before.starts_at, after.ends_at - after.starts_at
        )

    def test_resync_of_instances_across_the_change_is_idempotent(self):
        def pages():
            return [
                self.item(
                    "daily_20261031",
                    "2026-10-31T09:00:00-04:00",
                    "2026-10-31T10:00:00-04:00",
                ),
                self.item(
                    "daily_20261101",
                    "2026-11-01T09:00:00-05:00",
                    "2026-11-01T10:00:00-05:00",
                ),
            ]

        self.sync(pages())
        result, _ = self.sync(pages())
        self.assertEqual((result.created, result.updated, result.deleted), (0, 0, 0))

    def test_event_in_another_zone_keeps_that_zone_and_instant(self):
        # Paris left summer time on Oct 25, a week before New York.
        self.sync(
            [
                self.item(
                    "paris",
                    "2026-10-31T09:00:00+01:00",
                    "2026-10-31T10:00:00+01:00",
                    zone="Europe/Paris",
                )
            ]
        )
        event = self.stored("paris")
        self.assertEqual(event.timezone_name, "Europe/Paris")
        self.assertEqual(event.starts_at, datetime(2026, 10, 31, 8, tzinfo=UTC))
        self.assertEqual(
            event.starts_at.astimezone(NEW_YORK).hour, 4
        )  # still EDT in New York

    def test_event_without_its_own_zone_takes_the_calendar_zone(self):
        self.sync(
            [
                self.item(
                    "plain",
                    "2026-11-01T09:00:00-05:00",
                    "2026-11-01T10:00:00-05:00",
                    zone=None,
                )
            ],
            calendar_zone="America/Chicago",
        )
        self.assertEqual(self.stored("plain").timezone_name, "America/Chicago")

    def test_offset_less_local_time_is_read_in_the_event_zone(self):
        # API v3: dateTime may omit the offset when timeZone is given. 01:30 is
        # ambiguous on Nov 1;
        # Google, like Python's fold=0, means the first occurrence (EDT).
        self.sync(
            [
                self.item("naive", "2026-11-01T01:30:00", "2026-11-01T02:30:00"),
                self.item(
                    "naive-paris",
                    "2026-10-31T09:00:00",
                    "2026-10-31T10:00:00",
                    zone="Europe/Paris",
                ),
            ]
        )
        naive, paris = self.stored("naive"), self.stored("naive-paris")
        self.assertEqual(naive.starts_at, datetime(2026, 11, 1, 5, 30, tzinfo=UTC))
        self.assertEqual(naive.ends_at, datetime(2026, 11, 1, 7, 30, tzinfo=UTC))
        self.assertEqual(paris.starts_at, datetime(2026, 10, 31, 8, tzinfo=UTC))

    def test_request_window_is_exact_utc_days_around_now(self):
        now = datetime(2026, 12, 1, 6, 30, tzinfo=UTC)
        _, service = self.sync([], now=now)
        [call] = service.list_kwargs
        # 30 days before is Nov 1 06:30Z, the second 01:30 in New York.
        self.assertEqual(
            datetime.fromisoformat(call["timeMin"]),
            datetime(2026, 11, 1, 6, 30, tzinfo=UTC),
        )
        self.assertEqual(
            datetime.fromisoformat(call["timeMax"]), now + timedelta(days=FUTURE_DAYS)
        )
        self.assertTrue(
            call["timeMin"].endswith("+00:00") and call["timeMax"].endswith("+00:00")
        )

    def test_window_edges_around_the_change(self):
        now = datetime(2026, 12, 1, 6, 30, tzinfo=UTC)
        edge = now - timedelta(days=PAST_DAYS)
        far = now + timedelta(days=FUTURE_DAYS)

        def make(external_id, starts_at, ends_at):
            Event.objects.create(
                title=external_id,
                starts_at=starts_at,
                ends_at=ends_at,
                timezone_name="America/New_York",
                workspace=self.workspace,
                created_by=self.user,
                source=Event.Source.GOOGLE,
                calendar_connection=self.connection,
                external_id=external_id,
            )

        make(
            "ends-at-edge", edge - timedelta(hours=1), edge
        )  # outside: ends exactly at timeMin
        make("ends-after-edge", edge - timedelta(hours=1), edge + timedelta(minutes=1))
        make(
            "starts-at-max", far, far + timedelta(hours=1)
        )  # outside: starts exactly at timeMax
        make("starts-before-max", far - timedelta(minutes=1), far + timedelta(hours=1))
        result, _ = self.sync([], now=now)
        self.assertEqual(result.deleted, 2)
        self.assertEqual(
            set(Event.objects.values_list("external_id", flat=True)),
            {"ends-at-edge", "starts-at-max"},
        )
