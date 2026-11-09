from django.contrib.auth import get_user_model
from django.template import Context, Template
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Workspace

from ..forms import COMMON_TIMEZONES, EventScheduleForm
from ..templatetags.paint import possessive


class PossessiveTests(SimpleTestCase):
    def test_names_ending_in_s_take_only_an_apostrophe(self):
        self.assertEqual(possessive("The Garcias"), "The Garcias’")

    def test_other_names_take_apostrophe_s(self):
        self.assertEqual(possessive("Ana"), "Ana’s")

    def test_output_is_escaped_in_templates(self):
        html = Template("{% load paint %}{{ name|possessive }}").render(
            Context({"name": "<b>Kim"})
        )
        self.assertEqual(html, "&lt;b&gt;Kim’s")


class TimezoneMenuTests(TestCase):
    def test_menu_lists_common_zones_not_all_of_them(self):
        html = str(EventScheduleForm(auto_id="date_menu_%s")["timezone"])
        self.assertIn('value="America/New_York"', html)
        self.assertIn('value="Europe/Paris"', html)
        self.assertNotIn('value="Antarctica/Troll"', html)
        self.assertLess(html.count("<option"), len(COMMON_TIMEZONES) + 5)
        self.assertIn("Show all time zones", html)
        self.assertIn(f'data-all-timezones="{reverse("events:timezones")}"', html)
        self.assertIn('data-for="date_menu_timezone"', html)

    def test_chosen_zone_outside_the_short_list_stays_selected(self):
        form = EventScheduleForm({"timezone": "Asia/Kathmandu"})
        self.assertTrue(form.is_valid())
        self.assertIn('value="Asia/Kathmandu" selected', str(form["timezone"]))

    def test_current_zone_is_always_listed(self):
        with timezone.override("Pacific/Chatham"):
            html = str(EventScheduleForm()["timezone"])
        self.assertIn('value="Pacific/Chatham"', html)

    def test_any_valid_zone_still_validates(self):
        self.assertTrue(EventScheduleForm({"timezone": "Antarctica/Troll"}).is_valid())
        self.assertFalse(EventScheduleForm({"timezone": "Mars/Olympus"}).is_valid())

    def test_full_list_endpoint(self):
        response = self.client.get(reverse("events:timezones"))
        self.assertEqual(response.status_code, 200)
        names = response.json()
        self.assertIn("Antarctica/Troll", names)
        self.assertEqual(names, sorted(names))
        self.assertIn("max-age=86400", response["Cache-Control"])
        self.assertEqual(self.client.post(reverse("events:timezones")).status_code, 405)


class ShellTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username="Ana")
        cls.workspace = Workspace.objects.create(
            name="The Garcias", primary_admin=cls.user
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_week_heading_is_possessive(self):
        response = self.client.get(reverse("accounts:dashboard"))
        self.assertContains(response, "<h1>The Garcias’ week</h1>", html=True)

    def test_pages_carry_description_manifest_and_touch_icon(self):
        response = self.client.get(reverse("accounts:dashboard"))
        self.assertContains(response, '<meta name="description"')
        self.assertContains(
            response, 'rel="manifest" href="/static/events/manifest.json?v='
        )
        self.assertContains(
            response,
            'rel="apple-touch-icon" href="/static/events/icons/apple-touch-icon.png?v=',
        )

    def test_header_page_size_shrank(self):
        response = self.client.get(reverse("accounts:dashboard"))
        self.assertLess(response.content.decode().count("<option"), 150)

    def test_switches_are_wrapped_for_the_phone_menu(self):
        response = self.client.get(reverse("accounts:dashboard"))
        self.assertContains(response, '<span class="bar-switches">')
        self.assertContains(response, 'id="clock-switch"', count=1)
        self.assertContains(response, 'id="theme-switch"', count=1)
