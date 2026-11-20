"""Events, checklists and render caps (hardening spec 4.2, 4.3, 5 and 10.1): optimistic
locking per writer, the removed-member rule, the kid query, idempotent checklist
create and the render caps."""

from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Membership, Workspace
from checklists.models import Checklist, ChecklistItem
from events.access import family_events
from events.models import Event
from events.templatetags.paint import initial, person

NY = ZoneInfo("America/New_York")
PASSWORD = "Locking-Test-2026!"


def drop_check(table, name):
    """Drop a CHECK constraint inside the test's transaction, which rolls back."""
    with connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cursor.execute(f"ALTER TABLE {table} DROP CONSTRAINT {name}")


def login(user):
    client = Client()
    client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
    return client


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.admin = User.objects.create_user("lk_admin", password=PASSWORD)
        cls.workspace = Workspace.objects.create(name="Lock", primary_admin=cls.admin)
        cls.parent = User.objects.create_user("lk_parent", password=PASSWORD)
        cls.kid_a = User.objects.create_user("lk_kid_a", password=PASSWORD)
        cls.kid_b = User.objects.create_user("lk_kid_b", password=PASSWORD)
        for user, role in (
            (cls.parent, "parent"),
            (cls.kid_a, "kid"),
            (cls.kid_b, "kid"),
        ):
            Membership.objects.create(
                workspace=cls.workspace,
                user=user,
                role=role,
                invited_by=cls.admin,
                accepted_at=timezone.now(),
            )
        start = datetime(2027, 3, 1, 12, 0, tzinfo=NY)
        cls.event = Event.objects.create(
            created_by=cls.admin,
            workspace=cls.workspace,
            title="Soccer",
            starts_at=start,
            ends_at=start + timedelta(hours=1),
            timezone_name="America/New_York",
        )


class VersionTests(Base):
    def test_a_new_row_starts_at_zero_and_every_save_adds_one(self):
        self.assertEqual(self.event.version, 0)
        self.event.title = "Soccer practice"
        self.event.save(update_fields=["title"])
        self.assertEqual(self.event.version, 1)
        self.event.refresh_from_db()
        self.assertEqual(self.event.version, 1)
        self.event.save()
        self.assertEqual(Event.objects.get(pk=self.event.pk).version, 2)

    def test_an_empty_update_fields_list_changes_nothing(self):
        self.event.save(update_fields=[])
        self.assertEqual(Event.objects.get(pk=self.event.pk).version, 0)

    def _post(self, client, **changes):
        local = timezone.localtime(self.event.starts_at, NY)
        data = {
            "action": "save",
            "title": "Soccer",
            "date": local.date().isoformat(),
            "start_time": "12:00",
            "end_time": "13:00",
            "timezone": "America/New_York",
            "transport_mode": "transit",
        }
        data.update(changes)
        return client.post(reverse("events:edit", args=[self.event.pk]), data)

    def test_stale_hidden_version_gets_409_and_keeps_the_newer_edit(self):
        a, b = login(self.parent), login(self.admin)
        self.assertEqual(self._post(a, title="By A", version="0").status_code, 302)
        response = self._post(b, location="Field 9", version="0")
        self.assertEqual(response.status_code, 409)
        self.assertContains(
            response, "Someone changed this while you were editing", status_code=409
        )
        self.event.refresh_from_db()
        self.assertEqual(self.event.title, "By A")
        self.assertEqual(self.event.location, "")

    def test_the_edit_page_carries_the_version_as_a_hidden_field(self):
        response = login(self.parent).get(reverse("events:edit", args=[self.event.pk]))
        self.assertContains(response, 'name="version"')

    def test_change_time_and_transport_mode_bump_the_version(self):
        client = login(self.parent)
        client.post(
            reverse("events:change_time", args=[self.event.pk]),
            {
                "starts_date": "2027-03-01",
                "starts_time": "14:00",
                "ends_date": "2027-03-01",
                "ends_time": "15:00",
            },
        )
        client.post(
            reverse("events:set_transport_mode", args=[self.event.pk]),
            {"transport_mode": "walking"},
        )
        self.event.refresh_from_db()
        self.assertEqual(self.event.version, 2)

    def test_a_stale_checklist_edit_gets_409(self):
        checklist = Checklist.objects.create(
            workspace=self.workspace, created_by=self.admin, title="Bag"
        )
        url = reverse("checklists:edit", args=[checklist.pk])
        first = login(self.parent).post(url, {"title": "Camp bag", "version": "0"})
        self.assertEqual(first.status_code, 302)
        second = login(self.admin).post(url, {"title": "Old bag", "version": "0"})
        self.assertEqual(second.status_code, 409)
        checklist.refresh_from_db()
        self.assertEqual(checklist.title, "Camp bag")

    def test_a_stale_item_edit_gets_409_and_a_toggle_bumps_the_version(self):
        checklist = Checklist.objects.create(
            workspace=self.workspace, created_by=self.admin, title="Bag"
        )
        item = ChecklistItem.objects.create(checklist=checklist, description="Water")
        client = login(self.parent)
        client.post(
            reverse("checklists:item_toggle", args=[item.pk]), {"completed": "1"}
        )
        item.refresh_from_db()
        self.assertEqual(item.version, 1)
        stale = client.post(
            reverse("checklists:item_edit", args=[item.pk]),
            {"description": "Bottle", "version": "0"},
        )
        self.assertEqual(stale.status_code, 409)
        item.refresh_from_db()
        self.assertEqual(item.description, "Water")


