from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone as django_timezone

from accounts.models import Membership, Workspace

from .conflicts import (
    UTC, ScheduleItem, conflicting_keys, find_conflicts, group_by_pair, is_all_day, overlap_rule,
)
from .models import Event

NEW_YORK = ZoneInfo("America/New_York")
TOKYO = ZoneInfo("Asia/Tokyo")
KID, PARENT = 1, 2


def at(day, hour, minute=0, tz=NEW_YORK):
    return datetime(2026, 10, day, hour, minute, tzinfo=tz)


def fall_back(hour, minute, fold=0):
    """A New York time on 2026-11-01, when clocks go back at 2 AM."""
    return datetime(2026, 11, 1, hour, minute, fold=fold, tzinfo=NEW_YORK)


def item(key, starts_at, ends_at, members=(KID,), household="family", **fields):
    return ScheduleItem(key, starts_at, ends_at, frozenset(members), household=household, **fields)


def pairs(conflicts):
    return [(conflict.member_id, conflict.first, conflict.second) for conflict in conflicts]


class OverlapTests(SimpleTestCase):
    def test_overlapping_events_for_the_same_member_conflict(self):
        conflicts = find_conflicts([
            item("soccer", at(20, 15), at(20, 17)),
            item("piano", at(20, 16), at(20, 18)),
        ])
        self.assertEqual(pairs(conflicts), [(KID, "soccer", "piano")])
        self.assertEqual((conflicts[0].starts_at, conflicts[0].ends_at), (at(20, 16), at(20, 17)))
        self.assertEqual(conflicts[0].rule, "overlap")

    def test_touching_edges_do_not_conflict(self):
        self.assertEqual(find_conflicts([
            item("soccer", at(20, 15), at(20, 16)),
            item("piano", at(20, 16), at(20, 17)),
        ]), [])

    def test_one_minute_of_overlap_conflicts(self):
        self.assertEqual(len(find_conflicts([
            item("soccer", at(20, 15), at(20, 16, 1)),
            item("piano", at(20, 16), at(20, 17)),
        ])), 1)

    def test_different_members_do_not_conflict(self):
        self.assertEqual(find_conflicts([
            item("soccer", at(20, 15), at(20, 17), members=[KID]),
            item("meeting", at(20, 15), at(20, 17), members=[PARENT]),
        ]), [])

    def test_events_without_members_do_not_conflict(self):
        self.assertEqual(find_conflicts([
            item("dinner", at(20, 18), at(20, 20), members=[]),
            item("movie", at(20, 19), at(20, 21), members=[]),
        ]), [])

    def test_each_shared_member_is_reported_and_grouped_by_pair(self):
        conflicts = find_conflicts([
            item("soccer", at(20, 15), at(20, 17), members=[KID, PARENT]),
            item("piano", at(20, 16), at(20, 18), members=[KID, PARENT]),
        ])
        self.assertCountEqual(pairs(conflicts), [(KID, "soccer", "piano"), (PARENT, "soccer", "piano")])
        grouped = group_by_pair(conflicts)
        self.assertEqual(len(grouped), 1)
        self.assertCountEqual(grouped[0]["member_ids"], [KID, PARENT])

    def test_three_way_overlap_reports_every_pair(self):
        conflicts = find_conflicts([
            item("a", at(20, 9), at(20, 12)),
            item("b", at(20, 10), at(20, 11)),
            item("c", at(20, 10, 30), at(20, 13)),
        ])
        self.assertCountEqual(pairs(conflicts), [(KID, "a", "b"), (KID, "a", "c"), (KID, "b", "c")])
        self.assertEqual(conflicting_keys(conflicts)["a"], {"b", "c"})

    def test_contained_event_conflicts_with_its_container(self):
        conflicts = find_conflicts([
            item("b", at(20, 10), at(20, 11)),
            item("a", at(20, 9), at(20, 17)),
        ])
        self.assertEqual(pairs(conflicts), [(KID, "a", "b")])
        self.assertEqual((conflicts[0].starts_at, conflicts[0].ends_at), (at(20, 10), at(20, 11)))

    def test_identical_times_conflict(self):
        self.assertEqual(len(find_conflicts([
            item("a", at(20, 9), at(20, 10)),
            item("b", at(20, 9), at(20, 10)),
        ])), 1)

    def test_events_across_midnight(self):
        conflicts = find_conflicts([
            item("sleepover", at(20, 23), at(21, 1)),
            item("late-film", at(21, 0, 30), at(21, 2)),
            item("after", at(21, 1), at(21, 1, 15)),
        ])
        self.assertCountEqual(pairs(conflicts), [(KID, "sleepover", "late-film"), (KID, "late-film", "after")])

    def test_compares_instants_not_wall_times_across_timezones(self):
        # 9 AM in Tokyo on Oct 21 is 8 PM on Oct 20 in New York.
        self.assertEqual(len(find_conflicts([
            item("call", at(21, 9, tz=TOKYO), at(21, 10, tz=TOKYO)),
            item("dinner", at(20, 19, 30), at(20, 21)),
        ])), 1)

    def test_dst_fall_back_repeated_hour_is_not_a_false_conflict(self):
        # On 2026-11-01 New York repeats 1-2 AM: fold=0 is EDT, fold=1 is EST an hour later.
        self.assertEqual(find_conflicts([
            item("first-pass", fall_back(1, 0), fall_back(1, 45)),
            item("second-pass", fall_back(1, 15, fold=1), fall_back(1, 45, fold=1)),
        ]), [])

    def test_dst_fall_back_overlap_hidden_by_wall_times_is_found(self):
        # 1:00 EDT to 1:05 EST lasts 65 minutes and contains 1:30-1:50 EDT.
        conflicts = find_conflicts([
            item("long", fall_back(1, 0), fall_back(1, 5, fold=1)),
            item("short", fall_back(1, 30), fall_back(1, 50)),
        ])
        self.assertEqual(pairs(conflicts), [(KID, "long", "short")])

    def test_all_day_events_do_not_conflict(self):
        self.assertEqual(find_conflicts([
            item("holiday", at(20, 0), at(21, 0), all_day=True),
            item("birthday", at(20, 0), at(21, 0), all_day=True),
            item("dentist", at(20, 9), at(20, 10)),
        ]), [])

    def test_cancelled_events_do_not_conflict(self):
        self.assertEqual(find_conflicts([
            item("soccer", at(20, 15), at(20, 17), cancelled=True),
            item("piano", at(20, 16), at(20, 18)),
        ]), [])

    def test_households_are_never_compared(self):
        self.assertEqual(find_conflicts([
            item("ours", at(20, 15), at(20, 17), household="ours"),
            item("theirs", at(20, 16), at(20, 18), household="theirs"),
        ]), [])

    def test_results_are_in_time_order(self):
        conflicts = find_conflicts([
            item("late-a", at(22, 9), at(22, 11)),
            item("late-b", at(22, 10), at(22, 12)),
            item("early-a", at(20, 9), at(20, 11)),
            item("early-b", at(20, 10), at(20, 12)),
        ])
        self.assertEqual([conflict.first for conflict in conflicts], ["early-a", "late-a"])

    def test_rules_get_each_members_items_in_time_order(self):
        calls = []

        def travel_rule(member_id, items):
            calls.append((member_id, [entry.key for entry in items], [entry.location for entry in items]))
            return []

        find_conflicts([
            item("piano", at(20, 17), at(20, 18), location="Music school"),
            item("soccer", at(20, 15), at(20, 16), location="Park"),
        ], rules=(overlap_rule, travel_rule))
        self.assertEqual(calls, [(KID, ["soccer", "piano"], ["Park", "Music school"])])


