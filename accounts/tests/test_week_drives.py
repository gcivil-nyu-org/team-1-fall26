"""The parent's week: what each parent drives, and the flags for open rides and
double-bookings.
"""

import re
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.models import Membership, Workspace
from accounts.tests.test_kid_day import MORNING, WEDNESDAY, at, frozen
from events.models import Event


def text(html):
    """The words in some markup that a sighted person reads (visually hidden spans left
    out), tags removed and spaces squeezed.
    """
    html = re.sub(r'<span class="sr-only">.*?</span>', "", html)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html)).strip()


def card(response, name):
    """The markup of one person's row in the week."""
    match = re.search(
        rf'<section class="card"[^>]*aria-label="{name}’s week">(.*?)</section>',
        response.content.decode(),
        re.S,
    )
    assert match, f"no row for {name}"
    return match.group(1)


def chip(row, day):
    """The markup of one day's chip in a row (day is a date)."""
    match = re.search(
        rf'<a class="chip[^"]*" href="[^"]*date={day.isoformat()}">(.*?)</a>',
        row,
        re.S,
    )
    assert match, f"no chip for {day}"
    return match.group(0)


def flags(response):
    match = re.search(
        r'<section class="week-flags".*?</section>', response.content.decode(), re.S
    )
    return match.group(0) if match else None


