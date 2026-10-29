import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone as django_timezone

from accounts.models import MemberColor, Membership, Workspace

from ..models import Event
from ..timeline import DECK_HUES, family_members
from ..week import build_week, chip_level, week_start_for

NEW_YORK = ZoneInfo("America/New_York")


def at(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=NEW_YORK)


class WeekLayoutTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.admin = User.objects.create_user(username="Ana", password="FamilyTest2026!")
        cls.parent = User.objects.create_user(username="Mateo")
        cls.kid = User.objects.create_user(username="Lucia")
        cls.kid2 = User.objects.create_user(username="Tomas")
        cls.extra = User.objects.create_user(username="Zoe")
        cls.workspace = Workspace.objects.create(name="Reyes", primary_admin=cls.admin)
        for user, role in (
            (cls.parent, Membership.Role.PARENT), (cls.kid, Membership.Role.KID),
            (cls.kid2, Membership.Role.KID), (cls.extra, Membership.Role.KID),
        ):
            Membership.objects.create(
                workspace=cls.workspace, user=user, role=role,
                invited_by=cls.admin, accepted_at=django_timezone.now(),
            )
        cls.monday = date(2026, 10, 26)

    def event(self, title, starts_at, ends_at, people=()):
        event = Event.objects.create(
            created_by=self.admin, workspace=self.workspace, title=title,
            starts_at=starts_at, ends_at=ends_at,
        )
        event.participants.set(people)
        return event

    def week(self):
        members = family_members([self.workspace])
        events = Event.objects.prefetch_related("participants").order_by("starts_at", "pk")
        return build_week(events, members, self.monday, NEW_YORK, self.monday)

    def row(self, week, user):
        return next(row for row in week["rows"] if row["user"].pk == user.pk)

    def test_week_starts_on_monday(self):
        self.assertEqual(week_start_for(date(2026, 11, 1)), self.monday)
        self.assertEqual(week_start_for(self.monday), self.monday)

    def test_members_get_deck_hues_in_family_order_and_wrap(self):
        members = family_members([self.workspace])
        self.assertEqual([member["user"].username for member in members], ["Ana", "Mateo", "Lucia", "Tomas", "Zoe"])
        self.assertEqual([member["hue"] for member in members], ["a", "b", "c", "d", "e"])
        self.assertEqual(DECK_HUES, ("a", "b", "c", "d", "e", "f", "g", "h"))

    def test_ninth_member_wraps_to_the_first_hue(self):
        User = get_user_model()
        for index in range(4):
            user = User.objects.create_user(username=f"Zz{index}")
            Membership.objects.create(
                workspace=self.workspace, user=user, role=Membership.Role.KID,
                invited_by=self.admin, accepted_at=django_timezone.now(),
            )
        hues = [member["hue"] for member in family_members([self.workspace])]
        self.assertEqual(len(hues), 9)
        self.assertEqual(hues[:8], list(DECK_HUES))
        self.assertEqual(hues[8], "a")

    def test_a_day_of_only_unconfirmed_plans_is_dashed(self):
        tuesday = self.monday + timedelta(days=1)
        maybe = self.event("Sleepover?", at(self.monday, 18), at(self.monday, 20), [self.kid])
        maybe.is_tentative = True
        maybe.save()
        self.event("Swim", at(tuesday, 16), at(tuesday, 17), [self.kid])
        other = self.event("Party?", at(tuesday, 18), at(tuesday, 19), [self.kid])
        other.is_tentative = True
        other.save()
        chips = self.row(self.week(), self.kid)["chips"]
        self.assertTrue(chips[0]["tentative"])
        self.assertTrue(chips[0]["items"][0]["tentative"])
        self.assertFalse(chips[1]["tentative"])  # one confirmed plan keeps the edge solid
        self.assertFalse(chips[2]["tentative"])  # a free day is not "unconfirmed"

    def test_picked_colour_overrides_the_deck(self):
        MemberColor.objects.create(workspace=self.workspace, user=self.kid, color="#2e7d32")
        kid = next(member for member in family_members([self.workspace]) if member["user"] == self.kid)
        self.assertEqual(kid["custom_color"], "#2e7d32")
        parent = next(member for member in family_members([self.workspace]) if member["user"] == self.parent)
        self.assertIsNone(parent["custom_color"])

    def test_chip_depth_follows_how_full_the_day_is(self):
        self.event("Swim", at(self.monday, 16), at(self.monday, 17), [self.kid])
        self.event("Soccer", at(self.monday, 17, 30), at(self.monday, 18, 30), [self.kid])
        tuesday = self.monday + timedelta(days=1)
        self.event("School trip", at(tuesday, 8), at(tuesday, 15), [self.kid])
        chips = self.row(self.week(), self.kid)["chips"]
        self.assertEqual([chip["level"] for chip in chips[:3]], [2, 4, 0])
        self.assertEqual([item["event"].title for item in chips[0]["items"]], ["Swim", "Soccer"])

    def test_level_caps_and_extra_plans_are_counted(self):
        for hour in range(9, 14):
            self.event(f"Plan {hour}", at(self.monday, hour), at(self.monday, hour, 30), [self.parent])
        chip = self.row(self.week(), self.parent)["chips"][0]
        self.assertEqual(chip["level"], 4)
        self.assertEqual(len(chip["items"]), 2)
        self.assertEqual(chip["more"], 3)
        self.assertEqual(chip_level([]), 0)

    def test_event_without_participants_shows_for_the_whole_family(self):
        self.event("Lunch at Grandma's", at(self.monday, 12), at(self.monday, 13))
        week = self.week()
        for row in week["rows"]:
            self.assertEqual(row["chips"][0]["count"], 1, row["user"].username)

    def test_multi_day_event_spans_each_day_and_marks_all_day(self):
        tuesday = self.monday + timedelta(days=1)
        self.event("Boston trip", at(self.monday, 18), at(tuesday + timedelta(days=1), 18, 40), [self.admin])
        chips = self.row(self.week(), self.admin)["chips"]
        self.assertEqual([chip["count"] for chip in chips[:4]], [1, 1, 1, 0])
        self.assertTrue(chips[1]["items"][0]["all_day"])
        self.assertTrue(chips[2]["items"][0]["starts_earlier"])
        self.assertEqual(chips[1]["level"], 4)

    def test_dst_week_keeps_seven_local_days(self):
        # US clocks fall back on Sunday 1 November 2026: that day is 25 hours long.
        week = self.week()
        sunday = week["days"][6]
        self.assertEqual(sunday["date"], date(2026, 11, 1))
        self.assertEqual(sunday["end"].astimezone(ZoneInfo("UTC")) - sunday["start"].astimezone(ZoneInfo("UTC")), timedelta(hours=25))
        self.event("Late show", at(date(2026, 11, 1), 23), at(date(2026, 11, 1), 23, 45), [self.kid])
        self.assertEqual(self.row(self.week(), self.kid)["chips"][6]["count"], 1)