class AllDayTests(SimpleTestCase):
    def test_midnight_to_midnight_is_all_day(self):
        self.assertTrue(is_all_day(at(20, 0), at(21, 0), "America/New_York"))
        self.assertTrue(is_all_day(at(20, 0), at(23, 0), "America/New_York"))

    def test_dst_day_is_still_all_day(self):
        # Nov 1 2026 has 25 hours in New York.
        start = datetime(2026, 11, 1, tzinfo=NEW_YORK)
        end = datetime(2026, 11, 2, tzinfo=NEW_YORK)
        self.assertEqual(end.astimezone(UTC) - start.astimezone(UTC), timedelta(hours=25))
        self.assertTrue(is_all_day(start, end, "America/New_York"))

    def test_timed_events_are_not_all_day(self):
        self.assertFalse(is_all_day(at(20, 0), at(20, 12), "America/New_York"))
        self.assertFalse(is_all_day(at(20, 9), at(21, 9), "America/New_York"))

    def test_uses_the_events_own_timezone(self):
        # Midnight in Tokyo is not midnight in New York.
        start = datetime(2026, 10, 20, tzinfo=TOKYO)
        self.assertTrue(is_all_day(start, start + timedelta(days=1), "Asia/Tokyo"))
        self.assertFalse(is_all_day(start, start + timedelta(days=1), "America/New_York"))

    def test_invalid_timezone_is_not_all_day(self):
        self.assertFalse(is_all_day(at(20, 0), at(21, 0), "Not/AZone"))


class ConflictViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.parent = User.objects.create_user(username="PrimaryParent")
        cls.kid = User.objects.create_user(username="SharedKid")
        cls.sibling = User.objects.create_user(username="Sibling")
        cls.other_parent = User.objects.create_user(username="OtherFamilyParent")
        cls.workspace = Workspace.objects.create(name="Our Family", primary_admin=cls.parent)
        cls.other_workspace = Workspace.objects.create(name="Other Family", primary_admin=cls.other_parent)
        for user in (cls.kid, cls.sibling):
            Membership.objects.create(
                workspace=cls.workspace, user=user, role=Membership.Role.KID,
                invited_by=cls.parent, accepted_at=django_timezone.now(),
            )

    def setUp(self):
        self.client.force_login(self.parent)
        self.list_url = reverse("events:workspace_list", args=[self.workspace.pk])
        self.create_url = reverse("events:workspace_create", args=[self.workspace.pk])
        self.start = (django_timezone.now() + timedelta(days=2)).astimezone(NEW_YORK).replace(
            hour=15, minute=0, second=0, microsecond=0,
        )
        self.date = self.start.date().isoformat()

    def event(self, title, starts_at, ends_at, participants, workspace=None, created_by=None):
        event = Event.objects.create(
            title=title, starts_at=starts_at, ends_at=ends_at, timezone_name="America/New_York",
            workspace=workspace or self.workspace, created_by=created_by or self.parent,
        )
        event.participants.set(participants)
        return event

    def clash(self):
        soccer = self.event("Soccer", self.start, self.start + timedelta(hours=2), [self.kid])
        piano = self.event("Piano", self.start + timedelta(hours=1), self.start + timedelta(hours=3), [self.kid])
        return soccer, piano

    def schedule(self, view, **params):
        return self.client.get(self.list_url, {"view": view, "date": self.date, "timezone": "America/New_York", **params})

    def test_day_view_lists_conflicts_and_marks_events(self):
        soccer, piano = self.clash()
        response = self.schedule("day")
        [conflict] = response.context["conflicts"]
        self.assertEqual((conflict["first"], conflict["second"], conflict["members"]), (soccer, piano, [self.kid]))
        self.assertContains(response, "1 scheduling conflict")
        self.assertContains(response, "timeline-event has-conflict", count=2)
        self.assertContains(response, reverse("events:detail", args=[piano.pk]))

    def test_day_view_layout_is_unchanged_by_conflicts(self):
        self.clash()
        timeline = self.schedule("day").context["timeline"]
        kid_lane = next(lane for lane in timeline["lanes"] if lane["user"] == self.kid)
        # Both events stay in the kid's lane at their own times, drawn over each other as before.
        self.assertEqual([item["event"].title for item in kid_lane["items"]], ["Soccer", "Piano"])
        first, second = kid_lane["items"]
        self.assertAlmostEqual(second["left"] - first["left"], 100 / timeline["hour_count"], places=2)
        self.assertEqual(first["width"], second["width"])

    def test_week_view_badges_each_conflicting_event(self):
        soccer, piano = self.clash()
        self.event("Homework", self.start + timedelta(hours=4), self.start + timedelta(hours=5), [self.kid])
        response = self.schedule("week")
        self.assertContains(response, "1 scheduling conflict")
        self.assertContains(response, 'class="conflict-badge"', count=2)
        page = {event.title: event for event in response.context["page"]}
        self.assertEqual(page["Soccer"].conflicts_with, [piano])
        self.assertEqual(page["Homework"].conflicts_with, [])

    def test_upcoming_view_finds_conflicts(self):
        self.clash()
        response = self.schedule("upcoming")
        self.assertContains(response, "1 scheduling conflict")
        self.assertContains(response, 'class="conflict-badge"', count=2)

    def test_no_warning_without_conflicts(self):
        self.event("Soccer", self.start, self.start + timedelta(hours=1), [self.kid])
        self.event("Piano", self.start + timedelta(hours=1), self.start + timedelta(hours=2), [self.kid])
        self.event("Dentist", self.start, self.start + timedelta(hours=1), [self.sibling])
        for view in ("day", "week", "upcoming"):
            with self.subTest(view=view):
                response = self.schedule(view)
                self.assertEqual(response.context["conflicts"], [])
                self.assertNotContains(response, "scheduling conflict")

    def test_deleted_event_no_longer_conflicts(self):
        soccer, piano = self.clash()
        piano.delete()
        self.assertEqual(self.schedule("day").context["conflicts"], [])
        response = self.client.get(reverse("events:detail", args=[soccer.pk]))
        self.assertEqual(response.context["event_conflicts"], [])

    def test_all_day_event_does_not_conflict_on_the_schedule(self):
        day = self.start.replace(hour=0)
        self.event("Holiday", day, day + timedelta(days=1), [self.kid])
        self.event("Soccer", self.start, self.start + timedelta(hours=1), [self.kid])
        self.assertEqual(self.schedule("day").context["conflicts"], [])

    def test_detail_shows_the_events_causing_the_conflict(self):
        soccer, piano = self.clash()
        response = self.client.get(reverse("events:detail", args=[soccer.pk]))
        self.assertEqual(response.context["event_conflicts"], [{"event": piano, "members": [self.kid]}])
        self.assertContains(response, "SCHEDULING CONFLICT")
        self.assertContains(response, reverse("events:detail", args=[piano.pk]))

    def test_create_preview_warns_but_still_saves(self):
        soccer, _ = self.clash()
        data = {
            "title": "Dentist", "date": self.date, "start_time": "15:30", "end_time": "16:00",
            "timezone": "America/New_York", "participants": [self.kid.pk, self.sibling.pk], "location": "",
        }
        preview = self.client.post(self.create_url, {**data, "action": "preview"})
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(
            [(entry["event"].title, entry["members"]) for entry in preview.context["event_conflicts"]],
            [("Soccer", [self.kid])],
        )
        self.assertContains(preview, "This overlaps with other plans")
        token = self.client.get(self.create_url).context["form"]["submission_token"].value()
        saved =self.client.post(self.create_url, {**data, "action": "save", "submission_token": token})
        self.assertEqual(saved.status_code, 302)
        self.assertTrue(Event.objects.filter(title="Dentist").exists())

    def test_create_preview_without_conflict_has_no_warning(self):
        self.clash()
        preview = self.client.post(self.create_url, {
            "title": "Dentist", "date": self.date, "start_time": "15:30", "end_time": "16:00",
            "timezone": "America/New_York", "participants": [self.sibling.pk], "action": "preview",
        })
        self.assertEqual(preview.context["event_conflicts"], [])
        self.assertNotContains(preview, "This overlaps with other plans")

    def test_other_households_events_never_appear_as_conflicts(self):
        soccer = self.event("Soccer", self.start, self.start + timedelta(hours=2), [self.kid])
        # Even if another household's event somehow lists our kid, it must stay invisible here.
        secret = self.event(
            "Other household plan", self.start, self.start + timedelta(hours=2), [self.kid],
            workspace=self.other_workspace, created_by=self.other_parent,
        )
        for url in (self.list_url, reverse("events:list")):
            for view in ("day", "week", "upcoming"):
                with self.subTest(url=url, view=view):
                    response = self.client.get(url, {"view": view, "date": self.date, "timezone": "America/New_York"})
                    self.assertEqual(response.context["conflicts"], [])
                    self.assertNotContains(response, secret.title)
        detail = self.client.get(reverse("events:detail", args=[soccer.pk]))
        self.assertEqual(detail.context["event_conflicts"], [])
        self.assertNotContains(detail, secret.title)
        preview = self.client.post(self.create_url, {
            "title": "Dentist", "date": self.date, "start_time": "15:30", "end_time": "16:00",
            "timezone": "America/New_York", "participants": [self.kid.pk], "action": "preview",
        })
        self.assertEqual([entry["event"] for entry in preview.context["event_conflicts"]], [soccer])
        self.assertNotContains(preview, secret.title)

    def test_other_household_cannot_see_our_conflicts(self):
        self.clash()
        self.client.force_login(self.other_parent)
        response = self.client.get(reverse("events:list"), {"view": "day", "date": self.date, "timezone": "America/New_York"})
        self.assertEqual(response.context["conflicts"], [])
        self.assertNotContains(response, "Soccer")
        self.assertNotContains(response, "Piano")

    def test_events_in_two_households_of_one_admin_are_not_compared(self):
        second = Workspace.objects.create(name="Second Family", primary_admin=self.parent)
        self.event("Soccer", self.start, self.start + timedelta(hours=2), [self.parent])
        self.event("Board meeting", self.start, self.start + timedelta(hours=2), [self.parent], workspace=second)
        response = self.client.get(reverse("events:list"), {"view": "day", "date": self.date, "timezone": "America/New_York"})
        self.assertContains(response, "Board meeting")
        self.assertEqual(response.context["conflicts"], [])


