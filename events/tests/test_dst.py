"""Day, Week and Upcoming schedule views around the US daylight-saving changes."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone as django_timezone

from accounts.models import Workspace
from calendars.models import CalendarConnection

from ..forms import EventForm
from ..models import Event

NEW_YORK = ZoneInfo("America/New_York")
UTC = timezone.utc


class DaylightSavingScheduleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_user(username="Parent")
        cls.workspace = Workspace.objects.create(name="Family", primary_admin=cls.admin)

    def setUp(self):
        self.client.force_login(self.admin)
        self.url = reverse("events:workspace_list", args=[self.workspace.pk])

    def event(self, title, starts_at, ends_at, **fields):
        return Event.objects.create(
            title=title, starts_at=starts_at, ends_at=ends_at, timezone_name="America/New_York",
            workspace=self.workspace, created_by=self.admin, **fields,
        )

    def google_all_day(self, title, start_date, end_date):
        """What calendars.sync stores for start.date / end.date (end exclusive): local midnights."""
        connection = CalendarConnection.objects.get_or_create(
            user=self.admin, provider=CalendarConnection.Provider.GOOGLE, defaults={"workspace": self.workspace},
        )[0]
        return self.event(
            title, datetime.combine(start_date, datetime.min.time(), NEW_YORK),
            datetime.combine(end_date, datetime.min.time(), NEW_YORK),
            source=Event.Source.GOOGLE, calendar_connection=connection, external_id=title,
        )

    def get(self, view, date, zone="America/New_York"):
        response = self.client.get(self.url, {"view": view, "date": date, "timezone": zone})
        self.assertEqual(response.status_code, 200)
        return response

    def items(self, response):
        return [item for lane in response.context["timeline"]["lanes"] for item in lane["items"]]

    # --- Day timeline ---

    def test_ambiguous_hour_events_sit_one_real_hour_apart(self):
        first = self.event("First 1:30", datetime(2026, 11, 1, 1, 30, tzinfo=NEW_YORK),
                           datetime(2026, 11, 1, 1, 45, tzinfo=NEW_YORK))
        second = self.event("Second 1:30", datetime(2026, 11, 1, 6, 30, tzinfo=UTC), datetime(2026, 11, 1, 6, 45, tzinfo=UTC))
        self.assertEqual(first.starts_at.astimezone(UTC), datetime(2026, 11, 1, 5, 30, tzinfo=UTC))  # fold=0 is EDT
        lefts = {item["event"].title: item["left"] for item in self.items(self.get("day", "2026-11-01"))}
        self.assertEqual(lefts["First 1:30"], round(1.5 / 25 * 100, 3))
        self.assertEqual(lefts["Second 1:30"], round(2.5 / 25 * 100, 3))
        self.assertEqual(second.starts_at.astimezone(NEW_YORK).strftime("%-I:%M %p"), "1:30 AM")

    def test_event_spanning_the_change_has_its_real_width(self):
        self.event("Night shift", datetime(2026, 11, 1, 0, 30, tzinfo=NEW_YORK), datetime(2026, 11, 1, 3, 30, tzinfo=NEW_YORK))
        [item] = self.items(self.get("day", "2026-11-01"))
        # 00:30 EDT to 03:30 EST is four real hours of a 25-hour day.
        self.assertEqual(item["width"], round(4 / 25 * 100, 3))

    def test_event_spanning_the_spring_change_has_its_real_width(self):
        self.event("Night", datetime(2026, 3, 8, 1, 30, tzinfo=NEW_YORK), datetime(2026, 3, 8, 3, 30, tzinfo=NEW_YORK))
        [item] = self.items(self.get("day", "2026-03-08"))
        self.assertEqual(item["width"], round(1 / 23 * 100, 3))  # one real hour of a 23-hour day

    def test_event_after_spring_change_is_placed_by_elapsed_time(self):
        self.event("Brunch", datetime(2026, 3, 8, 10, tzinfo=NEW_YORK), datetime(2026, 3, 8, 11, tzinfo=NEW_YORK))
        [item] = self.items(self.get("day", "2026-03-08"))
        self.assertEqual(item["left"], round(9 / 23 * 100, 3))  # 10 AM is the tenth hour; only nine have elapsed

    def test_google_all_day_events_fill_the_25_and_23_hour_days(self):
        self.google_all_day("Fall back day", datetime(2026, 11, 1).date(), datetime(2026, 11, 2).date())
        self.google_all_day("Spring forward day", datetime(2026, 3, 8).date(), datetime(2026, 3, 9).date())
        for date, title in (("2026-11-01", "Fall back day"), ("2026-03-08", "Spring forward day")):
            with self.subTest(date=date):
                timeline = self.get("day", date).context["timeline"]
                self.assertEqual([item["event"].title for item in timeline["all_day"]], [title])
                self.assertEqual([item for lane in timeline["lanes"] for item in lane["items"]], [])

    def test_google_all_day_event_does_not_leak_onto_the_neighbouring_days(self):
        self.google_all_day("Fall back day", datetime(2026, 11, 1).date(), datetime(2026, 11, 2).date())
        for date in ("2026-10-31", "2026-11-02"):
            with self.subTest(date=date):
                response = self.get("day", date)
                self.assertEqual(response.context["timeline"]["all_day"], [])
                self.assertEqual(self.items(response), [])
                self.assertEqual(response.context["page"].paginator.count, 0)

    def test_multi_day_all_day_event_covers_the_change_day(self):
        self.google_all_day("Trip", datetime(2026, 10, 30).date(), datetime(2026, 11, 3).date())
        for date in ("2026-10-30", "2026-10-31", "2026-11-01", "2026-11-02"):
            with self.subTest(date=date):
                self.assertEqual(len(self.get("day", date).context["timeline"]["all_day"]), 1)
        self.assertEqual(len(self.get("day", "2026-11-03").context["timeline"]["all_day"]), 0)

    def test_event_across_midnight_before_the_change_is_cut_at_the_25_hour_day(self):
        self.event("Late party", datetime(2026, 10, 31, 22, tzinfo=NEW_YORK), datetime(2026, 11, 1, 3, tzinfo=NEW_YORK))
        [item] = self.items(self.get("day", "2026-11-01"))
        self.assertTrue(item["starts_before"])
        self.assertEqual(item["left"], 0)
        self.assertEqual(item["width"], round(4 / 25 * 100, 3))  # midnight to 3 AM EST: four real hours

    def test_now_marker_uses_elapsed_time_on_the_25_hour_day(self):
        now = datetime(2026, 11, 1, 7, 15, tzinfo=UTC)  # 2:15 AM EST, the fourth hour boundary + 15 min
        with patch("events.views.timezone.now", return_value=now):
            timeline = self.get("day", "2026-11-01").context["timeline"]
        self.assertEqual(timeline["now_left"], round(3.25 / 25 * 100, 3))

    def test_day_with_nothing_scrolls_to_8am_wall_clock_hours_in_elapsed_terms(self):
        # Product choice (open question): on 25-hour days 8/25 lands at 7 AM wall clock, 8/23 on Mar 8 at 9 AM.
        for date, hours in (("2026-11-01", 25), ("2026-03-08", 23), ("2026-10-23", 24)):
            with self.subTest(date=date), patch("events.views.timezone.now", return_value=datetime(2026, 1, 1, tzinfo=UTC)):
                self.assertEqual(self.get("day", date).context["timeline"]["scroll_to"], round(8 / hours * 100, 3))

    def test_axis_labels_on_the_25_hour_day_repeat_1_am(self):
        # Product choice (open question): the axis shows "1 AM" twice and has no EST/EDT marker.
        hours = self.get("day", "2026-11-01").context["timeline"]["hours"]
        labels = [hour["starts_at"].strftime("%-I %p") for hour in hours]
        self.assertEqual(labels.count("1 AM"), 2)
        self.assertEqual(labels[0], "12 AM")
        self.assertEqual(labels[-1], "11 PM")

    def test_axis_labels_on_the_23_hour_day_skip_2_am(self):
        hours = self.get("day", "2026-03-08").context["timeline"]["hours"]
        labels = [hour["starts_at"].strftime("%-I %p") for hour in hours]
        self.assertNotIn("2 AM", labels)
        self.assertEqual(len(labels), 23)

    def test_other_zone_picks_its_own_day_length(self):
        # London left summer time on Oct 25; Paris and London both have 24 hours on Nov 1; Phoenix never changes.
        for zone, hours in (("Europe/London", 24), ("America/Phoenix", 24), ("America/Los_Angeles", 25)):
            with self.subTest(zone=zone):
                self.assertEqual(self.get("day", "2026-11-01", zone).context["timeline"]["hour_count"], hours)
        self.assertEqual(self.get("day", "2026-10-25", "Europe/London").context["timeline"]["hour_count"], 25)

    def test_event_in_another_zone_is_placed_in_the_viewed_zone(self):
        # 09:00 Paris (UTC+1) on Nov 1 is 03:00 EST, four real hours into the New York day.
        self.event("Call", datetime(2026, 11, 1, 9, tzinfo=ZoneInfo("Europe/Paris")),
                   datetime(2026, 11, 1, 10, tzinfo=ZoneInfo("Europe/Paris")))
        [item] = self.items(self.get("day", "2026-11-01"))
        self.assertEqual(item["left"], round(4 / 25 * 100, 3))
        response = self.get("day", "2026-11-01")
        self.assertContains(response, "3:00 AM – 4:00 AM")

    # --- Week and Upcoming ---

    def test_week_view_covers_seven_local_days_across_the_change(self):
        # Mon Oct 26 to Sun Nov 1: the week ends on the 25-hour day; next week starts Mon Nov 2.
        sunday_late = self.event("Sunday late", datetime(2026, 11, 1, 23, 30, tzinfo=NEW_YORK), datetime(2026, 11, 2, 0, tzinfo=NEW_YORK))
        monday_early = self.event("Monday early", datetime(2026, 11, 2, 0, tzinfo=NEW_YORK), datetime(2026, 11, 2, 1, tzinfo=NEW_YORK))
        week = self.get("week", "2026-10-28")
        self.assertEqual(str(week.context["start_date"]), "2026-10-26")
        self.assertEqual(str(week.context["last_date"]), "2026-11-01")
        self.assertEqual([event.pk for event in week.context["page"]], [sunday_late.pk])
        self.assertEqual([event.pk for event in self.get("week", "2026-11-02").context["page"]], [monday_early.pk])

    def test_week_containing_the_change_has_a_169_hour_span(self):
        form_context = self.get("week", "2026-11-01").context["form"].cleaned_data
        self.assertEqual(form_context["ends_at"].astimezone(UTC) - form_context["starts_at"].astimezone(UTC), timedelta(hours=7 * 24 + 1))
        spring = self.get("week", "2026-03-08").context["form"].cleaned_data
        self.assertEqual(spring["ends_at"].astimezone(UTC) - spring["starts_at"].astimezone(UTC), timedelta(hours=7 * 24 - 1))
        self.assertEqual(str(spring["start_date"]), "2026-03-02")

    def test_week_view_includes_event_in_the_repeated_hour(self):
        self.event("Repeated hour", datetime(2026, 11, 1, 6, 30, tzinfo=UTC), datetime(2026, 11, 1, 6, 45, tzinfo=UTC))
        self.assertEqual(len(self.get("week", "2026-11-01").context["page"]), 1)

    def test_previous_and_next_links_step_whole_days_over_the_change(self):
        day = self.get("day", "2026-11-01").context
        self.assertEqual(str(day["form"].cleaned_data["previous_date"]), "2026-10-31")
        self.assertEqual(str(day["form"].cleaned_data["next_date"]), "2026-11-02")

    def test_upcoming_excludes_an_event_that_ended_exactly_now(self):
        self.event("Over", datetime(2026, 11, 1, 5, tzinfo=UTC), datetime(2026, 11, 1, 6, 30, tzinfo=UTC))
        self.event("Repeat hour", datetime(2026, 11, 1, 6, 15, tzinfo=UTC), datetime(2026, 11, 1, 7, 15, tzinfo=UTC))
        # 01:30 EST (second pass): the wall clock reads 1:30 AM, as it did an hour earlier.
        with patch("events.forms.timezone.now", return_value=datetime(2026, 11, 1, 6, 30, tzinfo=UTC)):
            titles = [event.title for event in self.get("upcoming", "2026-11-01").context["page"]]
        self.assertEqual(titles, ["Repeat hour"])

    def test_default_date_follows_the_local_day_not_the_utc_day(self):
        # 23:30 EDT on Oct 31 is already Nov 1 in UTC; the schedule must still open on Oct 31.
        now = datetime(2026, 11, 1, 3, 30, tzinfo=UTC)
        with patch("django.utils.timezone.now", return_value=now):
            response = self.client.get(self.url, {"view": "day", "timezone": "America/New_York"})
        self.assertEqual(str(response.context["form"].cleaned_data["date"]), "2026-10-31")
        # After the fall back, 00:30 EST on Nov 1 (05:30Z) is Nov 1 locally.
        now = datetime(2026, 11, 2, 4, 30, tzinfo=UTC)  # 23:30 EST on Nov 1, already Nov 2 in UTC
        with patch("django.utils.timezone.now", return_value=now):
            response = self.client.get(self.url, {"view": "day", "timezone": "America/New_York"})
        self.assertEqual(str(response.context["form"].cleaned_data["date"]), "2026-11-01")

    # --- Creating events by wall clock ---

    def form(self, date, start, end, zone="America/New_York"):
        return EventForm(
            {"title": "T", "date": date, "start_time": start, "end_time": end, "timezone": zone,
             "transport_mode": "transit"},
            user=self.admin, workspace=self.workspace,
        )

    def test_form_reads_wall_times_in_the_chosen_zone_after_the_change(self):
        form = self.form("2026-11-01", "03:00", "04:00")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.event.starts_at, datetime(2026, 11, 1, 8, tzinfo=UTC))  # EST
        form = self.form("2026-10-31", "03:00", "04:00")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.event.starts_at, datetime(2026, 10, 31, 7, tzinfo=UTC))  # EDT

    def test_form_overnight_event_across_the_change_ends_at_the_right_instant(self):
        form = EventForm(
            {"title": "T", "date": "2026-10-31", "start_time": "22:00", "end_date": "2026-11-01",
             "end_time": "03:00", "timezone": "America/New_York", "transport_mode": "transit"},
            user=self.admin, workspace=self.workspace,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.event.ends_at.astimezone(UTC) - form.event.starts_at.astimezone(UTC), timedelta(hours=6))

    def test_form_rejects_the_ambiguous_and_the_missing_hour_instead_of_guessing(self):
        # Product choice (open question): 1:30 AM on Nov 1 and 2:30 AM on Mar 8 cannot be entered.
        for date, start, end in (("2026-11-01", "01:30", "03:00"), ("2026-03-08", "02:30", "04:00")):
            with self.subTest(date=date):
                form = self.form(date, start, end)
                self.assertFalse(form.is_valid())
                self.assertIn("start_time", form.errors)