class RemovedMemberTests(Base):
    def test_parents_still_see_a_plan_whose_people_were_all_removed(self):
        self.event.participants.set([self.kid_a])
        membership = Membership.objects.get(user=self.kid_a)
        login(self.admin).post(
            reverse("accounts:remove_member", args=[self.workspace.pk, membership.pk])
        )
        self.event.refresh_from_db()
        self.assertTrue(self.event.private_after_removal)
        self.assertTrue(family_events(self.parent).filter(pk=self.event.pk).exists())
        self.assertFalse(family_events(self.kid_b).filter(pk=self.event.pk).exists())

    def test_a_plan_with_other_people_left_stays_visible_to_them(self):
        self.event.participants.set([self.kid_a, self.kid_b])
        membership = Membership.objects.get(user=self.kid_a)
        login(self.admin).post(
            reverse("accounts:remove_member", args=[self.workspace.pk, membership.pk])
        )
        self.event.refresh_from_db()
        self.assertFalse(self.event.private_after_removal)
        self.assertTrue(family_events(self.kid_b).filter(pk=self.event.pk).exists())

    def test_a_whole_family_plan_stays_visible_to_kids(self):
        self.assertTrue(family_events(self.kid_a).filter(pk=self.event.pk).exists())


class ChecklistCreateTests(Base):
    def test_a_signed_token_makes_one_list_for_two_posts(self):
        client = login(self.parent)
        url = reverse("checklists:create", args=[self.workspace.pk])
        page = client.get(url)
        token = page.context["form"]["submission_token"].value()
        for _ in range(2):
            client.post(url, {"title": "Beach", "submission_token": token})
        self.assertEqual(Checklist.objects.filter(title="Beach").count(), 1)

    def test_a_token_made_for_someone_else_is_ignored(self):
        parent_page = login(self.parent).get(
            reverse("checklists:create", args=[self.workspace.pk])
        )
        token = parent_page.context["form"]["submission_token"].value()
        client = login(self.admin)
        client.post(
            reverse("checklists:create", args=[self.workspace.pk]),
            {"title": "Mine", "submission_token": token},
        )
        self.assertTrue(Checklist.objects.filter(title="Mine").exists())
        self.assertNotEqual(str(Checklist.objects.get(title="Mine").pk), "")

    def test_the_list_page_shows_at_most_50_lists(self):
        Checklist.objects.bulk_create(
            Checklist(workspace=self.workspace, created_by=self.admin, title=f"L{i}")
            for i in range(120)
        )
        response = login(self.parent).get(
            reverse("checklists:list", args=[self.workspace.pk])
        )
        self.assertEqual(len(response.context["checklists"]), 50)
        self.assertContains(response, "Next page")


class RenderTests(Base):
    def test_person_cuts_long_names_and_initial_skips_symbols(self):
        user = get_user_model()(username="x_user", first_name="W" * 150)
        self.assertLessEqual(len(person(user)), 31)
        self.assertEqual(
            initial(get_user_model()(username="u", first_name="  ★Ana")), "A"
        )
        self.assertEqual(initial(get_user_model()(username="--", first_name="")), "?")

    def test_an_invalid_stored_colour_never_reaches_the_page(self):
        from accounts.models import MemberColor

        # The colour CHECK (spec 8.5) refuses this row now; an old release or raw
        # SQL could still have stored one, so drop the check for this test only.
        drop_check("accounts_membercolor", "membercolor_color_hex")
        MemberColor.objects.create(
            workspace=self.workspace, user=self.kid_a, color="><zz"
        )
        response = login(self.parent).get(reverse("accounts:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "&gt;&lt;zz")

    def test_far_future_plans_render_in_any_zone(self):
        # The window CHECK (spec 8.5) refuses this row now; see above.
        drop_check("events_event", "event_within_window")
        far = Event.objects.create(
            created_by=self.admin,
            workspace=self.workspace,
            title="Far",
            starts_at=datetime(9999, 12, 31, 13, 0, tzinfo=dt_timezone.utc),
            ends_at=datetime(9999, 12, 31, 14, 0, tzinfo=dt_timezone.utc),
        )
        response = login(self.parent).get(
            reverse("events:detail", args=[far.pk]) + "?timezone=Pacific/Kiritimati"
        )
        self.assertEqual(response.status_code, 200)


class ZapRoundFourTests(Base):
    """Live round 4 (ZAP) found two kinds of 500. Both must be 4xx or a redirect."""

    def test_a_bare_word_next_falls_back_to_the_dashboard(self):
        response = login(self.parent).post(
            reverse("events:set_transport_mode", args=[self.event.pk]),
            {"transport_mode": "walking", "next": "foo"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("accounts:dashboard"))

    def test_an_event_deleted_during_a_driver_save_is_404(self):
        from unittest import mock

        with mock.patch.object(
            Event, "refresh_from_db", side_effect=Event.DoesNotExist
        ):
            response = login(self.parent).post(
                reverse("events:detail", args=[self.event.pk]),
                {"requires_transport": "on", "dropoff_driver": self.kid_a.pk},
            )
        self.assertEqual(response.status_code, 404)
