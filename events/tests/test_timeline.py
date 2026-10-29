from datetime import datetime
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone as django_timezone

from accounts.models import MemberColor, Membership, Workspace
from calendars.models import CalendarConnection

from ..models import Event
from ..timeline import contrast_ratio, text_color_for

NEW_YORK = ZoneInfo("America/New_York")


class ContrastTests(SimpleTestCase):
    def test_event_text_meets_wcag_aa_on_any_colour(self):
        for background in (
            MemberColor.KID_DEFAULT,
            MemberColor.PARENT_DEFAULT,
            "#fafafa",
            "#101010",
            "#767676",
            "#ffff00",
            "#0000ff",
        ):
            with self.subTest(background=background):
                self.assertGreaterEqual(
                    contrast_ratio(background, text_color_for(background)), 4.5
                )

    def test_defaults_pick_readable_text(self):
        self.assertEqual(text_color_for(MemberColor.KID_DEFAULT), "#000000")
        self.assertEqual(text_color_for(MemberColor.PARENT_DEFAULT), "#ffffff")


class DayTimelineTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.admin = User.objects.create_user(username="PrimaryParent")
        cls.parent = User.objects.create_user(username="AcceptedParent")
        cls.kid = User.objects.create_user(username="AcceptedKid")
        cls.pending = User.objects.create_user(username="PendingParent")
        cls.inactive = User.objects.create_user(username="InactiveKid", is_active=False)
        cls.outsider = User.objects.create_user(username="OtherFamilyParent")
        cls.workspace = Workspace.objects.create(
            name="Shared Family", primary_admin=cls.admin
        )
        Workspace.objects.create(name="Other Family", primary_admin=cls.outsider)
        for user, role, accepted in (
            (cls.parent, Membership.Role.PARENT, True),
            (cls.kid, Membership.Role.KID, True),
            (cls.pending, Membership.Role.PARENT, False),
            (cls.inactive, Membership.Role.KID, True),
        ):
            Membership.objects.create(
                workspace=cls.workspace,
                user=user,
                role=role,
                invited_by=cls.admin,
                accepted_at=django_timezone.now() if accepted else None,
            )

    def setUp(self):
        self.client.force_login(self.parent)
        self.list_url = reverse("events:workspace_list", args=[self.workspace.pk])
        self.colors_url = reverse("events:member_colors", args=[self.workspace.pk])

    def event(self, title, starts_at, ends_at, participants=(), **fields):
        event = Event.objects.create(
            title=title,
            starts_at=starts_at,
            ends_at=ends_at,
            timezone_name="America/New_York",
            workspace=self.workspace,
            created_by=fields.pop("created_by", self.admin),
            **fields,
        )
        event.participants.set(participants)
        return event

    def day(self, date):
        response = self.client.get(
            self.list_url, {"view": "day", "date": date, "timezone": "America/New_York"}
        )
        self.assertEqual(response.status_code, 200)
        return response

    def lane(self, response, user):
        return next(
            lane
            for lane in response.context["timeline"]["lanes"]
            if lane["user"] == user
        )

    def test_lanes_list_active_family_parents_first(self):
        lanes = self.day("2026-10-23").context["timeline"]["lanes"]
        self.assertEqual(
            [lane["user"] for lane in lanes], [self.parent, self.admin, self.kid]
        )

    def test_event_position_is_a_share_of_the_day(self):
        self.event(
            "Soccer",
            datetime(2026, 10, 23, 9, tzinfo=NEW_YORK),
            datetime(2026, 10, 23, 10, 30, tzinfo=NEW_YORK),
            [self.kid, self.parent],
        )
        response = self.day("2026-10-23")
        for user in (self.kid, self.parent):
            [item] = self.lane(response, user)["items"]
            self.assertEqual((item["left"], item["width"]), (37.5, 6.25))
        self.assertEqual(self.lane(response, self.admin)["items"], [])
        self.assertContains(response, "9:00 AM – 10:30 AM")

    def test_dst_days_have_23_or_25_hours(self):
        for date, hours in (("2026-03-08", 23), ("2026-10-23", 24), ("2026-11-01", 25)):
            with self.subTest(date=date):
                timeline = self.day(date).context["timeline"]
                self.assertEqual(timeline["hour_count"], hours)
                self.assertEqual(len(timeline["hours"]), hours)

    def test_event_after_the_dst_change_is_placed_by_real_elapsed_time(self):
        # On Nov 1, 2026, 3 AM is the fifth hour of the day: 1 AM happens twice.
        self.event(
            "Early swim",
            datetime(2026, 11, 1, 3, tzinfo=NEW_YORK),
            datetime(2026, 11, 1, 4, tzinfo=NEW_YORK),
            [self.kid],
        )
        [item] = self.lane(self.day("2026-11-01"), self.kid)["items"]
        self.assertEqual(item["left"], round(4 / 25 * 100, 3))

    def test_event_across_midnight_is_cut_on_both_days(self):
        self.event(
            "Sleepover",
            datetime(2026, 10, 23, 23, tzinfo=NEW_YORK),
            datetime(2026, 10, 24, 1, tzinfo=NEW_YORK),
            [self.kid],
        )
        first = self.day("2026-10-23")
        [item] = self.lane(first, self.kid)["items"]
        self.assertFalse(item["starts_before"])
        self.assertTrue(item["ends_after"])
        self.assertEqual(item["left"] + item["width"], 100)
        self.assertContains(first, "11:00 PM – Oct 24, 1:00 AM")

        second = self.day("2026-10-24")
        [item] = self.lane(second, self.kid)["items"]
        self.assertTrue(item["starts_before"])
        self.assertFalse(item["ends_after"])
        self.assertEqual(item["left"], 0)
        self.assertContains(second, "Oct 23, 11:00 PM – 1:00 AM")

    def test_all_day_events_go_in_their_own_strip(self):
        self.event(
            "Holiday",
            datetime(2026, 10, 23, tzinfo=NEW_YORK),
            datetime(2026, 10, 24, tzinfo=NEW_YORK),
            [self.kid],
        )
        timeline = self.day("2026-10-23").context["timeline"]
        [item] = timeline["all_day"]
        self.assertEqual(item["event"].title, "Holiday")
        self.assertEqual(self.lane_items(timeline), [])

    def lane_items(self, timeline):
        return [item for lane in timeline["lanes"] for item in lane["items"]]

    def test_unassigned_events_go_in_a_whole_family_lane(self):
        self.event(
            "Picnic",
            datetime(2026, 10, 23, 12, tzinfo=NEW_YORK),
            datetime(2026, 10, 23, 14, tzinfo=NEW_YORK),
        )
        response = self.day("2026-10-23")
        family_lane = response.context["timeline"]["lanes"][-1]
        self.assertIsNone(family_lane["user"])
        self.assertEqual(family_lane["items"][0]["event"].title, "Picnic")
        self.assertContains(response, "Whole family")

    def test_google_events_go_in_the_connected_account_lane(self):
        connection = CalendarConnection.objects.create(
            user=self.parent,
            workspace=self.workspace,
            provider=CalendarConnection.Provider.GOOGLE,
        )
        self.event(
            "Dentist",
            datetime(2026, 10, 23, 15, tzinfo=NEW_YORK),
            datetime(2026, 10, 23, 16, tzinfo=NEW_YORK),
            created_by=self.parent,
            source=Event.Source.GOOGLE,
            calendar_connection=connection,
            external_id="abc",
        )
        [item] = self.lane(self.day("2026-10-23"), self.parent)["items"]
        self.assertEqual(item["event"].title, "Dentist")

    def test_default_colours_are_red_for_parents_and_light_blue_for_kids(self):
        response = self.day("2026-10-23")
        self.assertEqual(self.lane(response, self.admin)["color"], "#c62828")
        self.assertEqual(self.lane(response, self.parent)["color"], "#c62828")
        self.assertEqual(self.lane(response, self.kid)["color"], "#9cc9f0")

    def test_parent_can_change_their_own_and_their_kids_colours(self):
        response = self.client.post(
            self.colors_url,
            {
                f"color-{self.parent.pk}": "#2E7D32",
                f"color-{self.kid.pk}": "#ffb74d",
                "next": self.list_url + "?view=day",
            },
        )
        self.assertRedirects(
            response, self.list_url + "?view=day", fetch_redirect_response=False
        )
        reloaded = self.day("2026-10-23")
        self.assertEqual(self.lane(reloaded, self.parent)["color"], "#2e7d32")
        self.assertEqual(self.lane(reloaded, self.kid)["color"], "#ffb74d")
        self.assertEqual(self.lane(reloaded, self.admin)["color"], "#c62828")

    def test_colour_editor_offers_only_self_and_kids(self):
        members = self.day("2026-10-23").context["color_members"]
        self.assertEqual(
            [member["user"] for member in members], [self.parent, self.kid]
        )

    def test_parent_cannot_change_another_parents_colour(self):
        response = self.client.post(
            self.colors_url, {f"color-{self.admin.pk}": "#000000"}
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(MemberColor.objects.exists())

    def test_kid_cannot_change_colours(self):
        self.client.force_login(self.kid)
        response = self.client.post(
            self.colors_url, {f"color-{self.kid.pk}": "#000000"}
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(MemberColor.objects.exists())
        self.assertEqual(self.day("2026-10-23").context["color_members"], [])

    def test_other_families_cannot_reach_the_colours(self):
        self.client.force_login(self.outsider)
        response = self.client.post(
            self.colors_url, {f"color-{self.outsider.pk}": "#000000"}
        )
        self.assertEqual(response.status_code, 404)

    def test_invalid_colour_is_rejected(self):
        response = self.client.post(
            self.colors_url, {f"color-{self.kid.pk}": "red; background: url(x)"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(MemberColor.objects.exists())

    def test_unsafe_next_falls_back_to_the_day_view(self):
        response = self.client.post(
            self.colors_url,
            {f"color-{self.kid.pk}": "#123456", "next": "https://evil.example/"},
        )
        self.assertRedirects(
            response, self.list_url + "?view=day", fetch_redirect_response=False
        )

    def test_page_has_one_view_switcher_and_keeps_the_timezone_note(self):
        response = self.day("2026-10-23")
        content = response.content.decode()
        self.assertEqual(content.count('aria-label="Schedule period"'), 1)
        self.assertNotIn('aria-label="Schedule views"', content)
        self.assertNotIn("Day by day", content)
        self.assertContains(response, "Times are shown in")
