import re
from datetime import date, datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.models import BriefingSeen, MemberColor, Membership, Workspace
from events.briefing import build_kid_briefing
from events.models import Event

NEW_YORK = ZoneInfo("America/New_York")


def at(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=NEW_YORK)


WEDNESDAY = date(2026, 10, 28)
MORNING = at(WEDNESDAY, 10)


def frozen(moment):
    return mock.patch("django.utils.timezone.now", return_value=moment)


class KidDayTests(TestCase):
    """The kid's "Your day" pop-up: what it shows, and that it opens by itself once per
    local day.
    """

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.ana = User.objects.create_user(username="ana", first_name="Ana")
        cls.mateo = User.objects.create_user(username="mateo", first_name="Mateo")
        cls.lucia = User.objects.create_user(username="lucia", first_name="Lucia")
        cls.tomas = User.objects.create_user(username="tomas", first_name="Tomas")
        cls.workspace = Workspace.objects.create(name="Reyes", primary_admin=cls.ana)
        for user, role in (
            (cls.ana, Membership.Role.PARENT),
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
        cls.seen_url = reverse("accounts:day_seen")

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

    def briefing(self, now, user=None):
        return build_kid_briefing(user or self.lucia, self.workspace, now, NEW_YORK)

    def dashboard(self, now, user=None):
        self.client.force_login(user or self.lucia)
        with frozen(now):
            return self.client.get(self.url)

    def pop_up(self, response):
        match = re.search(
            r'<dialog class="my-day".*?</dialog>', response.content.decode(), re.S
        )
        self.assertIsNotNone(match, "the kid dashboard has no Your day pop-up")
        return match.group(0)

    def opens_by_itself(self, response):
        return "data-auto-open" in self.pop_up(response).split(">", 1)[0]

    # ---- Opening once per local day ----

    def test_opens_by_itself_on_the_first_visit_of_the_day(self):
        self.assertTrue(self.opens_by_itself(self.dashboard(MORNING)))

    def test_stays_closed_after_the_kid_closes_it_but_can_be_reopened(self):
        self.client.force_login(self.lucia)
        with frozen(MORNING):
            response = self.client.post(self.seen_url)
        self.assertRedirects(response, self.url, fetch_redirect_response=False)

        response = self.dashboard(MORNING + timedelta(hours=3))

        self.assertFalse(self.opens_by_itself(response))
        self.assertContains(response, "data-open-my-day")

    def test_script_close_gets_an_empty_answer(self):
        self.client.force_login(self.lucia)
        with frozen(MORNING):
            response = self.client.post(self.seen_url, HTTP_X_REQUESTED_WITH="fetch")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(BriefingSeen.objects.get(user=self.lucia).seen_on, WEDNESDAY)

    def test_closing_twice_is_harmless(self):
        self.client.force_login(self.lucia)
        with frozen(MORNING):
            self.client.post(self.seen_url, HTTP_X_REQUESTED_WITH="fetch")
            self.client.post(self.seen_url, HTTP_X_REQUESTED_WITH="fetch")
        self.assertEqual(BriefingSeen.objects.get(user=self.lucia).seen_on, WEDNESDAY)

    def test_opens_again_after_local_midnight(self):
        self.client.force_login(self.lucia)
        with frozen(at(WEDNESDAY, 23, 59)):
            self.client.post(self.seen_url)
        self.assertFalse(self.opens_by_itself(self.dashboard(at(WEDNESDAY, 23, 59))))
        # 00:01 Thursday in New York is still Wednesday evening in UTC.
        self.assertTrue(
            self.opens_by_itself(
                self.dashboard(at(WEDNESDAY + timedelta(days=1), 0, 1))
            )
        )

    def test_one_kid_closing_it_does_not_close_it_for_a_sibling(self):
        self.client.force_login(self.lucia)
        with frozen(MORNING):
            self.client.post(self.seen_url)
        self.assertTrue(self.opens_by_itself(self.dashboard(MORNING, self.tomas)))

    def test_a_parent_closing_theirs_leaves_the_kids_open(self):
        self.client.force_login(self.mateo)
        with frozen(MORNING):
            self.client.post(self.seen_url)
        self.assertTrue(self.opens_by_itself(self.dashboard(MORNING)))

    def test_closing_needs_a_signed_in_post(self):
        self.assertEqual(self.client.post(self.seen_url).status_code, 302)
        self.assertFalse(BriefingSeen.objects.exists())
        self.client.force_login(self.lucia)
        self.assertEqual(self.client.get(self.seen_url).status_code, 405)

    # ---- What it says ----

    def test_lists_each_ride_with_drivers_by_name_or_asks_a_parent(self):
        self.plan(
            "Soccer",
            at(WEDNESDAY, 16),
            at(WEDNESDAY, 18),
            [self.lucia],
            requires_transport=True,
            dropoff_driver=self.mateo,
            transport_mode=Event.TransportMode.DRIVING,
        )

        pop_up = self.pop_up(self.dashboard(MORNING))

        self.assertIn("Soccer", pop_up)
        self.assertIn("Drop-off", pop_up)
        self.assertIn("Mateo", pop_up)
        self.assertNotIn(">mateo<", pop_up)
        self.assertIn("Not decided yet, ask a parent", pop_up)
        self.assertIn("Car", pop_up)

    def test_plans_without_transport_say_nothing_about_rides(self):
        self.plan("Homework club", at(WEDNESDAY, 15), at(WEDNESDAY, 16), [self.lucia])

        pop_up = self.pop_up(self.dashboard(MORNING))

        self.assertIn("Homework club", pop_up)
        self.assertNotIn("Drop-off", pop_up)
        self.assertNotIn("Pickup", pop_up)

    def test_shows_only_the_kids_own_and_whole_family_plans(self):
        self.plan("Lucia's dentist", at(WEDNESDAY, 14), at(WEDNESDAY, 15), [self.lucia])
        self.plan("Family dinner", at(WEDNESDAY, 19), at(WEDNESDAY, 20))
        self.plan("Tomas's swim", at(WEDNESDAY, 16), at(WEDNESDAY, 17), [self.tomas])
        self.plan(
            "Parents' night out",
            at(WEDNESDAY, 20),
            at(WEDNESDAY, 23),
            [self.ana, self.mateo],
        )

        titles = [item["event"].title for item in self.briefing(MORNING)["items"]]

        self.assertEqual(titles, ["Lucia's dentist", "Family dinner"])

    def test_leaves_out_plans_that_already_ended_today(self):
        self.plan("Breakfast", at(WEDNESDAY, 7), at(WEDNESDAY, 8), [self.lucia])
        self.plan("Art", at(WEDNESDAY, 13), at(WEDNESDAY, 14), [self.lucia])

        briefing = self.briefing(MORNING)

        self.assertTrue(briefing["is_today"])
        self.assertEqual([item["event"].title for item in briefing["items"]], ["Art"])

    def test_shows_tomorrow_once_today_is_done(self):
        self.plan("Breakfast", at(WEDNESDAY, 7), at(WEDNESDAY, 8), [self.lucia])
        self.plan(
            "Thursday school",
            at(WEDNESDAY + timedelta(days=1), 8),
            at(WEDNESDAY + timedelta(days=1), 15),
            [self.lucia],
        )

        response = self.dashboard(at(WEDNESDAY, 21))
        pop_up = self.pop_up(response)

        self.assertIn("Your day tomorrow", pop_up)
        self.assertIn("all done for today", pop_up)
        self.assertIn("Thursday school", pop_up)
        self.assertNotIn("Breakfast", pop_up)

    def test_a_free_day_says_so(self):
        pop_up = self.pop_up(self.dashboard(MORNING))
        self.assertIn("Today is a free day", pop_up)
        self.assertIn("Nothing planned. Free time!", pop_up)

    def test_all_day_started_earlier_and_not_sure_yet(self):
        self.plan(
            "Field trip",
            at(WEDNESDAY, 0),
            at(WEDNESDAY + timedelta(days=1), 0),
            [self.lucia],
        )
        self.plan(
            "Sleepover",
            at(WEDNESDAY - timedelta(days=1), 19),
            at(WEDNESDAY, 11),
            [self.lucia],
        )
        self.plan(
            "Maybe movies",
            at(WEDNESDAY, 18),
            at(WEDNESDAY, 20),
            [self.lucia],
            is_tentative=True,
        )

        pop_up = self.pop_up(self.dashboard(MORNING))

        self.assertIn("All day", pop_up)
        self.assertIn("Already started", pop_up)
        self.assertIn("Not sure yet", pop_up)

    def test_a_plan_running_past_midnight_shows_on_both_days(self):
        late = self.plan(
            "Stargazing",
            at(WEDNESDAY, 22),
            at(WEDNESDAY + timedelta(days=1), 1),
            [self.lucia],
        )

        self.assertEqual(
            [item["event"] for item in self.briefing(MORNING)["items"]], [late]
        )
        tomorrow = self.briefing(at(WEDNESDAY + timedelta(days=1), 0, 30))
        self.assertEqual(
            [(item["event"], item["starts_earlier"]) for item in tomorrow["items"]],
            [(late, True)],
        )

    def test_the_long_day_when_clocks_go_back(self):
        # 1 November 2026 has 25 hours in New York.
        sunday = date(2026, 11, 1)
        whole = self.plan(
            "Fall back fair",
            at(sunday, 0),
            at(sunday + timedelta(days=1), 0),
            [self.lucia],
        )
        late = self.plan("Late show", at(sunday, 23), at(sunday, 23, 30), [self.lucia])

        briefing = self.briefing(at(sunday, 12))

        self.assertTrue(briefing["is_today"])
        self.assertEqual(
            [(item["event"], item["all_day"]) for item in briefing["items"]],
            [(whole, True), (late, False)],
        )

    def test_drawn_in_the_kids_colour(self):
        MemberColor.objects.create(
            workspace=self.workspace, user=self.lucia, color="#2a9d8f"
        )

        head = self.pop_up(self.dashboard(MORNING)).split(">", 1)[0]

        self.assertRegex(head, r'data-hue="[a-h]"')
        self.assertIn("--hue: #2a9d8f", head)

    def test_driver_summary_uses_the_family_name(self):
        self.plan(
            "Soccer",
            at(WEDNESDAY, 16),
            at(WEDNESDAY, 18),
            [self.lucia],
            requires_transport=True,
            dropoff_driver=self.mateo,
            pickup_driver=self.ana,
        )

        response = self.dashboard(MORNING)

        self.assertContains(response, 'Drop-off: <span translate="no">Mateo</span>')
        self.assertContains(response, 'Pickup: <span translate="no">Ana</span>')

    def test_query_count_does_not_grow_with_plans(self):
        def count(plans):
            Event.objects.all().delete()
            for index in range(plans):
                self.plan(
                    f"Plan {index}",
                    at(WEDNESDAY, 12 + index),
                    at(WEDNESDAY, 12 + index, 30),
                    [self.lucia],
                    requires_transport=True,
                    dropoff_driver=self.mateo,
                    pickup_driver=self.ana,
                )
            self.client.force_login(self.lucia)
            with frozen(MORNING), CaptureQueriesContext(connection) as queries:
                self.client.get(self.url)
            return len(queries)

        self.assertEqual(count(1), count(5))