@override_settings(TIME_ZONE="America/New_York")
class WeekPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.admin = User.objects.create_user(username="Ana")
        cls.kid = User.objects.create_user(username="Lucia")
        cls.workspace = Workspace.objects.create(name="Reyes", primary_admin=cls.admin)
        Membership.objects.create(
            workspace=cls.workspace, user=cls.kid, role=Membership.Role.KID,
            invited_by=cls.admin, accepted_at=django_timezone.now(),
        )

    def setUp(self):
        self.client.force_login(self.admin)
        self.url = reverse("accounts:dashboard")

    def test_week_page_draws_a_card_per_member_with_their_hue(self):
        response = self.client.get(self.url, {"week": "2026-10-28"})
        self.assertContains(response, "Reyes week")
        self.assertContains(response, "Mon 26 Oct to Sun 1 Nov")
        self.assertContains(response, 'class="card" data-hue="a"')
        self.assertContains(response, 'class="card" data-hue="b"')
        self.assertContains(response, "?week=2026-10-19")
        self.assertContains(response, "?week=2026-11-02")

    def test_bad_or_far_off_week_falls_back_to_this_week(self):
        this_week = week_start_for(django_timezone.localdate())
        for value in ("not-a-date", "0001-01-01", "9999-12-31"):
            with self.subTest(week=value):
                response = self.client.get(self.url, {"week": value})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["week_start"], this_week)

    def test_week_query_count_does_not_grow_with_events(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        monday = week_start_for(django_timezone.localdate())

        def add_plan(day):
            event = Event.objects.create(
                created_by=self.admin, workspace=self.workspace, title=f"Plan {day}",
                starts_at=at(monday + timedelta(days=day), 9), ends_at=at(monday + timedelta(days=day), 10),
            )
            event.participants.set([self.kid])

        # Start with today's plan, so the "Your day" briefing has something to show from the first count.
        today = (django_timezone.localdate() - monday).days
        add_plan(today)
        with CaptureQueriesContext(connection) as one_event:
            self.client.get(self.url)
        for day in range(7):
            if day != today:
                add_plan(day)
        with self.assertNumQueries(len(one_event)):
            self.client.get(self.url)

    def test_kid_keeps_the_kid_dashboard_and_gets_no_add_button(self):
        self.client.force_login(self.kid)
        response = self.client.get(self.url)
        self.assertTemplateUsed(response, "accounts/kid_dashboard.html")
        self.assertNotContains(response, "add-btn")

    def test_parent_shell_has_add_event_and_one_current_nav_item(self):
        response = self.client.get(self.url)
        self.assertContains(response, reverse("events:workspace_create", args=[self.workspace.pk]))
        self.assertContains(response, 'aria-current="page"', count=1)

    def test_pages_have_no_inline_scripts(self):
        # The production CSP is script-src 'self': an inline <script> would be blocked.
        self.client.logout()
        pages = [self.client.get(reverse("accounts:login"))]
        self.client.force_login(self.admin)
        pages += [self.client.get(self.url), self.client.get(reverse("events:workspace_list", args=[self.workspace.pk]))]
        for response in pages:
            for tag in re.findall(r"<script\b[^>]*>", response.content.decode()):
                self.assertIn(" src=", tag)


class RailColumnTests(TestCase):
    def test_plans_that_collide_on_screen_share_the_width(self):
        from ..week import place_side_by_side
        # 15:15-15:45 then 15:45-16:30: back to back in time, but the first is drawn 24px tall.
        items = [{"top": 48.529, "height": 2.941}, {"top": 51.471, "height": 4.412}, {"top": 80.0, "height": 5.0}]
        place_side_by_side(items)
        self.assertEqual([(item["column"], item["columns"]) for item in items], [(0, 2), (1, 2), (0, 1)])

    def test_a_long_plan_and_a_short_one_inside_it_split_once(self):
        from ..week import place_side_by_side
        items = [{"top": 10, "height": 30}, {"top": 15, "height": 5}, {"top": 25, "height": 5}]
        place_side_by_side(items)
        self.assertEqual([(item["column"], item["columns"]) for item in items], [(0, 2), (1, 2), (1, 2)])


class PaletteTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.admin = User.objects.create_user(username="Ana")
        cls.parent = User.objects.create_user(username="Mateo")
        cls.kid = User.objects.create_user(username="Lucia")
        cls.outsider = User.objects.create_user(username="Other")
        cls.workspace = Workspace.objects.create(name="Reyes", primary_admin=cls.admin)
        Workspace.objects.create(name="Other family", primary_admin=cls.outsider)
        for user, role in ((cls.parent, Membership.Role.PARENT), (cls.kid, Membership.Role.KID)):
            Membership.objects.create(
                workspace=cls.workspace, user=user, role=role,
                invited_by=cls.admin, accepted_at=django_timezone.now(),
            )

    def setUp(self):
        self.url = reverse("accounts:set_palette", args=[self.workspace.pk])

    def test_new_families_start_on_primaries(self):
        self.assertEqual(self.workspace.palette, Workspace.Palette.PRIMARIES)

    def test_a_parent_changes_the_family_deck_and_returns_to_the_page(self):
        self.client.force_login(self.parent)
        response = self.client.post(self.url, {"palette": "garden", "next": "/events/?view=day"})
        self.assertRedirects(response, "/events/?view=day", fetch_redirect_response=False)
        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.palette, "garden")
        page = self.client.get(reverse("accounts:dashboard"))
        self.assertContains(page, 'data-deck="garden"')
        self.assertContains(page, 'value="garden" aria-pressed="true"', count=2)  # bar tab + phone menu

    def test_kids_see_the_family_deck_but_cannot_change_it(self):
        self.workspace.palette = "bold"
        self.workspace.save()
        self.client.force_login(self.kid)
        page = self.client.get(reverse("accounts:dashboard"))
        self.assertContains(page, 'data-deck="bold"')
        self.assertContains(page, "My colors")
        self.assertNotContains(page, "Family colors")
        self.assertEqual(self.client.post(self.url, {"palette": "garden"}).status_code, 404)
        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.palette, "bold")

    def test_a_kid_picks_a_preset_for_their_own_screens_only(self):
        my_url = reverse("accounts:set_my_palette", args=[self.workspace.pk])
        self.client.force_login(self.kid)
        response = self.client.post(my_url, {"palette": "garden", "next": "/events/?view=day"})
        self.assertRedirects(response, "/events/?view=day", fetch_redirect_response=False)
        page = self.client.get(reverse("accounts:dashboard"))
        self.assertContains(page, 'data-deck="garden"')
        self.assertContains(page, 'value="garden" aria-pressed="true"', count=2)  # bar tab + phone menu

        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.palette, "primaries")
        self.client.force_login(self.parent)
        self.assertContains(self.client.get(reverse("accounts:dashboard")), 'data-deck="primaries"')

    def test_only_kids_can_pick_their_own_preset(self):
        my_url = reverse("accounts:set_my_palette", args=[self.workspace.pk])
        for user in (self.parent, self.outsider):
            self.client.force_login(user)
            self.assertEqual(self.client.post(my_url, {"palette": "garden"}).status_code, 404)
        self.client.force_login(self.kid)
        self.assertEqual(self.client.post(my_url, {"palette": "neon"}).status_code, 400)
        self.assertEqual(self.client.get(my_url).status_code, 405)
        self.assertEqual(Membership.objects.get(user=self.kid).palette, "")

    def test_other_families_cannot_change_it(self):
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.post(self.url, {"palette": "garden"}).status_code, 404)
        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.palette, "primaries")

    def test_unknown_palette_and_offsite_next_are_refused(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post(self.url, {"palette": "neon"}).status_code, 400)
        response = self.client.post(self.url, {"palette": "bold", "next": "https://evil.example/"})
        self.assertRedirects(response, reverse("accounts:dashboard"), fetch_redirect_response=False)

    def test_get_is_not_allowed(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(self.url).status_code, 405)


class TentativeEventTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.admin = User.objects.create_user(username="Ana")
        cls.workspace = Workspace.objects.create(name="Reyes", primary_admin=cls.admin)

    def test_unconfirmed_plan_is_saved_and_shown_as_not_confirmed(self):
        event = Event.objects.create(
            created_by=self.admin, workspace=self.workspace, title="Sleepover?",
            starts_at=django_timezone.now() + timedelta(hours=1),
            ends_at=django_timezone.now() + timedelta(hours=2), is_tentative=True,
        )
        self.client.force_login(self.admin)
        detail = self.client.get(reverse("events:detail", args=[event.pk]))
        self.assertContains(detail, "Not confirmed yet")
        week = self.client.get(reverse("accounts:dashboard"))
        self.assertContains(week, "Not confirmed")

    def test_events_are_confirmed_by_default(self):
        event = Event.objects.create(
            created_by=self.admin, workspace=self.workspace, title="Swim",
            starts_at=django_timezone.now(), ends_at=django_timezone.now() + timedelta(hours=1),
        )
        self.assertFalse(event.is_tentative)
