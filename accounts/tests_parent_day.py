import re
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.models import BriefingSeen, Membership, Workspace
from accounts.tests_kid_day import MORNING, NEW_YORK, WEDNESDAY, at, frozen
from events.briefing import build_family_briefing
from events.models import Event


class ParentDayTests(TestCase):
    """The parent's "Your day" pop-up: the whole family's plans, their drives, open rides and overlaps."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.ana = User.objects.create_user(username="ana", first_name="Ana")
        cls.mateo = User.objects.create_user(username="mateo", first_name="Mateo")
        cls.lucia = User.objects.create_user(username="lucia", first_name="Lucia")
        cls.tomas = User.objects.create_user(username="tomas", first_name="Tomas")
        # Ana created the family, so like every primary admin she has no membership row.
        cls.workspace = Workspace.objects.create(name="Reyes", primary_admin=cls.ana)
        for user, role in ((cls.mateo, Membership.Role.PARENT), (cls.lucia, Membership.Role.KID), (cls.tomas, Membership.Role.KID)):
            Membership.objects.create(
                workspace=cls.workspace, user=user, role=role, invited_by=cls.ana, accepted_at=MORNING,
            )
        cls.url = reverse("accounts:dashboard")
        cls.seen_url = reverse("accounts:day_seen")

    def plan(self, title, starts_at, ends_at, people=(), **fields):
        event = Event.objects.create(
            created_by=self.ana, workspace=self.workspace, title=title,
            starts_at=starts_at, ends_at=ends_at, **fields,
        )
        event.participants.set(people)
        return event

    def briefing(self, now, user=None):
        return build_family_briefing(user or self.ana, self.workspace, now, NEW_YORK)

    def dashboard(self, now, user=None):
        self.client.force_login(user or self.ana)
        with frozen(now):
            return self.client.get(self.url)

    def pop_up(self, response):
        match = re.search(r'<dialog class="my-day".*?</dialog>', response.content.decode(), re.S)
        self.assertIsNotNone(match, "the parent dashboard has no Your day pop-up")
        return match.group(0)

    def opens_by_itself(self, response):
        return "data-auto-open" in self.pop_up(response).split(">", 1)[0]

    # ---- Opening once per local day ----

    def test_the_primary_admin_gets_it_once_a_day(self):
        response = self.dashboard(MORNING)
        self.assertTemplateUsed(response, "accounts/parent_dashboard.html")
        self.assertTrue(self.opens_by_itself(response))
        self.assertContains(response, "data-open-my-day")

        with frozen(MORNING):
            self.assertEqual(self.client.post(self.seen_url, HTTP_X_REQUESTED_WITH="fetch").status_code, 204)
        self.assertEqual(BriefingSeen.objects.get(user=self.ana).seen_on, WEDNESDAY)
        self.assertFalse(self.opens_by_itself(self.dashboard(MORNING + timedelta(hours=2))))
        self.assertTrue(self.opens_by_itself(self.dashboard(at(WEDNESDAY + timedelta(days=1), 7))))

    def test_each_parent_closes_their_own(self):
        self.client.force_login(self.ana)
        with frozen(MORNING):
            self.client.post(self.seen_url)
        self.assertTrue(self.opens_by_itself(self.dashboard(MORNING, self.mateo)))

    # ---- What it says ----

    def test_shows_every_family_plan_and_who_it_is_for(self):
        self.plan("Lucia's dentist", at(WEDNESDAY, 14), at(WEDNESDAY, 15), [self.lucia])
        self.plan("Tomas's swim", at(WEDNESDAY, 16), at(WEDNESDAY, 17), [self.tomas])
        self.plan("Parents' night out", at(WEDNESDAY, 20), at(WEDNESDAY, 23), [self.ana, self.mateo])
        self.plan("Family dinner", at(WEDNESDAY, 19), at(WEDNESDAY, 20))

        pop_up = self.pop_up(self.dashboard(MORNING))

        self.assertEqual(
            [item["event"].title for item in self.briefing(MORNING)["items"]],
            ["Lucia's dentist", "Tomas's swim", "Family dinner", "Parents' night out"],
        )
        self.assertIn("4 family plans left today.", pop_up)
        self.assertIn("Whole family", pop_up)
        self.assertIn('You, <span translate="no">Mateo</span>', pop_up)

    def test_puts_their_drives_and_the_rides_with_no_driver_first(self):
        self.plan(
            "Soccer", at(WEDNESDAY, 16), at(WEDNESDAY, 18), [self.lucia], requires_transport=True,
            dropoff_driver=self.mateo, pickup_driver=self.ana,
        )
        self.plan("School", at(WEDNESDAY, 11), at(WEDNESDAY, 15), [self.tomas], requires_transport=True, dropoff_driver=self.ana)

        briefing = self.briefing(MORNING)
        pop_up = self.pop_up(self.dashboard(MORNING))

        self.assertEqual([(drive["event"].title, drive["leg"]) for drive in briefing["my_drives"]], [("School", "Drop-off"), ("Soccer", "Pickup")])
        self.assertEqual(briefing["open_rides"], 1)
        self.assertIn("You drive", pop_up)
        self.assertIn("1 ride still needs a driver.", pop_up)
        self.assertIn("No driver yet", pop_up)
        self.assertNotIn("ask a parent", pop_up)
        self.assertLess(pop_up.index("You drive"), pop_up.index('class="my-day-list"'))

    def test_leaves_out_rides_that_already_happened(self):
        self.plan(
            "School", at(WEDNESDAY, 8), at(WEDNESDAY, 15), [self.lucia], requires_transport=True,
            dropoff_driver=self.ana,
        )
        briefing = self.briefing(MORNING)
        self.assertEqual([(drive["event"].title, drive["leg"]) for drive in briefing["my_drives"]], [])
        self.assertEqual(briefing["open_rides"], 1)  # the pickup, still ahead

    def test_says_so_when_they_have_no_drives(self):
        self.plan("Soccer", at(WEDNESDAY, 16), at(WEDNESDAY, 18), [self.lucia], requires_transport=True, dropoff_driver=self.mateo, pickup_driver=self.mateo)
        pop_up = self.pop_up(self.dashboard(MORNING))
        self.assertIn("No drives for you today.", pop_up)
        self.assertNotIn("still needs a driver", pop_up)

    def test_flags_plans_that_double_book_someone(self):
        piano = self.plan("Piano", at(WEDNESDAY, 16), at(WEDNESDAY, 17), [self.lucia])
        soccer = self.plan("Soccer", at(WEDNESDAY, 16, 30), at(WEDNESDAY, 18), [self.lucia, self.tomas])
        self.plan("Swim", at(WEDNESDAY, 18), at(WEDNESDAY, 19), [self.tomas])  # starts as Soccer ends
        self.plan("Art", at(WEDNESDAY, 16), at(WEDNESDAY, 17), [self.mateo])  # same time, someone else
        self.plan("Field trip", at(WEDNESDAY, 0), at(WEDNESDAY + timedelta(days=1), 0), [self.lucia])  # all day

        briefing = self.briefing(MORNING)
        clashes = {item["event"].title: [clash["event"] for clash in item["clashes"]] for item in briefing["items"]}
        pop_up = self.pop_up(self.dashboard(MORNING))

        self.assertEqual(clashes, {"Field trip": [], "Piano": [soccer], "Art": [], "Soccer": [piano], "Swim": []})
        self.assertEqual(briefing["clashes"], 2)
        self.assertIn("2 plans overlap for the same person.", pop_up)
        self.assertIn('Overlaps Soccer for <span translate="no">Lucia</span>', pop_up)

    def test_shows_tomorrow_once_the_family_is_done_and_says_when_it_is_free(self):
        self.plan("Breakfast", at(WEDNESDAY, 7), at(WEDNESDAY, 8))
        self.plan("Thursday school", at(WEDNESDAY + timedelta(days=1), 8), at(WEDNESDAY + timedelta(days=1), 15), [self.lucia])

        pop_up = self.pop_up(self.dashboard(at(WEDNESDAY, 21)))

        self.assertIn("Your day tomorrow", pop_up)
        self.assertIn("The family is done for today. Here’s tomorrow.", pop_up)
        self.assertIn("Thursday school", pop_up)
        self.assertIn("No drives for you tomorrow.", pop_up)
        Event.objects.all().delete()
        self.assertIn("A free day for the family.", self.pop_up(self.dashboard(MORNING)))

    # ---- Changing how a plan is reached, right from the pop-up ----

    def test_parents_get_a_transport_dropdown_and_kids_get_text(self):
        soccer = self.plan("Soccer", at(WEDNESDAY, 16), at(WEDNESDAY, 18), [self.lucia], requires_transport=True, transport_mode=Event.TransportMode.DRIVING)
        pop_up = self.pop_up(self.dashboard(MORNING))
        self.assertIn(f'action="{reverse("events:set_transport_mode", args=[soccer.pk])}"', pop_up)
        self.assertIn('<option value="driving" selected>Car</option>', pop_up)
        self.assertIn('<option value="walking">Walking</option>', pop_up)

        kid_pop_up = self.pop_up(self.dashboard(MORNING, self.lucia))
        self.assertNotIn("<select", kid_pop_up)
        self.assertIn("<dd>Car</dd>", kid_pop_up)

    def test_a_parent_changes_the_mode_and_only_that(self):
        soccer = self.plan(
            "Soccer", at(WEDNESDAY, 16), at(WEDNESDAY, 18), [self.lucia], requires_transport=True,
            transport_mode=Event.TransportMode.DRIVING, dropoff_driver=self.mateo,
        )
        url = reverse("events:set_transport_mode", args=[soccer.pk])
        self.client.force_login(self.mateo)

        response = self.client.post(url, {"transport_mode": "walking"}, HTTP_X_REQUESTED_WITH="fetch")
        self.assertEqual(response.status_code, 204)
        soccer.refresh_from_db()
        self.assertEqual((soccer.transport_mode, soccer.dropoff_driver, soccer.title), ("walking", self.mateo, "Soccer"))

        response = self.client.post(url, {"transport_mode": "cycling", "next": "/accounts/dashboard/"})
        self.assertRedirects(response, "/accounts/dashboard/", fetch_redirect_response=False)
        soccer.refresh_from_db()
        self.assertEqual(soccer.transport_mode, "cycling")

    def test_only_this_familys_parents_can_change_the_mode(self):
        soccer = self.plan("Soccer", at(WEDNESDAY, 16), at(WEDNESDAY, 18), [self.lucia], requires_transport=True, transport_mode=Event.TransportMode.DRIVING)
        url = reverse("events:set_transport_mode", args=[soccer.pk])
        outsider = get_user_model().objects.create_user(username="outsider")
        Workspace.objects.create(name="Other family", primary_admin=outsider)
        for user in (self.lucia, outsider):
            self.client.force_login(user)
            self.assertEqual(self.client.post(url, {"transport_mode": "walking"}).status_code, 404)
        self.client.force_login(self.ana)
        self.assertEqual(self.client.post(url, {"transport_mode": "rocket"}).status_code, 400)
        self.assertEqual(self.client.get(url).status_code, 405)
        response = self.client.post(url, {"transport_mode": "walking", "next": "https://evil.example/"})
        self.assertRedirects(response, reverse("accounts:dashboard"), fetch_redirect_response=False)
        soccer.refresh_from_db()
        self.assertEqual(soccer.transport_mode, "walking")

    def test_drawn_in_the_parents_own_colour(self):
        head = self.pop_up(self.dashboard(MORNING)).split(">", 1)[0]
        self.assertRegex(head, r'data-hue="[a-h]"')

    def test_query_count_does_not_grow_with_plans(self):
        def count(plans):
            Event.objects.all().delete()
            for index in range(plans):
                self.plan(
                    f"Plan {index}", at(WEDNESDAY, 12 + index), at(WEDNESDAY, 12 + index, 30), [self.lucia, self.tomas],
                    requires_transport=True, dropoff_driver=self.mateo, pickup_driver=self.ana,
                )
            self.client.force_login(self.ana)
            with frozen(MORNING), CaptureQueriesContext(connection) as queries:
                self.client.get(self.url)
            return len(queries)

        self.assertEqual(count(1), count(5))
