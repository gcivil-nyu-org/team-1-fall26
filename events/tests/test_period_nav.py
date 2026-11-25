"""The period bar, the month view and the weekends switch."""

from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone as django_timezone

from accounts.models import Membership, Workspace
from syncmily.limits import RENDER

from ..models import Event

NEW_YORK = ZoneInfo("America/New_York")


class PeriodNavTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.parent = User.objects.create_user(username="Ana")
        cls.kid = User.objects.create_user(username="Lucia")
        cls.workspace = Workspace.objects.create(name="Reyes", primary_admin=cls.parent)
        Membership.objects.create(
            workspace=cls.workspace,
            user=cls.kid,
            role=Membership.Role.KID,
            invited_by=cls.parent,
            accepted_at=django_timezone.now(),
        )
        cls.list_url = reverse("events:workspace_list", args=[cls.workspace.pk])

    def setUp(self):
        self.client.force_login(self.parent)

    def event(self, title, day, hour=10, participants=()):
        event = Event.objects.create(
            title=title,
            starts_at=datetime(2026, 10, day, hour, tzinfo=NEW_YORK),
            ends_at=datetime(2026, 10, day, hour + 1, tzinfo=NEW_YORK),
            timezone_name="America/New_York",
            workspace=self.workspace,
            created_by=self.parent,
        )
        event.participants.set(participants)
        return event

    def month(self, **extra):
        return self.client.get(
            self.list_url,
            {
                "view": "month",
                "date": "2026-10-14",
                "timezone": "America/New_York",
                **extra,
            },
        )

    def test_week_has_arrows_with_names_and_no_previous_next_words(self):
        response = self.client.get(
            reverse("accounts:dashboard"), {"week": "2026-10-26"}
        )
        self.assertContains(response, 'aria-label="Previous week"')
        self.assertContains(response, 'aria-label="Next week"')
        self.assertContains(response, "?week=2026-10-19")
        self.assertContains(response, "?week=2026-11-02")
        self.assertNotContains(response, ">Previous<")
        self.assertNotContains(response, ">This week<")

    def test_view_menu_lists_day_week_month_list_with_shortcuts(self):
        response = self.client.get(reverse("accounts:dashboard"))
        for label, key in (("Day", "D"), ("Week", "W"), ("Month", "M"), ("List", "L")):
            self.assertContains(response, f'data-shortcut="{key}"')
            self.assertContains(response, f"<span>{label}</span>")
        self.assertContains(response, 'aria-label="Schedule period"', count=1)

    def test_month_grid_covers_whole_weeks_and_places_plans(self):
        self.event("Swim", 14, participants=[self.kid])
        self.event("Picnic", 31)
        response = self.month()
        self.assertEqual(response.status_code, 200)
        month = response.context["month"]
        # October 2026 starts on a Thursday and ends on a Saturday: 5 Monday weeks.
        self.assertEqual(len(month["weeks"]), 5)
        self.assertEqual(month["weeks"][0][0]["date"].isoformat(), "2026-09-28")
        self.assertFalse(month["weeks"][0][0]["in_month"])
        day = next(
            d
            for w in month["weeks"]
            for d in w
            if d["date"].day == 14 and d["in_month"]
        )
        self.assertEqual(day["items"][0]["event"].title, "Swim")
        self.assertEqual(day["items"][0]["member"]["user"], self.kid)
        self.assertContains(response, "<h1>October 2026</h1>", html=False)
        self.assertContains(response, 'aria-label="Previous month"')
        self.assertContains(response, "date=2026-09-01")
        self.assertContains(response, "date=2026-11-01")

    def test_month_shows_three_plans_then_a_count(self):
        for hour in (8, 10, 12, 14, 16):
            self.event(f"Plan {hour}", 20, hour)
        response = self.month()
        day = next(
            d
            for w in response.context["month"]["weeks"]
            for d in w
            if d["date"].day == 20
        )
        self.assertEqual(len(day["items"]), 3)
        self.assertEqual(day["more"], 2)
        self.assertContains(response, "+2 more")

    def test_hiding_weekends_drops_saturday_and_sunday_from_week_and_month(self):
        response = self.client.post(
            reverse("events:weekends"), {"next": reverse("accounts:dashboard")}
        )
        self.assertRedirects(
            response, reverse("accounts:dashboard"), fetch_redirect_response=False
        )
        week = self.client.get(reverse("accounts:dashboard"), {"week": "2026-10-26"})
        self.assertEqual(len(week.context["week"]["days"]), 5)
        self.assertContains(week, 'aria-pressed="false"')
        self.assertContains(week, 'style="--days: 5"')
        month = self.month()
        self.assertTrue(all(len(w) == 5 for w in month.context["month"]["weeks"]))
        # Toggle back on.
        self.client.post(reverse("events:weekends"))
        week = self.client.get(reverse("accounts:dashboard"), {"week": "2026-10-26"})
        self.assertEqual(len(week.context["week"]["days"]), 7)

    def test_weekends_switch_needs_post_and_ignores_unsafe_next(self):
        self.assertEqual(self.client.get(reverse("events:weekends")).status_code, 405)
        response = self.client.post(
            reverse("events:weekends"), {"next": "https://evil.example/"}
        )
        self.assertRedirects(
            response, reverse("accounts:dashboard"), fetch_redirect_response=False
        )

    def test_kid_gets_arrows_but_no_menu_and_always_sees_weekends(self):
        self.client.post(reverse("events:weekends"))
        self.client.force_login(self.kid)
        session = self.client.session
        session["hide_weekends"] = True
        session.save()
        response = self.client.get(reverse("accounts:dashboard"))
        self.assertContains(response, 'aria-label="Previous week"')
        self.assertNotContains(response, "data-shortcut")
        self.assertEqual(len(response.context["week"]["days"]), 7)

    def test_list_view_has_the_menu_but_no_arrows(self):
        response = self.client.get(self.list_url, {"view": "upcoming"})
        self.assertContains(response, 'data-shortcut="M"')
        self.assertNotContains(response, "arrow-btn")

    def test_far_off_dates_never_crash_the_month_or_list_view(self):
        for view in ("month", "upcoming"):
            for date in ("0001-01-01", "9999-12-31"):
                for zone in ("Pacific/Kiritimati", "Pacific/Pago_Pago"):
                    with self.subTest(view=view, date=date, zone=zone):
                        response = self.client.get(
                            self.list_url,
                            {"view": view, "date": date, "timezone": zone},
                        )
                        self.assertLess(response.status_code, 500)

    def test_kid_month_shows_only_plans_a_kid_may_see(self):
        self.event("Parents only", 14, participants=[self.parent])
        self.event("Lucia swim", 14, participants=[self.kid])
        self.client.force_login(self.kid)
        response = self.month()
        self.assertContains(response, "Lucia swim")
        self.assertNotContains(response, "Parents only")

    def links(self, response):
        return {
            link["key"]: link["url"] for link in response.context["period_nav"]["links"]
        }

    def week_list(self):
        return self.client.get(
            self.list_url,
            {"view": "week", "date": "2026-10-26", "timezone": "America/New_York"},
        )

    def test_a_period_that_holds_today_opens_the_other_views_on_today(self):
        tuesday = datetime(2026, 11, 24, 12, tzinfo=NEW_YORK)
        zone = {"timezone": "America/New_York"}
        with mock.patch("django.utils.timezone.now", return_value=tuesday):
            pages = {
                "this week": self.client.get(reverse("accounts:dashboard")),
                "this month": self.client.get(
                    self.list_url, {"view": "month", "date": "2026-11-01", **zone}
                ),
                "this week's list": self.client.get(
                    self.list_url, {"view": "week", "date": "2026-11-23", **zone}
                ),
            }
            next_week = self.client.get(
                reverse("accounts:dashboard"), {"week": "2026-11-30"}
            )
            december = self.client.get(
                self.list_url, {"view": "month", "date": "2026-12-01", **zone}
            )
        for name, response in pages.items():
            for view in ("day", "month", "upcoming"):
                with self.subTest(page=name, view=view):
                    self.assertIn("date=2026-11-24", self.links(response)[view])
        # Away from today, the links keep the first day on screen.
        self.assertIn("date=2026-11-30", self.links(next_week)["day"])
        self.assertIn("date=2026-12-01", self.links(december)["day"])

    def test_the_list_week_leaves_out_the_weekend_when_weekends_are_hidden(self):
        self.event("Friday swim", 30)
        self.event("Saturday match", 31)
        self.assertContains(self.week_list(), "Saturday match")
        self.client.post(reverse("events:weekends"))
        response = self.week_list()
        self.assertContains(response, "Friday swim")
        self.assertNotContains(response, "Saturday match")
        self.assertContains(response, "Mon 26 Oct to Fri 30 Oct, 1 plan")
        self.assertContains(response, "Show weekends")

    def test_a_kid_always_sees_weekends_and_is_never_offered_the_switch(self):
        self.event("Saturday match", 31, participants=[self.kid])
        self.assertContains(self.month(), "Show weekends")
        self.client.force_login(self.kid)
        self.client.post(reverse("events:weekends"))
        month = self.month()
        self.assertTrue(all(len(week) == 7 for week in month.context["month"]["weeks"]))
        self.assertNotContains(month, "Show weekends")
        week = self.week_list()
        self.assertContains(week, "Saturday match")
        self.assertNotContains(week, "Show weekends")

    def test_month_draws_at_most_100_plans_and_counts_the_rest(self):
        for number in range(RENDER.plans_per_response + 3):
            self.event(f"Plan {number}", 1 + number % 28)
        response = self.month()
        placed = sum(
            day["count"] for week in response.context["month"]["weeks"] for day in week
        )
        self.assertEqual(placed, RENDER.plans_per_response)
        self.assertContains(
            response, "This month has 3 more plans than this page shows."
        )
        self.assertNotContains(self.month(date="2026-12-14"), "than this page shows")

    def test_only_the_four_shortcut_letters_reach_the_selector(self):
        """A quote key would make the selector invalid, so the key is checked first."""
        with open(finders.find("events/shell.js"), encoding="utf-8") as handle:
            script = handle.read()
        check = script.index("if (!/^[dwml]$/i.test(event.key)) return;")
        self.assertLess(check, script.index("a[data-shortcut="))


class PeriodLabelTests(TestCase):
    def test_button_names_the_period_once_you_move_away(self):
        from datetime import date

        from ..nav import period_label

        d = date
        self.assertEqual(
            period_label("week", d(2026, 11, 2), d(2026, 11, 8), True), "This week"
        )
        self.assertEqual(
            period_label("week", d(2026, 11, 2), d(2026, 11, 8), False), "Nov 2 – 8"
        )
        self.assertEqual(
            period_label("week", d(2026, 10, 26), d(2026, 11, 1), False),
            "Oct 26 – Nov 1",
        )
        self.assertEqual(
            period_label("day", d(2026, 10, 31), d(2026, 10, 31), False), "Sat 31 Oct"
        )
        self.assertEqual(
            period_label("day", d(2026, 10, 31), d(2026, 10, 31), True), "Today"
        )
        self.assertEqual(
            period_label("month", d(2026, 12, 1), d(2026, 12, 31), False),
            "December 2026",
        )
