"""Adding an event at a time on the day view.

The day view's right-click menu links to Add event with `time=HH:MM`; the family
chooser carries it along, and the event form starts and ends on it.
"""

from datetime import time

from django.test import TestCase
from django.urls import reverse

from .test_paint_pages import FamilyMixin


class AddAtTimeTests(FamilyMixin, TestCase):
    def setUp(self):
        self.client.force_login(self.ana)
        self.create_url = reverse("events:workspace_create", args=[self.workspace.pk])

    def form_initial(self, **query):
        response = self.client.get(self.create_url, {"date": "2026-10-27", **query})
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        return form.initial.get("start_time"), form.initial.get("end_time")

    def test_time_starts_the_event_and_ends_it_an_hour_later(self):
        self.assertEqual(self.form_initial(time="15:30"), (time(15, 30), time(16, 30)))

    def test_late_time_keeps_the_end_on_the_same_day(self):
        self.assertEqual(self.form_initial(time="23:30"), (time(23, 30), time(23, 59)))

    def test_missing_or_invalid_time_leaves_the_times_empty(self):
        self.assertEqual(self.form_initial(), (None, None))
        self.assertEqual(self.form_initial(time="25:99"), (None, None))
        self.assertEqual(self.form_initial(time="<script>"), (None, None))

    def test_family_chooser_passes_the_time_along(self):
        response = self.client.get(
            reverse("events:choose_workspace"),
            {"date": "2026-10-27", "time": "09:00"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(self.create_url))
        self.assertIn("time=09%3A00", response.url)
        self.assertIn("date=2026-10-27", response.url)

    def test_day_view_offers_add_at_a_time_to_parents(self):
        response = self.client.get(
            reverse("events:workspace_list", args=[self.workspace.pk]),
            {"view": "day", "date": "2026-10-27"},
        )
        self.assertContains(
            response,
            f'data-add-at="{self.create_url}?date=2026-10-27'
            '&amp;timezone=America%2FNew_York"',
        )
        self.assertContains(response, 'aria-keyshortcuts="Shift+F10"')

    def test_kids_get_no_add_menu_on_the_day_view(self):
        self.client.force_login(self.lucia)
        response = self.client.get(
            reverse("events:list"), {"view": "day", "date": "2026-10-27"}
        )
        self.assertNotContains(response, "data-add-at")
        self.assertNotContains(response, "aria-keyshortcuts")