@override_settings(TIME_ZONE="America/New_York")
class WeekDrivesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.ana = User.objects.create_user(username="ana", first_name="Ana")
        cls.mateo = User.objects.create_user(username="mateo", first_name="Mateo")
        cls.lucia = User.objects.create_user(username="lucia", first_name="Lucia")
        cls.tomas = User.objects.create_user(username="tomas", first_name="Tomas")
        cls.workspace = Workspace.objects.create(name="Reyes", primary_admin=cls.ana)
        for user, role in (
            (cls.mateo, Membership.Role.PARENT),
            (cls.lucia, Membership.Role.KID),
            (cls.tomas, Membership.Role.KID),
        ):
            Membership.objects.create(
                workspace=cls.workspace,
                user=user,
                role=role,
                invited_by=cls.ana,
                accepted_at=MORNING,
            )
        cls.url = reverse("accounts:dashboard")
        cls.day_url = reverse("events:workspace_list", args=[cls.workspace.pk])

    def plan(self, title, starts_at, ends_at, people=(), **fields):
        event = Event.objects.create(
            created_by=self.ana,
            workspace=self.workspace,
            title=title,
            starts_at=starts_at,
            ends_at=ends_at,
            **fields,
        )
        event.participants.set(people)
        return event

    def swim(self):
        """Mateo drives Swim class both ways and takes no part in it."""
        return self.plan(
            "Swim class",
            at(WEDNESDAY, 18),
            at(WEDNESDAY, 19),
            [self.lucia, self.tomas],
            requires_transport=True,
            dropoff_driver=self.mateo,
            pickup_driver=self.mateo,
        )

    def page(self, user, now=MORNING, **query):
        self.client.force_login(user)
        with frozen(now):
            return self.client.get(self.url, query)

    # ---- Part 1: the driver's own row ----

    def test_the_drivers_own_row_says_you_drive_and_not_free(self):
        self.swim()
        response = self.page(self.mateo)
        wednesday = chip(card(response, "Mateo"), WEDNESDAY)
        self.assertIn("You drive", wednesday)
        self.assertIn("Swim class", text(wednesday))
        self.assertIn("both ways", wednesday)
        self.assertNotIn("Free", wednesday)
        self.assertNotIn('class="chip l0', wednesday)
        # The same time element and attribute the plan lines use.
        self.assertRegex(wednesday, r'<time datetime="[^"]+" data-clock="18:00">')
        self.assertRegex(wednesday, r'<time datetime="[^"]+" data-clock="19:00">')
        # A day without a drive is still free.
        self.assertIn("Free", chip(card(response, "Mateo"), WEDNESDAY - timedelta(1)))

    def test_the_other_parent_sees_drives_in_that_parents_row(self):
        self.swim()
        response = self.page(self.ana)
        wednesday = chip(card(response, "Mateo"), WEDNESDAY)
        self.assertIn("Drives", text(wednesday))
        self.assertNotIn("You drive", wednesday)
        self.assertNotIn("Free", wednesday)
        self.assertNotIn("You drive", card(response, "Ana"))

    def test_the_kids_rows_in_a_parents_week_hold_no_drives(self):
        self.swim()
        response = self.page(self.ana)
        for name in ("Lucia", "Tomas"):
            self.assertNotIn("drive-tag", card(response, name))

    def test_a_chip_lists_two_drives_and_counts_the_rest(self):
        for hour in (9, 11, 13):
            self.plan(
                f"Ride {hour}",
                at(WEDNESDAY, hour),
                at(WEDNESDAY, hour, 30),
                [self.lucia],
                requires_transport=True,
                dropoff_driver=self.mateo,
            )
        wednesday = chip(card(self.page(self.mateo), "Mateo"), WEDNESDAY)
        self.assertEqual(wednesday.count('class="drive"'), 2)
        self.assertIn("+1 more drive", wednesday)
        self.assertIn("Ride 9", wednesday)
        self.assertNotIn("Ride 13", wednesday)

    def test_a_drive_comes_before_the_plans_of_the_day(self):
        self.swim()
        self.plan("Gym", at(WEDNESDAY, 7), at(WEDNESDAY, 8), [self.mateo])
        self.plan(
            "Piano lesson",
            at(WEDNESDAY, 16),
            at(WEDNESDAY, 17),
            [self.mateo],
            requires_transport=True,
            dropoff_driver=self.mateo,
        )
        wednesday = chip(card(self.page(self.mateo), "Mateo"), WEDNESDAY)
        words = text(wednesday)
        self.assertLess(words.index("Swim class"), words.index("Gym"))
        self.assertEqual(wednesday.count("You drive"), 1)

    def test_the_kid_dashboard_holds_no_drive_tags_and_no_flags(self):
        self.swim()
        self.plan(
            "Soccer game",
            at(WEDNESDAY, 11),
            at(WEDNESDAY, 11, 30),
            [self.lucia],
            requires_transport=True,
        )
        self.plan("Dentist", at(WEDNESDAY, 13), at(WEDNESDAY, 14), [self.lucia])
        self.plan("Piano", at(WEDNESDAY, 13, 30), at(WEDNESDAY, 15), [self.lucia])
        response = self.page(self.lucia)
        self.assertTemplateUsed(response, "accounts/kid_dashboard.html")
        for words in (
            "You drive",
            "Drives",
            "week-flags",
            "Needs attention",
            "Plans overlap",
        ):
            self.assertNotContains(response, words)
        # The driver summary keeps its plain words for kids.
        self.assertContains(response, "Not assigned")
        self.assertNotContains(response, "ride-open")
        self.assertNotContains(response, "No driver yet")

    # ---- Part 2: the week's flags ----

    def open_rides(self):
        # A ride still ahead on Wednesday and two on Friday; Monday's and this
        # morning's are over; Piano has its drop-off covered.
        self.plan(
            "Soccer practice",
            at(WEDNESDAY - timedelta(2), 16),
            at(WEDNESDAY - timedelta(2), 18),
            [self.tomas],
            requires_transport=True,
        )
        self.plan(
            "School run",
            at(WEDNESDAY, 8),
            at(WEDNESDAY, 9),
            [self.tomas],
            requires_transport=True,
        )
        self.plan(
            "Piano lesson",
            at(WEDNESDAY, 17),
            at(WEDNESDAY, 17, 45),
            [self.lucia],
            requires_transport=True,
            dropoff_driver=self.ana,
        )
        self.plan(
            "Soccer game",
            at(WEDNESDAY + timedelta(2), 10),
            at(WEDNESDAY + timedelta(2), 11, 30),
            [self.tomas],
            requires_transport=True,
        )

    def test_the_flags_count_only_legs_still_ahead(self):
        self.open_rides()
        response = self.page(self.ana)
        banner = flags(response)
        self.assertIn("3 rides still need a driver: Wed 28, Fri 30.", text(banner))
        self.assertRegex(
            banner, r'<h2 id="week-flags-title" class="sr-only">Needs attention'
        )
        self.assertNotIn("aria-live", banner)
        self.assertNotIn("role=", banner)
        for day in (WEDNESDAY, WEDNESDAY + timedelta(2)):
            self.assertIn(
                f'href="{self.day_url}?view=day&amp;date={day.isoformat()}"', banner
            )
        self.assertEqual(banner.count('class="my-day-flag"'), 1)

    def test_one_open_ride_reads_in_the_singular(self):
        self.plan(
            "Piano lesson",
            at(WEDNESDAY, 17),
            at(WEDNESDAY, 17, 45),
            [self.lucia],
            requires_transport=True,
            dropoff_driver=self.ana,
        )
        self.assertIn(
            "1 ride still needs a driver: Wed 28.", text(flags(self.page(self.ana)))
        )

    def test_the_weekend_counts_even_when_its_columns_are_hidden(self):
        self.plan(
            "Birthday party",
            at(WEDNESDAY + timedelta(3), 14),
            at(WEDNESDAY + timedelta(3), 16),
            [self.lucia],
            requires_transport=True,
        )
        self.client.force_login(self.ana)
        session = self.client.session
        session["hide_weekends"] = True
        session.save()
        with frozen(MORNING):
            response = self.client.get(self.url)
        self.assertNotContains(response, "Sat 31</span>")
        self.assertIn("2 rides still need a driver: Sat 31.", text(flags(response)))

    def test_a_past_week_shows_nothing_and_a_future_week_counts_everything(self):
        monday = WEDNESDAY - timedelta(2)
        next_monday = monday + timedelta(7)
        for start in (monday - timedelta(7), next_monday):
            self.plan(
                f"Ride {start}",
                at(start, 16),
                at(start, 17),
                [self.tomas],
                requires_transport=True,
            )
            self.plan("A", at(start, 9), at(start, 10), [self.lucia])
            self.plan("B", at(start, 9, 30), at(start, 10, 30), [self.lucia])
        past = self.page(self.ana, week=(monday - timedelta(7)).isoformat())
        self.assertIsNone(flags(past))
        self.assertNotContains(past, "Needs attention")
        future = self.page(self.ana, week=next_monday.isoformat())
        banner = text(flags(future))
        self.assertIn("2 rides still need a driver: Mon 2.", banner)
        self.assertIn("Lucia is double-booked on Mon 2.", banner)

    def test_no_flags_section_when_nothing_is_open(self):
        self.swim()
        self.plan("Dentist", at(WEDNESDAY, 13), at(WEDNESDAY, 14), [self.lucia])
        self.plan("Piano", at(WEDNESDAY, 14), at(WEDNESDAY, 15), [self.lucia])
        response = self.page(self.ana)
        self.assertIsNone(flags(response))
        self.assertNotContains(response, "week-flags")
        self.assertNotContains(response, "Needs attention")

    def test_the_double_booked_sentence_names_the_person_and_links_the_day(self):
        self.swim()
        self.plan(
            "Piano make-up lesson",
            at(WEDNESDAY, 18, 30),
            at(WEDNESDAY, 19, 15),
            [self.lucia],
        )
        banner = flags(self.page(self.ana))
        self.assertRegex(
            banner,
            r'<span translate="no">Lucia</span> is double-booked on '
            rf'<a href="[^"]*date={WEDNESDAY.isoformat()}">Wed 28'
            r'<span class="sr-only"> day view</span></a>\.',
        )
        self.assertNotIn("Tomas", banner)  # Tomas only has the swim class
        self.assertNotIn("Needs a driver", banner)

    def test_several_days_are_joined_with_and_and_the_rest_are_skipped(self):
        for offset in (0, 1, 2):
            day = WEDNESDAY + timedelta(offset)
            self.plan("Standup", at(day, 11), at(day, 12), [self.ana])
            self.plan("Dentist", at(day, 11, 30), at(day, 12, 30), [self.ana])
        # Over already (Monday), only touching (Tuesday) and one whole-family plan.
        for day, first, second in (
            (WEDNESDAY - timedelta(2), (9, 0, 10, 0), (9, 30, 10, 30)),
            (WEDNESDAY - timedelta(1), (11, 0, 12, 0), (12, 0, 13, 0)),
        ):
            self.plan("One", at(day, first[0]), at(day, first[2]), [self.mateo])
            self.plan("Two", at(day, second[0]), at(day, second[2]), [self.mateo])
        self.plan("Lunch", at(WEDNESDAY, 12), at(WEDNESDAY, 14))
        banner = text(flags(self.page(self.ana)))
        self.assertIn("Ana is double-booked on Wed 28, Thu 29 and Fri 30.", banner)
        self.assertNotIn("Mateo", banner)

    def test_a_double_booking_whose_later_plan_has_ended_is_not_flagged(self):
        self.plan("One", at(WEDNESDAY, 8), at(WEDNESDAY, 9), [self.ana])
        self.plan("Two", at(WEDNESDAY, 8, 30), at(WEDNESDAY, 9, 30), [self.ana])
        self.assertIsNone(flags(self.page(self.ana)))
        self.assertIsNotNone(flags(self.page(self.ana, now=at(WEDNESDAY, 9))))

    def test_flags_show_to_parents_only(self):
        self.open_rides()
        self.assertIsNotNone(flags(self.page(self.mateo)))
        self.assertIsNone(flags(self.page(self.tomas)))

    def test_an_open_leg_is_a_badge_that_holds_no_link(self):
        self.plan(
            "Piano lesson",
            at(WEDNESDAY, 17),
            at(WEDNESDAY, 17, 45),
            [self.lucia],
            requires_transport=True,
            dropoff_driver=self.ana,
        )
        response = self.page(self.ana)
        wednesday = chip(card(response, "Lucia"), WEDNESDAY)
        self.assertIn(
            'Drop-off: <span translate="no">Ana</span> · Pickup: '
            '<span class="ride-open">No driver yet</span>',
            wednesday,
        )
        self.assertNotIn("Not assigned", wednesday)
        self.assertEqual(wednesday.count("<a "), 1)  # only the chip's own link
        self.assertNotIn("<button", wednesday)

    def test_a_midnight_pickup_reads_midnight_in_the_chip(self):
        self.plan(
            "Late movie",
            at(WEDNESDAY, 22),
            at(WEDNESDAY + timedelta(1), 0),
            [self.lucia],
            requires_transport=True,
            dropoff_driver=self.ana,
            pickup_driver=self.mateo,
        )
        chip_text = text(chip(card(self.page(self.mateo), "Mateo"), WEDNESDAY))
        self.assertIn("Late movie pickup midnight", chip_text)
        self.assertNotIn("00:00", chip_text)

    def test_the_drive_tag_and_title_are_separate_words(self):
        self.swim()
        wednesday = chip(card(self.page(self.mateo), "Mateo"), WEDNESDAY)
        self.assertIn("You drive</span> Swim class <span", wednesday)

    def test_each_flag_link_says_it_opens_the_day(self):
        self.open_rides()
        self.assertIn(" day view</span></a>", flags(self.page(self.ana)))

    def test_the_chip_says_when_plans_overlap(self):
        self.plan("Dentist", at(WEDNESDAY, 13), at(WEDNESDAY, 14), [self.lucia])
        self.plan("Piano", at(WEDNESDAY, 13, 30), at(WEDNESDAY, 15), [self.lucia])
        self.plan("Math", at(WEDNESDAY, 13), at(WEDNESDAY, 14), [self.tomas])
        response = self.page(self.ana)
        self.assertIn("Plans overlap", chip(card(response, "Lucia"), WEDNESDAY))
        self.assertNotIn("Plans overlap", chip(card(response, "Tomas"), WEDNESDAY))

    # ---- Queries ----

    def test_query_count_does_not_grow_with_events(self):
        def count(sets):
            Event.objects.all().delete()
            for index in range(sets):
                day = WEDNESDAY + timedelta(1 + index % 3)
                hour = 8 + index
                self.plan(
                    f"Swim {index}",
                    at(day, hour),
                    at(day, hour, 45),
                    [self.lucia, self.tomas],
                    requires_transport=True,
                    dropoff_driver=self.mateo,
                    pickup_driver=self.ana,
                )
                self.plan(
                    f"Soccer {index}",
                    at(day, hour, 15),
                    at(day, hour, 50),
                    [self.lucia],
                    requires_transport=True,
                )
                self.plan(f"Gym {index}", at(day, 20), at(day, 21), [self.mateo])
            self.client.force_login(self.ana)
            with frozen(MORNING), CaptureQueriesContext(connection) as queries:
                response = self.client.get(self.url)
            self.assertIsNotNone(flags(response))
            return len(queries)

        self.assertEqual(count(2), count(6))
