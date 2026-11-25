"""The week's flags worked out from plain events: which rides still need a driver and
who is double-booked.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from accounts.models import Membership, Workspace

from ..flags import build_week_flags
from ..models import Event
from ..timeline import family_members

NEW_YORK = ZoneInfo("America/New_York")
MONDAY = date(2026, 10, 26)
SUNDAY = date(2026, 11, 1)


def at(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=NEW_YORK)


class WeekFlagsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.ana = User.objects.create_user(username="ana", first_name="Ana")
        cls.lucia = User.objects.create_user(username="lucia", first_name="Lucia")
        cls.workspace = Workspace.objects.create(name="Reyes", primary_admin=cls.ana)
        Membership.objects.create(
            workspace=cls.workspace,
            user=cls.lucia,
            role=Membership.Role.KID,
            invited_by=cls.ana,
            accepted_at=timezone.now(),
        )

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

    def flags(self, now, week_start=MONDAY):
        events = Event.objects.prefetch_related("participants").order_by("starts_at")
        return build_week_flags(
            events, family_members([self.workspace]), week_start, NEW_YORK, now
        )

    def test_nothing_to_say_gives_none(self):
        self.assertIsNone(self.flags(at(MONDAY, 8)))

    def test_a_pickup_at_midnight_belongs_to_the_day_that_ends_there(self):
        self.plan(
            "Sleepover",
            at(SUNDAY, 20),
            at(SUNDAY + timedelta(days=1), 0),
            [self.lucia],
            requires_transport=True,
            dropoff_driver=self.ana,
        )
        this_week = self.flags(at(MONDAY, 8))
        self.assertEqual(this_week["open_rides"], 1)
        self.assertEqual(this_week["open_days"], [SUNDAY])
        self.assertIsNone(self.flags(at(MONDAY, 8), MONDAY + timedelta(days=7)))

    def test_legs_already_past_and_ridden_legs_do_not_count(self):
        self.plan(
            "Soccer",
            at(MONDAY, 9),
            at(MONDAY, 11),
            [self.lucia],
            requires_transport=True,
            dropoff_driver=self.ana,
        )
        self.assertIsNone(self.flags(at(MONDAY, 12)))
        flags = self.flags(at(MONDAY, 10))
        self.assertEqual((flags["open_rides"], flags["open_days"]), (1, [MONDAY]))

    def test_an_all_day_plan_is_not_a_double_booking(self):
        self.plan(
            "Trip", at(MONDAY, 0), at(MONDAY + timedelta(days=1), 0), [self.lucia]
        )
        self.plan("Dentist", at(MONDAY, 9), at(MONDAY, 10), [self.lucia])
        self.assertIsNone(self.flags(at(MONDAY, 8)))

    def test_a_clash_lists_each_day_it_happens(self):
        for offset in (0, 2):
            day = MONDAY + timedelta(days=offset)
            self.plan("One", at(day, 9), at(day, 10), [self.lucia])
            self.plan("Two", at(day, 9, 30), at(day, 10, 30), [self.lucia])
        [clash] = self.flags(at(MONDAY, 8))["double_booked"]
        self.assertEqual(clash["member"]["user"], self.lucia)
        self.assertEqual(clash["days"], [MONDAY, MONDAY + timedelta(days=2)])
