from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone as django_timezone

from accounts.models import Membership, Workspace
from calendars.models import CalendarConnection

from .access import family_events
from .models import Event


class KidEventVisibilityTests(TestCase):
    """Kids see their own plans and whole-family ones, never just the parents'."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.admin = User.objects.create_user(username="Ana", password="FamilyTest2026!")
        cls.parent = User.objects.create_user(username="Mateo", password="FamilyTest2026!")
        cls.kid = User.objects.create_user(username="Lucia", password="FamilyTest2026!")
        cls.kid2 = User.objects.create_user(username="Tomas", password="FamilyTest2026!")
        cls.workspace = Workspace.objects.create(name="Reyes", primary_admin=cls.admin)
        for user, role in (
            (cls.parent, Membership.Role.PARENT),
            (cls.kid, Membership.Role.KID),
            (cls.kid2, Membership.Role.KID),
        ):
            Membership.objects.create(
                workspace=cls.workspace, user=user, role=role,
                invited_by=cls.admin, accepted_at=django_timezone.now(),
            )

        soon = django_timezone.now() + timedelta(hours=2)
        cls.family_dinner = cls.event("Family dinner", soon)
        cls.kid_practice = cls.event("Soccer practice", soon, people=[cls.kid])
        cls.shared_trip = cls.event("Zoo trip", soon, people=[cls.admin, cls.kid])
        cls.parents_only = cls.event("Date night", soon, people=[cls.admin, cls.parent])
        cls.sibling_only = cls.event("Piano lesson", soon, people=[cls.kid2])
        connection = CalendarConnection.objects.create(
            user=cls.parent, workspace=cls.workspace, provider=CalendarConnection.Provider.GOOGLE,
        )
        cls.parent_import = cls.event(
            "Work meeting", soon, created_by=cls.parent, source=Event.Source.GOOGLE,
            calendar_connection=connection, external_id="work-1",
        )

    @classmethod
    def event(cls, title, starts_at, people=(), created_by=None, **fields):
        event = Event.objects.create(
            created_by=created_by or cls.admin, workspace=cls.workspace, title=title,
            starts_at=starts_at, ends_at=starts_at + timedelta(hours=1), **fields,
        )
        event.participants.set(people)
        return event

    def titles(self, user):
        return set(family_events(user).values_list("title", flat=True))

    def test_kid_sees_own_and_whole_family_events(self):
        self.assertSetEqual(
            self.titles(self.kid),
            {"Family dinner", "Soccer practice", "Zoo trip"},
        )

    def test_kid_does_not_see_parents_or_siblings_events(self):
        hidden = self.titles(self.kid) & {"Date night", "Piano lesson", "Work meeting"}
        self.assertSetEqual(hidden, set())

    def test_parents_still_see_every_family_event(self):
        everything = {
            "Family dinner", "Soccer practice", "Zoo trip",
            "Date night", "Piano lesson", "Work meeting",
        }
        self.assertSetEqual(self.titles(self.admin), everything)
        self.assertSetEqual(self.titles(self.parent), everything)

    def test_kid_cannot_open_a_parents_event_by_link(self):
        self.client.force_login(self.kid)

        hidden = self.client.get(reverse("events:detail", args=[self.parents_only.pk]))
        visible = self.client.get(reverse("events:detail", args=[self.kid_practice.pk]))

        self.assertEqual(hidden.status_code, 404)
        self.assertEqual(visible.status_code, 200)

    def test_schedule_list_hides_parents_events_from_kids(self):
        self.client.force_login(self.kid)

        response = self.client.get(
            reverse("events:workspace_list", args=[self.workspace.pk]) + "?view=upcoming"
        )

        self.assertContains(response, "Soccer practice")
        self.assertNotContains(response, "Date night")
        self.assertNotContains(response, "Work meeting")

    def test_kid_week_shows_only_their_row_and_plans(self):
        self.client.force_login(self.kid)

        response = self.client.get(reverse("accounts:dashboard"))

        self.assertTemplateUsed(response, "accounts/kid_dashboard.html")
        self.assertEqual(response.context["my_week"]["user"], self.kid)
        self.assertContains(response, "My week")
        self.assertContains(response, "Soccer practice")
        self.assertContains(response, "Family dinner")
        self.assertNotContains(response, "Date night")
        self.assertNotContains(response, "Piano lesson")

    def test_kid_timeline_shows_today_without_parents_plans(self):
        self.client.force_login(self.kid)

        response = self.client.get(reverse("accounts:dashboard"))

        self.assertContains(response, "My day")
        self.assertContains(response, ">Previous<")
        self.assertContains(response, ">Next<")
        rail_titles = {item["event"].title for item in response.context["rail"]["items"]}
        self.assertLessEqual(rail_titles, {"Family dinner", "Soccer practice", "Zoo trip"})

    def test_kid_timeline_is_hidden_on_other_weeks(self):
        self.client.force_login(self.kid)
        next_week = (django_timezone.localdate() + timedelta(days=7)).isoformat()

        response = self.client.get(reverse("accounts:dashboard"), {"week": next_week})

        self.assertIsNone(response.context["rail"])
        self.assertNotContains(response, "My day")

    def test_parent_week_is_unchanged(self):
        self.client.force_login(self.admin)

        response = self.client.get(reverse("accounts:dashboard"))

        self.assertTemplateUsed(response, "accounts/parent_dashboard.html")
        self.assertNotIn("my_week", response.context)
        self.assertContains(response, "Date night")
