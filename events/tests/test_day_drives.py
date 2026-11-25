"""The day view: who drives, and which plans book one person twice."""

import re
from datetime import date, datetime, timedelta
from unittest import mock

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from syncmily.limits import RENDER

from ..day import lane_note, vertical_day
from ..models import Event
from ..timeline import build_day_timeline, family_members
from .test_paint_pages import NEW_YORK, FamilyMixin, at

THURSDAY = date(2026, 10, 29)


def text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html)).strip()


@override_settings(TIME_ZONE="America/New_York")
class DayDrivesTests(FamilyMixin, TestCase):
    def setUp(self):
        self.url = reverse("events:workspace_list", args=[self.workspace.pk])

    def ride(self, title, starts, minutes, people=(), **drivers):
        return self.plan(
            title, starts, minutes, people, requires_transport=True, **drivers
        )

    def swim(self):
        """Mateo drives Swim class both ways and takes no part in it."""
        return self.ride(
            "Swim class",
            at(29, 18),
            60,
            [self.lucia],
            dropoff_driver=self.mateo,
            pickup_driver=self.mateo,
        )

    def page(self, user=None):
        self.client.force_login(user or self.ana)
        response = self.client.get(
            self.url,
            {
                "view": "day",
                "date": THURSDAY.isoformat(),
                "timezone": "America/New_York",
            },
        )
        self.assertEqual(response.status_code, 200)
        return response

    def lane(self, response, name):
        """The markup of one member's lane."""
        for part in response.content.decode().split('<section class="lane')[1:]:
            if f'translate="no">{name}</span>' in part:
                return part
        self.fail(f"no lane for {name}")

    def test_lane_note_words(self):
        self.assertEqual(lane_note(0, []), "Free")
        self.assertEqual(lane_note(1, []), "1 plan")
        self.assertEqual(lane_note(3, []), "3 plans")
        self.assertEqual(lane_note(0, [1]), "Drives 1 ride")
        self.assertEqual(lane_note(0, [1, 2]), "Drives 2 rides")
        self.assertEqual(lane_note(2, [1]), "2 plans, drives 1 ride")

    def test_a_drive_only_lane_names_the_ride_instead_of_free(self):
        self.swim()
        response = self.page()
        mateo = self.lane(response, "Mateo")
        self.assertIn('<small class="has-drives">Drives 1 ride</small>', mateo)
        self.assertNotIn("Free", mateo)
        self.assertNotIn("Nothing planned", mateo)
        self.assertIn(
            "Drives Swim class: drop-off 6:00 PM, pickup 7:00 PM", text(mateo)
        )
        self.assertRegex(mateo, r'<time data-clock="18:00">6:00 PM</time>')
        # Ana drives nothing and has nothing planned, so she is still free.
        ana = self.lane(response, "Ana")
        self.assertIn(">Free</small>", ana)
        self.assertIn("Nothing planned", ana)
        self.assertContains(response, 'class="daygrid has-drives"')

    def test_a_lane_with_plans_and_a_ride_reads_both(self):
        self.swim()
        self.plan("Gym", at(29, 7), 60, [self.mateo])
        self.plan("Carpool", at(29, 12), 60, [self.mateo])
        mateo = self.lane(self.page(), "Mateo")
        self.assertIn("2 plans, drives 1 ride", mateo)
        self.assertNotIn("Nothing planned", mateo)

    def test_a_day_without_rides_keeps_its_old_lane_text(self):
        self.plan("Gym", at(29, 7), 60, [self.mateo])
        response = self.page()
        self.assertNotContains(response, "has-drives")
        self.assertIn(">1 plan</small>", self.lane(response, "Mateo"))
        self.assertIn(">Free</small>", self.lane(response, "Lucía"))

    def test_the_free_note_leaves_out_a_driver(self):
        day = self.day_of(
            self.swim(), self.plan("Book club", at(29, 19), 90, [self.ana])
        )
        self.assertEqual(day["free_note"], "")
        calm = self.day_of(self.plan("Book club", at(29, 19), 90, [self.ana]))
        self.assertEqual(calm["free_note"], "Mateo and Lucía have nothing planned.")

    def day_of(self, *events):
        start = datetime.combine(THURSDAY, datetime.min.time(), tzinfo=NEW_YORK)
        timeline = build_day_timeline(
            Event.objects.filter(pk__in=[e.pk for e in events]).prefetch_related(
                "participants"
            ),
            family_members([self.workspace]),
            start,
            start + timedelta(days=1),
            NEW_YORK,
            timezone.now(),
        )
        return vertical_day(timeline, NEW_YORK)

    def test_both_overlapping_blocks_carry_a_cue_that_is_not_colour(self):
        self.plan("Carpool", at(29, 17), 60, [self.mateo])
        self.plan("Gym", at(29, 17, 30), 60, [self.mateo])
        self.plan("Dentist", at(29, 10), 30, [self.mateo])
        response = self.page()
        mateo = self.lane(response, "Mateo")
        self.assertEqual(mateo.count('class="plan overlap"'), 2)
        self.assertIn(", overlaps Gym", text(mateo))
        self.assertIn(", overlaps Carpool", text(mateo))
        self.assertNotIn("overlaps Dentist", text(mateo))
        self.assertEqual(self.lane(response, "Ana").count("overlap"), 0)
        # The summary line takes the flag style.
        self.assertContains(
            response,
            '<span class="span my-day-flag">Three plans, Mateo double-booked</span>',
        )

    def test_a_plan_names_three_of_the_plans_it_overlaps_and_counts_the_rest(self):
        day = self.day_of(
            *[self.plan(f"Plan {n}", at(29, 17), 60, [self.mateo]) for n in range(5)]
        )
        mateo = next(lane for lane in day["lanes"] if lane["user"] == self.mateo)
        first = next(s for s in mateo["slots"] if s["event"].title == "Plan 0")
        self.assertEqual(first["overlaps"], ["Plan 1", "Plan 2", "Plan 3", "1 more"])

    def test_plans_that_only_touch_carry_no_cue_and_the_summary_stays_plain(self):
        self.plan("Carpool", at(29, 17), 60, [self.mateo])
        self.plan("Gym", at(29, 18), 60, [self.mateo])
        response = self.page()
        self.assertNotContains(response, 'class="plan overlap"')
        self.assertNotContains(response, "my-day-flag")

    def test_the_open_leg_is_a_badge_for_parents_and_plain_words_for_kids(self):
        self.ride(
            "Soccer game", at(29, 10), 90, [self.lucia], dropoff_driver=self.mateo
        )
        # The badge marks a leg that is still ahead, so the day has not started yet.
        with mock.patch("django.utils.timezone.now", return_value=at(29, 8)):
            parent = self.lane(self.page(), "Lucía")
            kid = self.lane(self.page(self.lucia), "Lucía")
        self.assertIn('<span class="ride-open">No driver yet</span>', parent)
        self.assertNotIn("Not assigned", parent)
        self.assertIn("Not assigned", kid)
        self.assertNotIn("ride-open", kid)

    def test_a_lane_lists_at_most_30_ride_notes_and_counts_the_rest(self):
        for number in range(RENDER.plans_per_lane + 2):
            self.ride(
                f"Ride {number}",
                at(29, 6) + timedelta(minutes=20 * number),
                15,
                [self.lucia],
                dropoff_driver=self.mateo,
            )
        mateo = self.lane(self.page(), "Mateo")
        self.assertEqual(mateo.count('class="drive-note"'), RENDER.plans_per_lane)
        self.assertIn(">+2 more</a>", mateo)
        self.assertIn("Drives 32 rides", mateo)

    def test_kids_read_the_day_as_before(self):
        """D1: no ride notes, no overlap words, no taller headers for a kid."""
        self.swim()
        self.plan("Carpool", at(29, 17), 60, [self.lucia])
        self.plan("Gym", at(29, 17, 30), 60, [self.lucia])
        response = self.page(self.lucia)
        mateo = self.lane(response, "Mateo")
        self.assertIn(">Free</small>", mateo)
        self.assertIn("Nothing planned", mateo)
        self.assertNotIn("Drives", text(mateo))
        self.assertNotIn("drive-note", mateo)
        lucia = self.lane(response, "Lucía")
        self.assertIn(">3 plans</small>", lucia)
        self.assertNotIn("overlap", lucia)
        self.assertNotContains(response, "has-drives")
        self.assertNotContains(response, "my-day-flag")
        # The words the page already had for a double-booking stay.
        self.assertContains(response, "Lucía double-booked")

    def test_the_headline_counts_this_days_rides_that_still_need_a_driver(self):
        self.ride("Swim class", at(29, 18), 60, [self.lucia], pickup_driver=self.mateo)
        for now, expected in (
            (at(29, 8), "One plan, 1 ride still needs a driver"),
            # The drop-off has passed, so nothing is left to drive.
            (at(29, 18, 30), "One plan"),
        ):
            with (
                self.subTest(now=now),
                mock.patch("django.utils.timezone.now", return_value=now),
            ):
                self.assertContains(self.page(), f"{expected}</span>")
                self.assertNotContains(self.page(self.lucia), "a driver</span>")

    def test_the_headline_leaves_another_days_leg_to_that_day(self):
        self.ride("Sleepover", at(29, 18), 24 * 60, [self.lucia])
        with mock.patch("django.utils.timezone.now", return_value=at(28, 8)):
            self.assertContains(self.page(), "One plan, 1 ride still needs a driver")

    def test_a_kid_view_leaves_the_free_note_as_before(self):
        self.plan("Book club", at(29, 19), 90, [self.ana])
        start = datetime.combine(THURSDAY, datetime.min.time(), tzinfo=NEW_YORK)
        timeline = build_day_timeline(
            Event.objects.prefetch_related("participants"),
            family_members([self.workspace]),
            start,
            start + timedelta(days=1),
            NEW_YORK,
            timezone.now(),
        )
        self.swim()
        self.assertEqual(
            vertical_day(timeline, NEW_YORK, parent_view=False)["free_note"],
            "Mateo and Lucía have nothing planned.",
        )

    def test_a_pickup_at_midnight_reads_midnight_not_12_am(self):
        self.ride(
            "Late movie",
            at(29, 22),
            120,
            [self.lucia],
            dropoff_driver=self.ana,
            pickup_driver=self.mateo,
        )
        mateo = self.lane(self.page(), "Mateo")
        self.assertIn("Drives Late movie: pickup midnight (end of day)", text(mateo))
        self.assertNotIn("12:00 AM", mateo)
        ana = self.lane(self.page(), "Ana")
        self.assertIn("drop-off 10:00 PM", text(ana))

    def test_ride_notes_sit_in_their_own_row_above_the_hours(self):
        self.swim()
        self.plan("Gym", at(29, 7), 60, [self.mateo])
        mateo = self.lane(self.page(), "Mateo")
        notes, track = mateo.index('<ul class="drive-notes">'), mateo.index(
            '<div class="track"'
        )
        self.assertLess(notes, track)
        self.assertNotIn("drive-note", mateo[track:])

    def test_query_count_does_not_grow_with_events(self):
        def count(sets):
            Event.objects.all().delete()
            for index in range(sets):
                self.ride(
                    f"Swim {index}",
                    at(29, 8 + index),
                    30,
                    [self.lucia],
                    dropoff_driver=self.mateo,
                    pickup_driver=self.ana,
                )
                self.plan(f"Gym {index}", at(29, 8 + index, 15), 30, [self.mateo])
                self.plan(f"Carpool {index}", at(29, 8 + index, 20), 30, [self.mateo])
            self.client.force_login(self.ana)
            with CaptureQueriesContext(connection) as queries:
                self.client.get(self.url, {"view": "day", "date": "2026-10-29"})
            return len(queries)

        self.assertEqual(count(1), count(4))

    def test_summary_names_the_clash(self):
        self.plan("Carpool", at(29, 17), 60, [self.mateo])
        self.plan("Gym", at(29, 17, 30), 60, [self.mateo])
        day = self.day_of(*Event.objects.all())
        self.assertTrue(day["clash"])
        self.assertEqual(day["summary"], "Two plans, Mateo double-booked")
