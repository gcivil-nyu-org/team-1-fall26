"""Pin the query count of the busiest pages, at 1x and 3x the data.

The pinned numbers are the same at both sizes: a count that grows with the data is an N+1.
"""

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .tests_query_fixtures import build_household

# Queries per page, including the session and user lookups.
EXPECTED = {
    "dashboard": 9,
    "day": 11,
    "week": 7,
    "upcoming": 7,
    "chat": 7,
    "profile": 7,
}
# Substring each page must render, so a count can't pass because the data went missing.
RENDERS = {
    "dashboard": "Welcome back",
    "day": "data-event-card",
    "week": "data-event-card",
    "upcoming": "data-event-card",
    "chat": "message-bubble",
    "profile": "Profile",
}


class QueryCountTests(TestCase):
    scale = 1

    def setUp(self):
        parent, workspace = build_household(self.scale)
        self.client.force_login(parent)
        today = timezone.localdate().isoformat()
        schedule = reverse("events:workspace_list", args=[workspace.pk])
        self.urls = {
            "dashboard": reverse("accounts:dashboard"),
            "day": f"{schedule}?view=day&date={today}",
            "week": f"{schedule}?view=week&date={today}",
            "upcoming": f"{schedule}?view=upcoming",
            "chat": reverse("chat:workspace_chat", args=[workspace.pk]),
            "profile": reverse("accounts:profile"),
        }

    def check(self, page):
        with self.assertNumQueries(EXPECTED[page]):
            response = self.client.get(self.urls[page])
        self.assertContains(response, RENDERS[page])

    def test_dashboard(self):
        self.check("dashboard")

    def test_day_timeline(self):
        self.check("day")

    def test_week(self):
        self.check("week")

    def test_upcoming(self):
        self.check("upcoming")

    def test_chat(self):
        self.check("chat")

    def test_profile(self):
        self.check("profile")


class QueryCountTripleDataTests(QueryCountTests):
    scale = 3