class ConflictQueryCountTests(TestCase):
    """Conflict detection must not add a query per event."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.parent = User.objects.create_user(username="PrimaryParent")
        cls.kids = [User.objects.create_user(username=f"Kid{index}") for index in range(3)]
        cls.workspace = Workspace.objects.create(name="Our Family", primary_admin=cls.parent)
        for kid in cls.kids:
            Membership.objects.create(
                workspace=cls.workspace, user=kid, role=Membership.Role.KID,
                invited_by=cls.parent, accepted_at=django_timezone.now(),
            )

    def setUp(self):
        self.client.force_login(self.parent)
        self.start = (django_timezone.now() + timedelta(days=2)).astimezone(NEW_YORK).replace(
            hour=8, minute=0, second=0, microsecond=0,
        )
        self.added = 0

    def add_events(self, count):
        for _ in range(count):
            starts_at = self.start + timedelta(minutes=45 * self.added)
            event = Event.objects.create(
                title=f"Event {self.added}", starts_at=starts_at, ends_at=starts_at + timedelta(minutes=90),
                timezone_name="America/New_York", workspace=self.workspace, created_by=self.parent,
            )
            event.participants.set([self.kids[self.added % 3], self.parent])
            self.added += 1
        return event

    def assert_constant_queries(self, expected, request):
        for count in (3, 12):
            self.add_events(count - self.added)
            with self.subTest(events=count), self.assertNumQueries(expected):
                self.assertEqual(request().status_code, 200)

    def test_schedule_views(self):
        list_url = reverse("events:workspace_list", args=[self.workspace.pk])
        params = {"date": self.start.date().isoformat(), "timezone": "America/New_York"}
        for view, expected in (("day", 10), ("week", 7), ("upcoming", 9)):
            with self.subTest(view=view):
                self.added = 0
                Event.objects.all().delete()
                self.assert_constant_queries(expected, lambda: self.client.get(list_url, {**params, "view": view}))

    def test_event_detail(self):
        first = self.add_events(1)
        self.assert_constant_queries(7, lambda: self.client.get(reverse("events:detail", args=[first.pk])))

    def test_create_preview(self):
        create_url = reverse("events:workspace_create", args=[self.workspace.pk])
        data = {
            "title": "New", "date": self.start.date().isoformat(), "start_time": "09:00", "end_time": "12:00",
            "timezone": "America/New_York", "participants": [kid.pk for kid in self.kids], "action": "preview",
        }
        self.assert_constant_queries(8, lambda: self.client.post(create_url, data))
